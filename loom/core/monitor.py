"""What stops what loom starts: the dev-stack supervisor, the reaper, the spawn queue (D14, D34).

Loom starts an agent (under a pty daemon) and a dev stack for every chat. One idea decides when they
should run: a chat is **in use** while a browser tab shows it or its agent is mid-turn, and for
`IDLE_S` after its last sign of life (a turn event, a keystroke, a tab leaving it, a transcript write).

**Supervisor** (every sweep): for each chat in use, keep its task's *wanted* services up. It starts
the ones that aren't running at once, and restarts the ones that probe down a few sweeps in a row.
Wanted means every service with a `health:` URL, except that an agent-spawned chat gets no `frontend`
(Vite) unless someone asks for it: the dev-stack start button, or `loom dev up frontend`, which
lands in `task.requested`. A stack someone deliberately stopped (state `stopped`) is left alone.

**Reaper** (startup + every `_REAP_INTERVAL`):
  • an *idle* chat's stack is **parked**: stopped, state `ready` (not `stopped`), so the supervisor
    brings it back by itself the next time the chat is in use;
  • an agent-spawned chat's frontend that nobody asked for is stopped (once it isn't busy);
  • an *archived* chat's agent is stopped once it's quiet: no tab shows it, it isn't mid-turn, and
    nothing happened for `_ARCHIVED_QUIET_S`. That covers whatever started it again after the
    archive: an agent archiving itself (that archive can't stop its own turn), `loom chats
    send|ask`, a notification reply, or opening it from the archived tab. Its stack is parked and
    its ports released;
  • a duplicate pty daemon for one chat (possible before the daemon lock, pty_server.py) is stopped.

**Spawn queue** (every sweep): launches queued agent-spawned chats as slots free up (spawnq.py).

Before this, nothing stopped anything. Every chat whose agent was alive counted as active, so the
supervisor kept, and even restarted, stacks of chats idle for days, and the reaper skipped them. The
11-27 audit counted 22 backends of chats idle a day or more, 36 archived chats running (11 GB), 30
spawned perf chats each with its own Vite (12.7 GB) and 10 chats running two or three times.

  • Services without a `health:` URL in `.loom.yaml` are neither probed nor auto-started.
  • A service that stays down through `LOOM_SUPERVISE_MAX_RESTARTS` restarts (default 3) is torn
    down and given up on until you start it again — a broken boot isn't retried forever.
  • Disable with `LOOM_SUPERVISE=0` / `LOOM_REAP=0`; tune with `LOOM_SUPERVISE_INTERVAL`,
    `LOOM_REAP_INTERVAL`, `LOOM_IDLE_STOP_MIN`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loom.core import agents, events, manager, process, pty_server, registry, spawnq, terminals
from loom.core import sessions as sessions_mod
from loom.core.config import IDLE_STOP_MIN, load_repo_config

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
# A service that never comes up (e.g. an ImportError on boot) would otherwise be restarted
# forever. After _MAX_RESTARTS restarts with no healthy probe in between, the supervisor gives
# up: it tears the service down and leaves it alone until something else starts it again (the
# task card's start → it reappears in task.services). Reset by any healthy probe.
_MAX_RESTARTS = int(os.environ.get("LOOM_SUPERVISE_MAX_RESTARTS", "3"))
_restarts: dict[tuple[str, str], int] = {}  # (task_id, service) -> restarts since last healthy
_gave_up: set[tuple[str, str]] = set()

_REAP = os.environ.get("LOOM_REAP", "1") == "1"
_REAP_INTERVAL = float(os.environ.get("LOOM_REAP_INTERVAL", "60"))  # seconds between reap passes
IDLE_S = IDLE_STOP_MIN * 60.0
_ARCHIVED_QUIET_S = 120.0  # an archived chat's agent is stopped after this long with nothing happening
_GHOST_MIN_AGE_S = 60.0  # a daemon this young may still be starting up — never mistake it for a duplicate
_HOOKLESS_QUIET_S = 120.0  # an agent without lifecycle hooks (grok, codex) is working if it drew this recently
# Services an agent-spawned chat doesn't get unless someone asks: its agent works against the backend,
# and a Vite per chat was the single biggest cost of a spawn wave (~400 MB each).
ON_DEMAND = frozenset({"frontend"})


# --- is a chat in use? ----------------------------------------------------------
@dataclass
class _World:
    """One read of everything the sweeps decide on."""

    now: float
    ov: dict  # chat id → overlay
    turns: dict  # chat id → latest lifecycle event (events.turn_states)
    running: set[str]  # chats whose agent process is alive
    seen: dict[str, float]  # chat id → transcript last_active
    chat_by_cwd: dict[str, str]  # worktree → its most recent chat (legacy tasks with no chat_id)


def _world() -> _World:
    seen: dict[str, float] = {}
    best: dict[str, tuple[float, str]] = {}
    for s in sessions_mod.build_index():
        la = float(s.get("last_active") or 0.0)
        seen[s["id"]] = la
        cwd = s.get("cwd")
        if cwd and la >= best.get(cwd, (-1.0, ""))[0]:
            best[cwd] = (la, s["id"])
    return _World(
        now=time.time(), ov=sessions_mod.overlay_all(), turns=events.turn_states(),
        running=terminals.running_chat_ids(), seen=seen, chat_by_cwd={k: v[1] for k, v in best.items()},
    )


def _chat_of(task, w: _World) -> str | None:
    return task.chat_id or w.chat_by_cwd.get(task.worktree_path)


@dataclass
class Use:
    busy: bool  # in use this moment: a tab shows it, or its agent is mid-turn
    last: float  # epoch of its last sign of life (0 = none known)
    alive: bool  # its agent process is running


def chat_use(cid: str, w: _World) -> Use:
    alive = cid in w.running
    ov = w.ov.get(cid) or {}
    turn = w.turns.get(cid) or {}
    # opened_at = loom launched / attached it — covers a just-started agent before its first hook fires
    last = max(w.seen.get(cid, 0.0), float(turn.get("since") or 0.0), float(ov.get("opened_at") or 0.0))
    hooks = agents.normalize_agent(ov.get("agent")) == "claude"
    working = alive and turn.get("turn") == "running"
    act = terminals.activity(cid)
    if act:
        touched, drew = act
        # claude's status line can redraw on its own, so its output proves nothing — its hooks do
        last = max(last, touched if hooks else max(touched, drew))
        if not hooks:
            working = working or (alive and w.now - drew < _HOOKLESS_QUIET_S)
    return Use(busy=terminals.viewing(cid) or working, last=last, alive=alive)


def in_use(u: Use, now: float) -> bool:
    return u.busy or now - u.last < IDLE_S


def wanted_services(task, cfg, spawned: bool) -> list[str]:
    """The services the supervisor keeps up while the task's chat is in use (see the module doc)."""
    names = [s.name for s in cfg.services if s.health]
    if spawned:
        names = [n for n in names if n not in ON_DEMAND or n in task.requested]
    return names


