"""loom global paths + per-repo `.loom.yaml` config loading.

A target repo describes its services/tests in a `.loom.yaml` committed at its
root, so config travels with the code and loom itself stays project-agnostic.
The file is optional: a repo without one is a plain project (no services/setup)
on git's default branch.
"""

from __future__ import annotations

import functools
import os
import subprocess
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


@functools.lru_cache(maxsize=64)
def default_branch(repo_root: str) -> str:
    """git's default branch for a repo: what origin/HEAD points at, else a local `main` or
    `master`, else whatever is checked out, else "main"."""

    def git(*args: str) -> str | None:
        r = subprocess.run(["git", "-C", repo_root, *args], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    origin_head = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if origin_head:
        return origin_head.removeprefix("origin/")
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return git("symbolic-ref", "--quiet", "--short", "HEAD") or "main"


def load_repo_config(repo_root: str | Path) -> RepoConfig:
    """The repo's `.loom.yaml`, with `root` / `name` / `base_branch` defaulted from the repo
    itself. A repo without the file gets an all-defaults config (no services, setup or
    ports), so any git repo can be added as a project without writing one first."""
    repo_root = Path(repo_root).expanduser().resolve()
    cfg_path = repo_root / ".loom.yaml"
    data: dict[str, Any] = (yaml.safe_load(cfg_path.read_text()) or {}) if cfg_path.exists() else {}
    data.setdefault("root", str(repo_root))
    data.setdefault("name", repo_root.name)
    if "base_branch" not in data:
        data["base_branch"] = default_branch(str(repo_root))
    return RepoConfig(**data)
