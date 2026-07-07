"""FastAPI app: REST API under /api, and the built dashboard served at /."""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from loom import __version__
from loom.core import manager, monitor, perf, registry, terminals
from loom.server.api import router

DASHBOARD_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"

_SWEEP_INTERVAL = 8.0  # seconds between background git/health refreshes


def _sweep_once() -> None:
    active_cwds = {ts.cwd for ts in terminals.active_sessions() if ts.cwd}
    manager.sweep_status(registry.list_tasks(), active_cwds)


async def _status_sweeper() -> None:
    """Warm manager's git/health cache off the request path, so /api/tasks never shells out
    git on the request path (which was starving the terminal's event loop). Only worktrees with
    an open terminal or a running dev stack are refreshed continuously — see manager.sweep_status.
    Runs the blocking probe in an executor thread; git releases the GIL while running, so the loop
    keeps servicing the PTY between spawns."""
    loop = asyncio.get_running_loop()
    while True:
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, _sweep_once)
        await asyncio.sleep(_SWEEP_INTERVAL)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Dev-stack supervisor: keep active chats' frontend/backend up (see core/monitor.py).
    task = monitor.start()
    sweeper = asyncio.create_task(_status_sweeper())
    lag = asyncio.create_task(perf.loop_lag_monitor()) if perf.enabled() else None
    try:
        yield
    finally:
        for t in (task, sweeper, lag):
            if t:
                t.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await t


def create_app() -> FastAPI:
    app = FastAPI(title="loom", version=__version__, lifespan=_lifespan)
    # Single-user localhost tool — permissive CORS so the Vite dev server (5173)
    # can talk to the API while you hack on the dashboard.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Time every API request; log the slow ones. A sync endpoint's wall-clock here also
    # captures time spent waiting for a threadpool slot — i.e. server-side contention that
    # the UI feels as lag. Only /api/* (skips static asset serving).
    if perf.enabled():
        @app.middleware("http")
        async def _timing(request: Request, call_next):  # noqa: ANN001
            t0 = time.perf_counter()
            resp = await call_next(request)
            dt = (time.perf_counter() - t0) * 1000
            if dt > perf.SLOW_REQ_MS and request.url.path.startswith("/api/"):
                perf.log(f"SLOW {request.method} {request.url.path} {dt:.0f}ms")
            return resp

    app.include_router(router, prefix="/api")

    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=str(DASHBOARD_DIST), html=True), name="dashboard")

    return app


app = create_app()