# --- supervisor -----------------------------------------------------------------
def _down_services(task, cfg, names: list[str]) -> list[str]:
    """Which of `names` (services with a `health:` URL) aren't answering, in cfg order."""
    ctx = manager._ctx(cfg, task)
    return [
        svc.name for svc in cfg.services
        if svc.name in names and svc.health
        and not process.health_check(manager._render(svc.health, ctx), timeout=_PROBE_TIMEOUT)
    ]


def _confirmed_down(task, probed: list[str], down: list[str]) -> list[str]:
    """Update per-service miss streaks; return only services down _DOWN_STREAK sweeps in a row."""
    confirmed: list[str] = []
    for name in probed:
        key = (task.id, name)
        if name in down:
            _down_streaks[key] = _down_streaks.get(key, 0) + 1
            if _down_streaks[key] >= _DOWN_STREAK:
                confirmed.append(name)
        else:
            _down_streaks.pop(key, None)
            _restarts.pop(key, None)
    return confirmed


def _supervise(task, cfg, wanted: list[str]) -> None:
    running = {s.name for s in task.services}
    # A given-up service that's back in task.services was started by hand — supervise it afresh.
    for key in [k for k in _gave_up if k[0] == task.id and k[1] in running]:
        _gave_up.discard(key)
        _restarts.pop(key, None)
    # Not running at all (a parked stack, a fresh worktree) → start now; no streak to wait out.
    missing = [n for n in wanted if n not in running and (task.id, n) not in _gave_up]
    probed = [n for n in wanted if n in running]
    down = [n for n in _confirmed_down(task, probed, _down_services(task, cfg, probed)) if (task.id, n) not in _gave_up]
    if not missing and not down:
        return
    # Cooldown: give a just-(re)started service time to come up instead of restarting it again.
    if time.monotonic() - _last_restart.get(task.id, 0.0) < _RESTART_COOLDOWN:
        return
    # Re-read state right before acting, to narrow the race with a concurrent user `stop`.
    fresh = registry.get_task(task.id)
    if not fresh or fresh.state.value in _SKIP_STATES:
        return
    exhausted = {n for n in down if _restarts.get((task.id, n), 0) >= _MAX_RESTARTS}
    if exhausted:
        with contextlib.suppress(Exception):
            manager.stop_services(task.id, exhausted)
        for name in exhausted:
            _gave_up.add((task.id, name))
            _down_streaks.pop((task.id, name), None)
        print(
            f"[loom supervisor] {task.id}: gave up on {', '.join(sorted(exhausted))} after "
            f"{_MAX_RESTARTS} failed restarts — check its log, then start it from the task card",
            flush=True,
        )
        down = [n for n in down if n not in exhausted]
    if not missing and not down:
        return
    for name in down:  # count the attempt even if start_task raises — that's a failure too
        _restarts[(task.id, name)] = _restarts.get((task.id, name), 0) + 1
    with contextlib.suppress(Exception):
        manager.start_task(cfg, task.id, only=set(missing) | set(down))
        _last_restart[task.id] = time.monotonic()
        for name in down:
            _down_streaks.pop((task.id, name), None)  # fresh start → fresh streak
        what = [*(f"started {n}" for n in missing), *(f"restarted {n}" for n in down)]
        print(f"[loom supervisor] {task.id}: {', '.join(what)}", flush=True)


