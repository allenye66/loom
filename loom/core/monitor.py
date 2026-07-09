"""Dev-stack supervisor + reaper for loom tasks.

Two opposing lifecycle jobs share the background loop here:

**Supervisor** (every sweep): for each *active chat* (a live terminal session) it maps the
chat's worktree to a loom task and — unless the task was explicitly stopped — (re)starts any
of the task's dev services that probe as **down**. So a crashed server comes back and an open
chat's stack stays up, without fighting an explicit `stop`.

**Reaper** (startup + every `_REAP_INTERVAL`): the inverse — a stack whose chat is *done*
shouldn't keep running. It stops any task holding services that has no live terminal session
and whose chat is archived, or whose worktree has seen no chat activity for
`_REAP_IDLE_HOURS`. Archiving a chat also triggers an immediate targeted stop
(`stop_for_archived_chat`, called from `PATCH /chats`). Without this nothing ever stopped a
stack except the explicit stop button, and abandoned Django/Vite servers accumulated for
weeks — enough of them once exhausted RAM+swap into a machine-wide OOM.

Scope/intent:
  • Only **active chats** (live terminal sessions opened this loom run) are supervised.
  • A task you explicitly **stopped** (state `stopped`) is left alone — that's the opt-out.
    A *reaped* task also lands on `stopped`, so the supervisor won't resurrect it either;
    reopening its chat needs a manual start from the task card.
  • Services without a `health:` URL in `.loom.yaml` aren't probed (we won't blindly restart them).
  • Disable with `LOOM_SUPERVISE=0` / `LOOM_REAP=0`; tune with `LOOM_SUPERVISE_INTERVAL`,
    `LOOM_REAP_INTERVAL`, `LOOM_REAP_IDLE_HOURS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import datetime

from loom.core import manager, process, registry, terminals
from loom.core import sessions as sessions_mod
from loom.core.config import load_repo_config

_SUPERVISE = os.environ.get("LOOM_SUPERVISE", "1") == "1"
_INTERVAL = float(os.environ.get("LOOM_SUPERVISE_INTERVAL", "15"))  # seconds between sweeps
# Don't supervise these: `stopped` = you stopped it (respect it); error/archived/created have no
# usable, ready worktree to run a stack in.
_SKIP_STATES = {"stopped", "error", "archived", "created"}
# After (re)starting a task's services, leave it alone for a bit — a dev server takes time to come
# up, so probing it as "down" on the next sweep and restarting again just thrashes.
_RESTART_COOLDOWN = 60.0
_last_restart: dict[str, float] = {}
# A busy-but-ALIVE dev server is not down. Vite blocks the event loop during dependency
# optimization, and under system load anything can miss a 1s probe — with the old
# 1s-timeout/restart-on-first-miss policy the supervisor kill-looped healthy vites
# (thousands of restarts; each kill mid-optimization also orphaned a deps_temp_* dir in the
# vite cache — that's the 121 GB incident). So: generous probe timeout, and a service must
# miss _DOWN_STREAK consecutive sweeps (~45s at the default interval) before it's restarted.
# A genuinely crashed server refuses instantly and still comes back within ~1 min.
_PROBE_TIMEOUT = 5.0
_DOWN_STREAK = 3
_down_streaks: dict[tuple[str, str], int] = {}  # (task_id, service) -> consecutive failed probes

_REAP = os.environ.get("LOOM_REAP", "1") == "1"
_REAP_INTERVAL = float(os.environ.get("LOOM_REAP_INTERVAL", "600"))  # seconds between reap passes
_REAP_IDLE_S = float(os.environ.get("LOOM_REAP_IDLE_HOURS", "12")) * 3600.0


def _down_services(task, cfg) -> list[str]:
    """Configured services (that declare a `health:` URL) which aren't answering, in cfg order."""
    ctx = manager._ctx(cfg, task)
    down: list[str] = []
    for svc in cfg.services:
        if not svc.health:
            continue  # no probe → don't blindly (re)start it
        if not process.health_check(manager._render(svc.health, ctx), timeout=_PROBE_TIMEOUT):
            down.append(svc.name)
    return down


def _confirmed_down(task, cfg, down: list[str]) -> list[str]:
    """Update per-service miss streaks; return only services down _DOWN_STREAK sweeps in a row."""
    confirmed: list[str] = []
    for svc in cfg.services:
        if not svc.health:
            continue
        key = (task.id, svc.name)
        if svc.name in down:
            _down_streaks[key] = _down_streaks.get(key, 0) + 1
            if _down_streaks[key] >= _DOWN_STREAK:
                confirmed.append(svc.name)
        else:
            _down_streaks.pop(key, None)
    return confirmed


