"""Dev-stack supervisor for active chats.

Every sweep, for each *active chat* (a live terminal session) it maps the chat's worktree to a
loom task and — unless the task was explicitly stopped — (re)starts any of the task's dev services
that probe as **down**. So a crashed server comes back and an open chat's stack stays up, without
fighting an explicit `stop`.

Scope/intent:
  • Only **active chats** (live terminal sessions opened this loom run) are supervised.
  • A task you explicitly **stopped** (state `stopped`) is left alone — that's the opt-out.
  • Services without a `health:` URL in `.loom.yaml` aren't probed (we won't blindly restart them).
  • Disable entirely with `LOOM_SUPERVISE=0`; tune cadence with `LOOM_SUPERVISE_INTERVAL` (seconds).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

from loom.core import manager, process, registry, terminals
from loom.core.config import load_repo_config

_INTERVAL = float(os.environ.get("LOOM_SUPERVISE_INTERVAL", "15"))  # seconds between sweeps
# Don't supervise these: `stopped` = you stopped it (respect it); error/archived/created have no
# usable, ready worktree to run a stack in.
_SKIP_STATES = {"stopped", "error", "archived", "created"}
# After (re)starting a task's services, leave it alone for a bit — a dev server takes time to come
# up, so probing it as "down" on the next sweep and restarting again just thrashes.
_RESTART_COOLDOWN = 60.0
_last_restart: dict[str, float] = {}


def _down_services(task, cfg) -> list[str]:
    """Configured services (that declare a `health:` URL) which aren't answering, in cfg order."""
    ctx = manager._ctx(cfg, task)
    down: list[str] = []
    for svc in cfg.services:
        if not svc.health:
            continue  # no probe → don't blindly (re)start it
        if not process.health_check(manager._render(svc.health, ctx)):
            down.append(svc.name)
    return down


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
        down = _down_services(task, cfg)
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
            print(f"[loom supervisor] {task.id}: restarted {', '.join(down)}", flush=True)


async def _loop() -> None:
    loop = asyncio.get_running_loop()
    while True:
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, _sweep)  # keep blocking probes/spawns off the event loop
        await asyncio.sleep(_INTERVAL)


def start() -> asyncio.Task | None:
    """Launch the supervisor loop as a background task (None if disabled via LOOM_SUPERVISE=0)."""
    if os.environ.get("LOOM_SUPERVISE", "1") != "1":
        return None
    return asyncio.create_task(_loop())
