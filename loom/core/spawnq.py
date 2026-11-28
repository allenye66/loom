"""Spawn queue: at most `MAX_WORKING` agent-spawned chats per spawner are working at once (D34).

`loom chats spawn` used to launch every chat it was asked for on the spot. One orchestrator turn
fanned out 30 perf-fix chats, each with its own agent and dev stack, on a 48 GB machine. Now a
spawner gets `MAX_WORKING` children *working* at a time (mid-turn, or just launched). A spawn past
that still creates the worktree and the chat right away (the caller gets its id, branch and
sidebar row), but its handoff prompt waits in the chat's overlay (`queued_at` / `queued_prompt`).
The monitor loop launches queued chats oldest-first as the spawner's earlier children finish their
turns. Opening a queued chat, or `loom chats send` to it, launches it at once, on its handoff (see
`terminals.open_terminal`). An archived queued chat waits until it's restored.
"""

from __future__ import annotations

import asyncio
import os
import time

from loom.core import agents, events, manager, terminals
from loom.core import sessions as sessions_mod

MAX_WORKING = max(1, int(os.environ.get("LOOM_SPAWN_MAX_WORKING", "4")))
# A just-launched child counts as working until its own first turn event says otherwise (claude
# fires SessionStart + UserPromptSubmit within a second of a spawn, so this only covers a slow boot).
_BOOT_GRACE_S = 180.0
_QUIET_S = 120.0  # an agent without lifecycle hooks (grok, codex) is working if it drew this recently

launch_lock = asyncio.Lock()  # a spawn's slot check + launch, and the queue drain, one at a time
# chat id → (failed launches in a row, monotonic time before which it isn't retried)
_failures: dict[str, tuple[int, float]] = {}


def _working(cid: str, ov: dict, turns: dict, running: set[str], now: float) -> bool:
    """Is this chat's agent working right now (not idle, not waiting on someone)?"""
    if cid not in running:
        return False
    opened = float(ov.get("opened_at") or 0.0)
    if agents.normalize_agent(ov.get("agent")) == "claude":
        t = turns.get(cid)
        if t and float(t.get("since") or 0.0) >= opened - 1.0:  # an event from this launch
            return t.get("turn") == "running"
        return now - opened < _BOOT_GRACE_S
    act = terminals.activity(cid)
    return act is None or now - act[1] < _QUIET_S  # alive but unattached → can't tell → count it


def working_children(parent: str, *, ov_all: dict | None = None, running: set[str] | None = None) -> list[str]:
    """The spawner's children that are working right now. Blocking — call off the event loop."""
    ov_all = ov_all if ov_all is not None else sessions_mod.overlay_all()
    running = running if running is not None else terminals.running_chat_ids()
    turns = events.turn_states()
    now = time.time()
    return [
        cid for cid, ov in ov_all.items()
        if ov.get("spawned_by") == parent and not ov.get("archived") and not ov.get("queued_at")
        and _working(cid, ov, turns, running, now)
    ]


def queued(ov_all: dict | None = None) -> list[tuple[str, dict]]:
    """(chat id, overlay) of every chat still waiting for a slot, oldest first."""
    ov_all = ov_all if ov_all is not None else sessions_mod.overlay_all()
    rows = [(cid, ov) for cid, ov in ov_all.items() if ov.get("queued_at")]
    return sorted(rows, key=lambda r: float(r[1]["queued_at"]))


def enqueue(chat_id: str, prompt: str | None) -> int:
    """Park a spawned chat's handoff until its spawner has a free slot. Returns its place in that
    spawner's queue (1 = next)."""
    ov = sessions_mod.set_overlay(chat_id, {"queued_at": time.time(), "queued_prompt": prompt or ""})
    parent = ov.get("spawned_by")
    return sum(1 for _, o in queued() if o.get("spawned_by") == parent)


def _due() -> list[str]:
    """Queued chats whose spawner has a free slot now, in launch order. Blocking."""
    ov_all = sessions_mod.overlay_all()
    waiting = [(cid, ov) for cid, ov in queued(ov_all) if not ov.get("archived") and not ov.get("deleted")]
    if not waiting:
        return []
    running = terminals.running_chat_ids()
    free: dict[str, int] = {}
    out: list[str] = []
    for cid, ov in waiting:
        parent = ov.get("spawned_by") or ""
        if parent not in free:
            free[parent] = MAX_WORKING - len(working_children(parent, ov_all=ov_all, running=running))
        if free[parent] > 0:
            free[parent] -= 1
            out.append(cid)
    return out


async def launch_due() -> list[str]:
    """Launch every queued chat that has a slot (the monitor loop calls this each tick). A chat whose
    worktree is gone is dropped from the queue."""
    loop = asyncio.get_running_loop()
    launched: list[str] = []
    async with launch_lock:
        for cid in await loop.run_in_executor(None, _due):
            fails, until = _failures.get(cid, (0, 0.0))
            if time.monotonic() < until:
                continue
            task = await loop.run_in_executor(None, manager.task_for_chat, cid)
            if task is None or not task.worktree_path:
                await loop.run_in_executor(
                    None, sessions_mod.set_overlay, cid, {"queued_at": None, "queued_prompt": None},
                )
                continue
            try:
                await terminals.start_chat(cid, task.worktree_path)  # open_terminal starts it on its handoff
            except Exception as e:  # noqa: BLE001 — stays queued; retried with a growing pause (15 s … 30 min)
                _failures[cid] = (fails + 1, time.monotonic() + min(15.0 * 2 ** fails, 1800.0))
                print(f"[loom spawn-queue] {cid[:8]}: launch failed ({fails + 1}× in a row): {e!r}", flush=True)
                continue
            _failures.pop(cid, None)
            launched.append(cid)
            print(f"[loom spawn-queue] {cid[:8]}: launched (a slot freed up)", flush=True)
    return launched
