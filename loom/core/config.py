"""loom global paths + per-repo `.loom.yaml` config loading.

A target repo describes its services/tests in a `.loom.yaml` committed at its
root, so config travels with the code and loom itself stays project-agnostic.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# --- loom's own home / runtime paths -----------------------------------------
LOOM_HOME = Path(os.environ.get("LOOM_HOME", Path.home() / ".loom"))
REGISTRY_PATH = LOOM_HOME / "registry.json"
LOGS_DIR = LOOM_HOME / "logs"
DEFAULT_WORKTREE_BASE = LOOM_HOME / "worktrees"

# loom's API runs on a high port so it never collides with the 8000/3000
# ranges it hands out to worktrees.
LOOM_API_HOST = os.environ.get("LOOM_API_HOST", "127.0.0.1")
LOOM_API_PORT = int(os.environ.get("LOOM_API_PORT", "8787"))


def ensure_dirs() -> None:
    for d in (LOOM_HOME, LOGS_DIR, DEFAULT_WORKTREE_BASE):
        d.mkdir(parents=True, exist_ok=True)


# --- per-repo config ----------------------------------------------------------
class TestConfig(BaseModel):
    command: str = "pytest"
    cwd: str = "{worktree}"
    env: dict[str, str] = Field(default_factory=dict)
    # serialize  -> take a global lock so concurrent runs don't clash on one
    #               shared test resource (e.g. a single test DB). Works out of the box.
    # db-suffix  -> inject LOOM_TEST_DB_SUFFIX so the suite names its test DB per branch.
    # db-port    -> point the run at a per-worktree test DB on <base>+offset.
    isolation: str = "serialize"


class ServiceConfig(BaseModel):
    name: str
    cwd: str = "{worktree}"
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    health: str | None = None


class RepoConfig(BaseModel):
    name: str
    root: str
    base_branch: str = "main"
    worktree_base: str | None = None
    setup: list[str] = Field(default_factory=list)
    # logical port name -> base port (e.g. {"backend": 8000, "frontend": 3000})
    ports: dict[str, int] = Field(default_factory=dict)
    services: list[ServiceConfig] = Field(default_factory=list)
    test: TestConfig = Field(default_factory=TestConfig)
    # Command to open a worktree in an editor (the chat header's edit button). A full
    # command template; `{worktree}` is substituted (a bare command like "code" gets the
    # path appended). Per-machine override: the `$LOOM_EDITOR` env var.
    editor: str = "cursor --new-window {worktree}"


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    repo_root = Path(repo_root).expanduser().resolve()
    cfg_path = repo_root / ".loom.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No .loom.yaml at {repo_root}. Copy one from loom/projects/ or run `loom init-repo`."
        )
    data: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}
    data.setdefault("root", str(repo_root))
    data.setdefault("name", repo_root.name)
    return RepoConfig(**data)
