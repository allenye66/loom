"""git worktree operations (shell out to git — most reliable for worktrees)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True)


def slugify(branch: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", branch).strip("-").lower() or "task"


def add_worktree(repo_root: str, branch: str, worktree_path: str, base_branch: str) -> None:
    """Create a worktree for `branch`.

    A NEW branch is always cut from the *latest* `origin/<base_branch>` (e.g. origin/develop):
    we fetch it first so the worktree starts current and avoids stale-base merge conflicts. The
    base branch is never itself worktreed or checked out — we branch off its remote-tracking ref,
    so the user's local `develop` is left untouched.
    """
    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "fetch", "origin", base_branch], cwd=repo_root)  # refresh origin/<base> (best-effort)
    branch_exists = _run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo_root).returncode == 0
    if branch_exists:
        # Reusing an existing branch → take it as-is (it may not be based on the latest base).
        cp = _run(["git", "worktree", "add", worktree_path, branch], cwd=repo_root)
    else:
        # Cut the new branch from the just-fetched origin/<base> (latest); fall back to a local
        # <base> only if there's no remote-tracking ref (offline / no origin).
        remote_ref = f"origin/{base_branch}"
        has_remote = _run(["git", "rev-parse", "--verify", "--quiet", remote_ref], cwd=repo_root).returncode == 0
        base_ref = remote_ref if has_remote else base_branch
        cp = _run(["git", "worktree", "add", "-b", branch, worktree_path, base_ref], cwd=repo_root)
    if cp.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {cp.stderr.strip() or cp.stdout.strip()}")


def remove_worktree(repo_root: str, worktree_path: str, force: bool = False) -> None:
    args = ["git", "worktree", "remove", worktree_path]
    if force:
        args.append("--force")
    cp = _run(args, cwd=repo_root)
    if cp.returncode != 0:
        raise RuntimeError(f"git worktree remove failed: {cp.stderr.strip()}")


def git_status(worktree_path: str) -> dict:
    # --no-optional-locks: this runs on loom's poll loop, so it must never take git's index
    # lock — otherwise a poll can collide with the user's own `git add`/`commit` in the same
    # worktree ("Unable to create '…/index.lock': File exists").
    #
    # ONE `status --porcelain=v2 --branch` instead of the old three spawns (status + rev-parse +
    # rev-list): with dozens of worktrees polled continuously, 3× the git process spawns was a
    # major source of the CPU/GIL storm that starved the terminal event loop. v2 --branch carries
    # branch + ahead/behind (`# branch.ab +A -B`) + dirtiness (any non-`#` line) in a single call.
    cp = _run(["git", "--no-optional-locks", "status", "--porcelain=v2", "--branch"], cwd=worktree_path)
    branch = ""
    ahead = behind = 0
    dirty = False
    for line in cp.stdout.splitlines():
        if line.startswith("# branch.head "):
            branch = line[14:].strip()
        elif line.startswith("# branch.ab "):
            for p in line[12:].split():
                if p.startswith("+"):
                    ahead = int(p[1:])
                elif p.startswith("-"):
                    behind = int(p[1:])
        elif line and not line.startswith("#"):
            dirty = True
    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}