def _sweep() -> None:
    """One supervision pass (runs in a thread — blocking probes + process spawns)."""
    w = _world()
    for task in registry.list_tasks():
        if task.state.value in _SKIP_STATES or not task.worktree_path:
            continue
        cid = _chat_of(task, w)
        ov = w.ov.get(cid, {}) if cid else {}
        if not cid or ov.get("archived") or not in_use(chat_use(cid, w), w.now):
            continue
        try:
            cfg = load_repo_config(task.repo_root)
        except Exception:  # noqa: BLE001 — missing/garbled .loom.yaml → skip this task
            continue
        _supervise(task, cfg, wanted_services(task, cfg, bool(ov.get("spawned_by"))))


# --- reaper ---------------------------------------------------------------------
def _iso_ts(s: str | None) -> float:
    try:
        return datetime.fromisoformat(s).timestamp() if s else 0.0
    except ValueError:
        return 0.0


@dataclass
class _Plan:
    stop_agents: list[tuple[str, str]]  # (chat id, why)
    park: list[tuple[str, str]]  # (task id, why)
    trim: list[tuple[str, set[str]]]  # (task id, services) — a spawned chat's frontend nobody asked for
    release: list[str]  # task ids whose port offset goes back to the pool


def _archived_quiet(u: Use, now: float) -> bool:
    return not u.busy and now - u.last >= _ARCHIVED_QUIET_S


def _reap_plan() -> _Plan:
    """Decide what the reap pass stops (blocking reads only — `_reap_pass` acts on it)."""
    w = _world()
    plan = _Plan([], [], [], [])
    task_chats: set[str] = set()
    for task in registry.list_tasks():
        cid = _chat_of(task, w)
        ov = w.ov.get(cid, {}) if cid else {}
        u = chat_use(cid, w) if cid else Use(False, 0.0, False)
        if cid:
            task_chats.add(cid)
        has_stack = bool(task.services) or task.state.value == "running"
        if ov.get("archived"):
            if u.alive and not _archived_quiet(u, w.now):
                continue  # someone's still using it (a tab, a turn) — the next pass looks again
            if u.alive:
                plan.stop_agents.append((cid, "chat archived"))
            if has_stack:
                plan.park.append((task.id, "chat archived"))
            if task.ports:
                plan.release.append(task.id)
            continue
        if not has_stack:
            continue
        ref = max(u.last, task.stack_started_at or _iso_ts(task.updated_at))
        if not u.busy and w.now - ref >= IDLE_S:
            plan.park.append((task.id, f"nothing happened in its chat for {_ago(w.now - ref)}"))
            continue
        if ov.get("spawned_by") and not u.busy:  # never pull a server out from under a turn in progress
            extra = {s.name for s in task.services if s.name in ON_DEMAND and s.name not in task.requested}
            if extra:
                plan.trim.append((task.id, extra))
    # Archived chats with no worktree task run agents too.
    for cid in w.running - task_chats:
        if (w.ov.get(cid) or {}).get("archived") and _archived_quiet(chat_use(cid, w), w.now):
            plan.stop_agents.append((cid, "chat archived"))
    return plan


def _ago(secs: float) -> str:
    return f"{secs / 60:.0f} min" if secs < 2 * 3600 else f"{secs / 3600:.1f} h" if secs < 48 * 3600 else f"{secs / 86400:.0f} days"


