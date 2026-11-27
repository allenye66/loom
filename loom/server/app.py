"""FastAPI app: REST API under /api, and the built dashboard served at /."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from loom import __version__
from loom.core.config import LOOM_API_HOST
from loom.core import manager, monitor, perf, radar, registry, sessindex, sessions, terminals
from loom.server.api import router
from loom.server.guard import CORS_ORIGIN_REGEX, LoopbackOnlyMiddleware
from loom.server.board_api import router as board_router
from loom.server.inbox_api import router as inbox_router

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
    n = 0
    while True:
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, _sweep_once)
        n += 1
        if n % 8 == 0:  # ~once a minute: bind any chat whose conversation turned up under another session id
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, sessions.repair_all)
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, radar.compute_all)
        await asyncio.sleep(_SWEEP_INTERVAL)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Dev-stack supervisor: keep active chats' frontend/backend up (see core/monitor.py).
    task = monitor.start()
    sweeper = asyncio.create_task(_status_sweeper())
    # Chats whose conversation came out under another session id before bindings existed get bound
    # now (off the request path; a stat per task, a transcript scan only for the rare mismatch).
    asyncio.get_running_loop().run_in_executor(None, sessions.repair_all)
    lag = asyncio.create_task(perf.loop_lag_monitor()) if perf.enabled() else None
    sessindex.start()  # background session index (D33): facts now, Haiku summaries as sessions go quiet
    try:
        yield
    finally:
        sessindex.stop()
        for t in (task, sweeper, lag):
            if t:
                t.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await t


def create_app() -> FastAPI:
    app = FastAPI(title="loom", version=__version__, lifespan=_lifespan)
    # Single-user localhost tool. CORS is answered only to loopback origins (any port — the Vite dev
    # server, per-worktree frontends, the dashboard itself), never `*`; and the guard below refuses any
    # request whose Host or Origin isn't this machine (see server/guard.py: DNS rebinding, foreign sites).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(LoopbackOnlyMiddleware, bind_host=os.environ.get("LOOM_BIND_HOST") or LOOM_API_HOST)  # outermost
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
    app.include_router(board_router, prefix="/api")
    app.include_router(inbox_router, prefix="/api")

    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=str(DASHBOARD_DIST), html=True), name="dashboard")

    return app


app = create_app()
