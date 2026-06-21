"""Isolated test runs.

The blocking pain ("run /address-comments on PR B while coding on A") is solved
here: a worktree can run its suite independently of the others. When the suite
shares one test resource (e.g. a single test DB), loom isolates concurrent runs by:

  serialize  — a global file lock; concurrent runs queue. Works out of the box.
               Safe but not parallel.
  db-suffix  — inject LOOM_TEST_DB_SUFFIX so the suite can name its test DB per
               branch (the suite must read this var).
  db-port    — point the run at a per-worktree test DB on <base>+offset (the suite
               must read LOOM_TEST_DB_PORT).
"""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Iterator

from loom.core.config import LOOM_HOME, RepoConfig, ensure_dirs
from loom.models import Task


def _render(s: str, ctx: dict) -> str:
    for k, v in ctx.items():
        s = s.replace("{" + k + "}", str(v))
    return s


def build_test_run(task: Task, cfg: RepoConfig, pytest_args: str = "") -> tuple[str, str, dict]:
    ctx: dict = {
        "worktree": task.worktree_path,
        "repo_root": cfg.root,
        "slug": task.id,
        "pytest_args": pytest_args,
    }
    if task.ports:
        ctx["backend_port"] = task.ports.backend
        ctx["frontend_port"] = task.ports.frontend

    command = _render(cfg.test.command, ctx)
    cwd = _render(cfg.test.cwd, ctx)
    env = {k: _render(v, ctx) for k, v in cfg.test.env.items()}

    if cfg.test.isolation in ("db-suffix", "db-port"):
        env.setdefault("LOOM_TEST_DB_SUFFIX", task.id.replace("-", "_"))
        if cfg.test.isolation == "db-port" and task.ports:
            env.setdefault("LOOM_TEST_DB_PORT", str(7432 + task.ports.offset))
    return command, cwd, env


@contextlib.contextmanager
def serialize_lock() -> Iterator[None]:
    """Cross-process lock so two worktrees don't hit the one shared test resource at once."""
    ensure_dirs()
    f = open(LOOM_HOME / "test.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
