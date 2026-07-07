"""High-level task lifecycle — the API the CLI and HTTP server both call."""

from __future__ import annotations

import os
import subprocess
import time
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
    if only:
        # Clean (re)start of just the named services: free their port from whatever is actually
        # LISTENING now (kill_port is LISTEN-only). Do NOT kill_group(svc.pid) — a stored pid can be
        # stale/reused and the supervisor calls this on a loop, so acting on current port state
        # (not a remembered pid) avoids killing an unrelated process. Other services stay running.
        for svc in task.services:
            if svc.name in only and svc.port:
                process.kill_port(svc.port)
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
    # When starting a subset, keep the services we didn't touch + the ones we just (re)started.
    task.services = ([s for s in task.services if s.name not in only] + procs) if only else procs
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


# git_status + health probes are the bulk of `GET /api/tasks` — and with dozens of worktrees they
# were O(N) git shell-outs (3 spawns each) + HTTP health probes ON THE REQUEST PATH, every poll.
# That storm ran in the server threadpool and starved the asyncio loop that drives the terminal
# PTY (measured: /api/tasks taking 4–21s, with matching event-loop stalls → laggy typing).
#
# Now the probing runs OFF the request path: a background sweeper (server lifespan) calls
# refresh_status_all() every few seconds to warm this cache; refresh_status() (request path) only
# READS it and never shells out. So /api/tasks is just cache reads + a warm build_index.
_status_cache: dict[str, tuple[float, dict, dict]] = {}  # task_id -> (ts, git, {service: healthy})
# `git status` on these worktrees runs 0.5–9s each (large trees on disk), so re-probing all N every
# sweep would keep git churning the disk continuously. Skip any task whose cache is younger than
# this — the sweeper then only refreshes the stale ones each tick, spreading the load.
_MIN_REFRESH_AGE = 15.0


def _probe(task: Task, *, git: bool = True) -> None:
    """Liveness/health (+ optionally git) probe for one task → writes _status_cache. Runs on the
    background sweeper thread, never the request path. `git=False` skips the slow `git status`
    worktree walk and keeps whatever git value was last cached — used for running-but-not-open
    tasks, where we still want a live health dot but the branch/dirty state isn't worth re-walking."""
    for svc in task.services:
        alive = process.is_alive(svc.pid) if svc.pid else False
        svc.healthy = process.health_check(svc.health_url) if (alive and svc.health_url) else alive
    if git and Path(task.worktree_path).exists():
        git_val = wt.git_status(task.worktree_path)
    else:
        prev = _status_cache.get(task.id)
        git_val = prev[1] if prev else {}  # keep last-known git when not re-walking
    _status_cache[task.id] = (time.monotonic(), git_val, {s.name: s.healthy for s in task.services})


def _safe_probe(task: Task, *, git: bool = True) -> None:
    try:
        _probe(task, git=git)
    except Exception:  # noqa: BLE001 — one bad worktree must not stop the sweep
        pass


def sweep_status(tasks: list[Task], active_cwds: set[str], cold_batch: int = 3) -> None:
    """Background status sweep — the ONLY place `git status` runs (never the request path).

    The slow part is `git status`, which on these worktrees takes 0.5–9s each; re-walking all N
    every tick was the CPU/disk storm that starved the terminal loop. So we walk git only for
    worktrees with an **open terminal session** (`active_cwds`) — the ones you're actually looking
    at — throttled to `_MIN_REFRESH_AGE`. Running-but-not-open tasks get a cheap health-only refresh
    (dev-stack dot stays live) with NO git walk. Idle tasks get a one-time cold fill (`cold_batch`
    per sweep) so their cards aren't blank, then keep that value until you open them.

    Sequential on purpose — subprocess.run releases the GIL while git runs, so one-at-a-time lets
    the event loop breathe between spawns (a 12-wide fan-out was itself a GIL storm)."""
    now = time.monotonic()
    cold = 0
    for task in tasks:
        cached = _status_cache.get(task.id)
        fresh = cached is not None and now - cached[0] < _MIN_REFRESH_AGE
        if task.worktree_path in active_cwds:
            if not fresh:
                _safe_probe(task, git=True)  # open terminal → keep git fresh
        elif task.services:
            if not fresh:
                _safe_probe(task, git=False)  # running stack → health only, skip the slow git walk
        elif cached is None and cold < cold_batch:
            _safe_probe(task, git=True)  # idle + never probed → fill once so the card isn't blank
            cold += 1


def refresh_status(task: Task) -> dict:
    """Request path: return the sweeper's cached git status + apply cached service health. Never
    shells out. Returns {} (git unknown) until the first background sweep warms the cache — a
    brief cold-start window, not a per-request cost."""
    hit = _status_cache.get(task.id)
    if hit:
        _, git, health = hit
        for svc in task.services:
            svc.healthy = bool(health.get(svc.name, False))
        return git
    # Not warmed yet — cheap PID liveness only (no git subprocess, no HTTP health probe).
    for svc in task.services:
        svc.healthy = process.is_alive(svc.pid) if svc.pid else False
    return {}
