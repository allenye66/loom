"""High-level task lifecycle — the API the CLI and HTTP server both call."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from loom.core import ports as ports_mod
from loom.core import process, registry
from loom.core import worktree as wt
from loom.core.config import DEFAULT_WORKTREE_BASE, LOGS_DIR, RepoConfig, ensure_dirs
from loom.models import ServiceProc, Task, TaskState


def _ctx(cfg: RepoConfig, task: Task) -> dict:
    ctx: dict = {"worktree": task.worktree_path, "repo_root": cfg.root, "slug": task.id}
    if task.ports:
        ctx |= {
            "backend_port": task.ports.backend,
            "frontend_port": task.ports.frontend,
            # {offset} = this worktree's stable hash offset; a repo derives any other
            # per-worktree index (e.g. a DB number) from it in its .loom.yaml.
            "offset": task.ports.offset,
        }
    return ctx


def _render(s: str, ctx: dict) -> str:
    for k, v in ctx.items():
        s = s.replace("{" + k + "}", str(v))
    return s


# --- create / remove ----------------------------------------------------------
def create_task(cfg: RepoConfig, branch: str, base_branch: str | None = None, note: str | None = None) -> Task:
    branch = (branch or "").strip()
    # Reject anything git can't use as a branch (spaces are the usual culprit) *before*
    # persisting, so a typo never leaves a broken task pointing at a worktree that the
    # `git worktree add` failed to create.
    if not branch or subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"], capture_output=True
    ).returncode != 0:
        raise ValueError(
            f"'{branch}' isn't a valid git branch name — no spaces or special characters "
            "(try e.g. 'booking-network-error')"
        )
    slug = wt.slugify(branch)
    if registry.get_task(slug):
        raise ValueError(f"Task '{slug}' already exists")
    base = base_branch or cfg.base_branch
    wt_base = Path(cfg.worktree_base).expanduser() if cfg.worktree_base else (DEFAULT_WORKTREE_BASE / cfg.name)
    wt_path = str(wt_base / slug)

    taken = {t.ports.offset for t in registry.list_tasks() if t.ports}
    alloc = ports_mod.allocate(slug, cfg.ports.get("backend", 8000), cfg.ports.get("frontend", 3000), taken)

    task = Task(
        id=slug, repo=cfg.name, repo_root=cfg.root, branch=branch, base_branch=base,
        worktree_path=wt_path, state=TaskState.created, ports=alloc,
        created_at=registry.now_iso(), updated_at=registry.now_iso(), note=note,
        chat_id=str(uuid.uuid4()),  # the task's one chat, created with this id on first open
    )
    registry.upsert(task)
    try:
        wt.add_worktree(cfg.root, branch, wt_path, base)
        _run_setup(cfg, task)
        task.state, task.note = TaskState.ready, None
    except Exception as e:  # noqa: BLE001 — surface to the user via task.note
        task.state, task.note = TaskState.error, str(e)
    return registry.upsert(task)


def _run_setup(cfg: RepoConfig, task: Task) -> None:
    ctx = _ctx(cfg, task)
    for cmd in cfg.setup:
        subprocess.run(_render(cmd, ctx), shell=True, cwd=task.worktree_path, capture_output=True, text=True)


def remove_task(task_id: str, force: bool = False) -> None:
    task = registry.get_task(task_id)
    if not task:
        return
    stop_task(task_id)
    try:
        wt.remove_worktree(task.repo_root, task.worktree_path, force=force)
    except Exception:
        if not force:
            raise
    registry.delete(task_id)


# --- start / stop services (Phase 2) -----------------------------------------
def start_task(cfg: RepoConfig, task_id: str, only: set[str] | None = None) -> Task:
    task = registry.get_task(task_id)
    if not task:
        raise ValueError(f"unknown task '{task_id}'")
    ensure_dirs()
    procs: list[ServiceProc] = []
    for svc in cfg.services:
        if only and svc.name not in only:
            continue
        ctx = _ctx(cfg, task)
        pid = process.spawn(
            _render(svc.command, ctx),
            _render(svc.cwd, ctx),
            {k: os.path.expandvars(_render(v, ctx)) for k, v in svc.env.items()},
            str(LOGS_DIR / f"{task.id}-{svc.name}.log"),
        )
        port = None
        if task.ports:
            port = {"backend": task.ports.backend, "frontend": task.ports.frontend}.get(svc.name)
        procs.append(
            ServiceProc(name=svc.name, pid=pid, port=port, health_url=_render(svc.health, ctx) if svc.health else None)
        )
    task.services = procs
    task.state = TaskState.running
    return registry.upsert(task)


def stop_task(task_id: str) -> None:
    task = registry.get_task(task_id)
    if not task:
        return
    for svc in task.services:
        if svc.pid:
            process.kill_group(svc.pid)
        if svc.port:
            process.kill_port(svc.port)
    task.services = []
    if task.state == TaskState.running:
        task.state = TaskState.stopped
    registry.upsert(task)


def refresh_status(task: Task) -> dict:
    """Update in-memory health/liveness; return git status. Caller persists."""
    for svc in task.services:
        alive = process.is_alive(svc.pid) if svc.pid else False
        svc.healthy = process.health_check(svc.health_url) if (alive and svc.health_url) else alive
    return wt.git_status(task.worktree_path) if Path(task.worktree_path).exists() else {}
