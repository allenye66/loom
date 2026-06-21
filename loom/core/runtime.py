"""Per-worktree runtime context for Claude sessions.

When a chat/terminal's cwd is inside a loom worktree, loom injects that task's
ports + log dir as environment variables (so the agent's Bash/curl target the
worktree's own dev stack, not a default dev port) and appends a short note to
Claude Code's system prompt describing them. Both the terminal launcher
(`terminals.py`) use this; it's deliberately project-agnostic — anything
app-specific lives in the repo's `.loom.yaml`, never here. The terminal
launcher (`terminals.py`) uses this to build each session's environment.
"""

from __future__ import annotations

from pathlib import Path

from loom.core import registry
from loom.core.config import LOGS_DIR

# --- resilience ---------------------------------------------------------------
# Merged into every session's env. Turns on Claude Code's *native* recovery that
# loom otherwise leaves at defaults — the stream watchdog (a stalled stream becomes
# a retryable error instead of an infinite hang) plus TCP-keepalive workarounds for
# the "socket connection was closed unexpectedly" drop (anthropics/claude-code#60133).
# Unknown vars are ignored by CLI versions that don't support them, so this is safe.
_RESILIENCE_ENV: dict[str, str] = {
    "CLAUDE_CODE_MAX_RETRIES": "10",               # native retry of 5xx/429/529/timeout (default; pinned)
    "CLAUDE_ENABLE_STREAM_WATCHDOG": "1",          # abort a stream whose body stalls mid-flight
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "300000",     # ...after 5 min idle (documented minimum)
    "CLAUDE_CODE_REMOTE_SEND_KEEPALIVES": "true",  # experimental keepalive (issue #60133)
    "BUN_CONFIG_HTTP_IDLE_TIMEOUT": "300",         # experimental keepalive (issue #60133)
}


def _loom_runtime(cwd: str | None) -> tuple[dict[str, str], str | None]:
    """If `cwd` is inside a loom worktree, return (env, system-prompt note) telling
    the agent its task's ports/logs so it targets the worktree's stack, not a default
    dev port. Returns ({}, None) for a plain (non-worktree) session — never raises, so
    a missing/garbled registry just yields a plain session.
    """
    if not cwd:
        return {}, None
    try:
        target = Path(cwd).expanduser().resolve()
        task = next(
            (
                t
                for t in registry.list_tasks()
                if t.worktree_path
                and t.ports
                and (target == (wt := Path(t.worktree_path).expanduser().resolve()) or wt in target.parents)
            ),
            None,
        )
        if task is None or not task.ports:
            return {}, None
        be, fe = task.ports.backend, task.ports.frontend
        env = {
            "LOOM_TASK": task.id,
            "LOOM_WORKTREE": task.worktree_path,
            "LOOM_BACKEND_PORT": str(be),
            "LOOM_BACKEND_URL": f"http://localhost:{be}",
            "LOOM_FRONTEND_PORT": str(fe),
            "LOOM_FRONTEND_URL": f"http://localhost:{fe}",
            "LOOM_LOG_DIR": str(LOGS_DIR / task.id),
        }
        note = (
            f"<loom-runtime>\n"
            f"You're in loom worktree task `{task.id}` (branch `{task.branch}`). Its dev stack, when "
            f"running, is backend $LOOM_BACKEND_URL (http://localhost:{be}) and frontend $LOOM_FRONTEND_URL "
            f"(http://localhost:{fe}) — use THESE worktree-specific URLs, not any default dev ports. "
            f"This worktree's server logs are under $LOOM_LOG_DIR. Start/stop the stack from the loom "
            f"task card.\n"
            f"</loom-runtime>"
        )
        return env, note
    except Exception:  # noqa: BLE001 — never let runtime lookup break a session
        return {}, None
