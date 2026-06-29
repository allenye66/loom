"""FastAPI app: REST API under /api, and the built dashboard served at /."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from loom import __version__
from loom.core import monitor
from loom.server.api import router

DASHBOARD_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Dev-stack supervisor: keep active chats' frontend/backend up (see core/monitor.py).
    task = monitor.start()
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task


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
    app.include_router(router, prefix="/api")

    if DASHBOARD_DIST.exists():
        app.mount("/", StaticFiles(directory=str(DASHBOARD_DIST), html=True), name="dashboard")

    return app


app = create_app()
