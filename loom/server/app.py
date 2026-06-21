"""FastAPI app: REST API under /api, and the built dashboard served at /."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from loom import __version__
from loom.server.api import router

DASHBOARD_DIST = Path(__file__).resolve().parents[2] / "dashboard" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="loom", version=__version__)
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