def _apply(plan: _Plan) -> None:
    for tid, why in plan.park:
        cur = registry.get_task(tid)
        if not cur or (not cur.services and cur.state.value != "running"):
            continue  # already stopped meanwhile (the archive follow-up got there first)
        with contextlib.suppress(Exception):
            manager.park_task(tid)
            then = " — it restarts when the chat is used" if why != "chat archived" else ""
            print(f"[loom reaper] {tid}: stopped dev stack ({why}){then}", flush=True)
    for tid, names in plan.trim:
        with contextlib.suppress(Exception):
            manager.stop_services(tid, names)
            print(f"[loom reaper] {tid}: stopped {', '.join(sorted(names))} (agent-spawned chat; nobody asked for it)", flush=True)
    for tid in plan.release:
        with contextlib.suppress(Exception):
            manager.release_ports(tid)


def _reap_ghosts() -> None:
    """Stop extra pty daemons for one chat. A daemon holds its socket's lock for life, so this can
    only find copies started before the lock existed (or ones whose socket someone deleted): every
    daemon for a socket except the one its pidfile names, and — once the chat isn't mid-turn —
    daemons nobody can reach at all (their socket file is gone; the next open would start yet
    another copy)."""
    turns = None
    for sock, procs in pty_server.find_daemons(str(terminals._SOCK_DIR)).items():
        owner = pty_server.read_pid(sock) if os.path.exists(sock) else None
        pids = {p for p, _ in procs}
        extra = [(p, age) for p, age in procs if p != owner and age >= _GHOST_MIN_AGE_S]
        if not extra:
            continue
        cid = Path(sock).name.removeprefix("loomx-").removesuffix(".sock")
        if owner not in pids:  # no reachable copy: don't cut an in-flight turn short — wait it out
            turns = turns if turns is not None else events.turn_states()
            if (turns.get(cid) or {}).get("turn") == "running":
                continue
        pty_server.kill_daemons([p for p, _ in extra])
        why = "a duplicate of the running one" if owner in pids else "unreachable — its socket is gone"
        print(f"[loom reaper] {cid[:8]}: stopped {len(extra)} extra agent process(es) ({why})", flush=True)


async def _reap_pass(loop: asyncio.AbstractEventLoop) -> None:
    plan = await loop.run_in_executor(None, _reap_plan)
    for cid, why in plan.stop_agents:
        if terminals.viewing(cid):
            continue  # a tab opened it since the plan was made
        try:
            if await terminals.close_chat(cid):
                print(f"[loom reaper] {cid[:8]}: stopped agent ({why})", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[loom reaper] {cid[:8]}: stopping agent failed: {e!r}", flush=True)
    await loop.run_in_executor(None, _apply, plan)
    await loop.run_in_executor(None, _reap_ghosts)


def stop_for_archived_chat(chat_id: str) -> str | None:
    """Immediate, targeted reap when a chat is archived (from `PATCH /chats`, after its agent was
    stopped): park its task's dev stack. Skipped while a live terminal session still uses the
    worktree (an agent that archived itself is finishing its turn) — the reaper parks it once that
    agent is stopped. Returns the parked task id, else None. Blocking — call off the event loop."""
    task = manager.task_for_chat(chat_id)
    if not task or (task.state.value != "running" and not task.services):
        return None
    if task.worktree_path and task.worktree_path in {ts.cwd for ts in terminals.active_sessions() if ts.cwd}:
        return None
    manager.park_task(task.id)
    return task.id


async def _loop() -> None:
    loop = asyncio.get_running_loop()
    next_reap = 0.0  # monotonic; 0 → the FIRST tick reaps (cleans up what leaked before this run)
    while True:
        if _SUPERVISE:
            try:
                await loop.run_in_executor(None, _sweep)  # keep blocking probes/spawns off the event loop
            except Exception as e:  # noqa: BLE001
                print(f"[loom supervisor] sweep failed: {e!r}", flush=True)
        try:
            await spawnq.launch_due()
        except Exception as e:  # noqa: BLE001
            print(f"[loom spawn-queue] drain failed: {e!r}", flush=True)
        if _REAP and time.monotonic() >= next_reap:
            next_reap = time.monotonic() + _REAP_INTERVAL
            try:
                await _reap_pass(loop)
            except Exception as e:  # noqa: BLE001
                print(f"[loom reaper] pass failed: {e!r}", flush=True)
        await asyncio.sleep(_INTERVAL)


def start() -> asyncio.Task:
    """Launch the background loop. Always runs — even with supervision and reaping off, queued
    spawns still have to launch."""
    return asyncio.create_task(_loop())