def _sweep() -> None:
    """One supervision pass (runs in a thread — blocking probes + process spawns)."""
    sessions = terminals.active_sessions()
    if not sessions:
        return
    cwds = {ts.cwd for ts in sessions if ts.cwd}
    by_path = {t.worktree_path: t for t in registry.list_tasks() if t.worktree_path}
    seen: set[str] = set()
    for cwd in cwds:
        task = by_path.get(cwd)
        if not task or task.id in seen:
            continue
        seen.add(task.id)
        if task.state.value in _SKIP_STATES:
            continue
        try:
            cfg = load_repo_config(task.repo_root)
        except Exception:  # noqa: BLE001 — missing/garbled .loom.yaml → skip this task
            continue
        down = _confirmed_down(task, cfg, _down_services(task, cfg))
        if not down:
            continue
        # Cooldown: give a just-(re)started service time to come up instead of restarting it again.
        if time.monotonic() - _last_restart.get(task.id, 0.0) < _RESTART_COOLDOWN:
            continue
        # Re-read state right before acting, to narrow the race with a concurrent user `stop`.
        fresh = registry.get_task(task.id)
        if not fresh or fresh.state.value in _SKIP_STATES:
            continue
        with contextlib.suppress(Exception):
            manager.start_task(cfg, task.id, only=set(down))
            _last_restart[task.id] = time.monotonic()
            for name in down:
                _down_streaks.pop((task.id, name), None)  # fresh start → fresh streak
            print(f"[loom supervisor] {task.id}: restarted {', '.join(down)}", flush=True)


# --- reaper -------------------------------------------------------------------
def _iso_ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(s).timestamp() if s else 0.0
    except ValueError:
        return 0.0


def _worktree_chat_meta() -> tuple[dict, dict[str, tuple[float, bool]]]:
    """(overlay, worktree_path → (last chat activity, latest chat archived?)) — the same
    "worktree's most-recent chat" view the dashboard's task list is grouped by."""
    ov = sessions_mod.overlay_all()
    latest: dict[str, tuple[float, bool]] = {}
    for s in sessions_mod.build_index():
        cwd = s.get("cwd")
        if not cwd:
            continue
        la = float(s.get("last_active") or 0.0)
        if cwd not in latest or la > latest[cwd][0]:
            latest[cwd] = (la, bool(ov.get(s["id"], {}).get("archived")))
    return ov, latest


def _reap_reason(task, ov: dict, latest: dict, live_cwds: set[str], now: float) -> str | None:
    """Why this task's stack should be stopped (None = leave it alone)."""
    if task.state.value != "running" and not task.services:
        return None  # nothing to stop
    if not task.worktree_path or task.worktree_path in live_cwds:
        return None  # an open terminal is using this worktree — the supervisor's domain
    la, latest_archived = latest.get(task.worktree_path, (0.0, False))
    # Linked chat archived (strict 1:1); legacy tasks fall back to the worktree's latest chat.
    archived = bool(ov.get(task.chat_id, {}).get("archived")) if task.chat_id else latest_archived
    if archived:
        return "chat archived"
    # Idle: no chat activity in the worktree and no task-record change either (updated_at is
    # stamped on every registry upsert, so a stack started before its first chat isn't "idle").
    ref = max(la, _iso_ts(task.updated_at))
    if now - ref >= _REAP_IDLE_S:
        return f"no activity for {(now - ref) / 3600:.0f}h"
    return None


def _reap_sweep() -> None:
    """One reap pass (runs in a thread — blocking kills + registry writes)."""
    now = time.time()
    live_cwds = {ts.cwd for ts in terminals.active_sessions() if ts.cwd}
    ov, latest = _worktree_chat_meta()
    for task in registry.list_tasks():
        reason = _reap_reason(task, ov, latest, live_cwds, now)
        if not reason:
            continue
        with contextlib.suppress(Exception):
            manager.stop_task(task.id)
            print(f"[loom reaper] {task.id}: stopped dev stack ({reason})", flush=True)


def stop_for_archived_chat(chat_id: str) -> str | None:
    """Immediate, targeted reap when a chat is archived (from `PATCH /chats`): an archived
    chat's task shouldn't keep a dev stack running. Skipped while a live terminal session
    still uses the worktree — the periodic reaper catches it once that session goes away.
    Returns the stopped task id, else None. Blocking — call off the event loop."""
    task = next((t for t in registry.list_tasks() if t.chat_id == chat_id), None)
    if task is None:
        # Legacy task not linked yet — match the chat's recorded cwd to a worktree.
        cwd = next((s.get("cwd") for s in sessions_mod.build_index() if s["id"] == chat_id), None)
        if cwd:
            task = next((t for t in registry.list_tasks() if t.worktree_path == cwd), None)
    if not task or (task.state.value != "running" and not task.services):
        return None
    if task.worktree_path and task.worktree_path in {ts.cwd for ts in terminals.active_sessions() if ts.cwd}:
        return None
    manager.stop_task(task.id)
    return task.id


async def _loop() -> None:
    loop = asyncio.get_running_loop()
    next_reap = 0.0  # monotonic; 0 → the FIRST tick reaps (cleans stacks leaked before this run)
    while True:
        if _SUPERVISE:
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, _sweep)  # keep blocking probes/spawns off the event loop
        if _REAP and time.monotonic() >= next_reap:
            next_reap = time.monotonic() + _REAP_INTERVAL
            with contextlib.suppress(Exception):
                await loop.run_in_executor(None, _reap_sweep)
        await asyncio.sleep(_INTERVAL)


def start() -> asyncio.Task | None:
    """Launch the supervisor+reaper loop as a background task (None if both are disabled)."""
    if not (_SUPERVISE or _REAP):
        return None
    return asyncio.create_task(_loop())
