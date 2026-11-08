"""Per-worktree runtime context for agent sessions (Claude / Grok).

When a chat/terminal's cwd is inside a loom worktree, loom injects that task's
ports + log dir as environment variables (so the agent's Bash/curl target the
worktree's own dev stack, not a default dev port) and appends a short note to
the agent system prompt describing them. The terminal launcher (`terminals.py`)
uses this; it's deliberately project-agnostic — anything app-specific lives in
the repo's `.loom.yaml`, never here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loom.core import registry
from loom.core.config import LOGS_DIR, LOOM_API_HOST, LOOM_API_PORT

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


# --- harness (loom self-awareness) ---------------------------------------------
# Every loom chat is told it lives in loom and can drive loom's chats through the `loom` CLI
# (docs/DECISIONS.md D20). Kept short — it rides in every chat's system prompt; the CLI's own
# --help carries the detail, and `loom whoami` the current facts (category etc. change after
# launch, so they aren't baked in here).
HARNESS_NOTE = """<loom-harness>
You are running inside loom, a local orchestrator that runs many agent chats in parallel (each \
usually in its own git worktree, grouped into categories in loom's sidebar). This chat's loom id \
is $LOOM_CHAT_ID. When the user asks you to look at or manage their loom chats, use the `loom` CLI:
  loom whoami                                   this chat: project, category, branch, worktree
  loom chats ls [--category NAME | --uncategorized] [--tab archived|all] [--json]
  loom projects                                 the user's projects (repos) + chat counts
  loom chats show <id> [--tail N]  ·  loom chats brief <id>     read a chat / its status brief
  loom chats archive|unarchive|close <id...>    archive also stops the agent; close only stops it
  loom chats categorize <id...> <category>  ·  loom chats rename <id> <name>
  loom chats spawn --branch <b> --prompt <text> [--category C] [--model M] [--effort E]
                                                new worktree + chat, already working
  loom chats send <id> <text>                   type a message into another chat
  loom categories ls|add|rm|rename
The user has several projects (repos); `ls`, `spawn`, and `categories` default to THIS chat's \
project — pass `--project NAME`, or `-A` (all projects) to `ls` / `categories ls`, when the user \
means another project or "all my chats". Categories belong to a project. A spawned chat runs \
this chat's agent, model and effort unless the user asks for others (`--agent`, `--model` e.g. \
opus/sonnet/fable, `--effort` low|medium|high|xhigh|max). \
<id> is an id, unique id prefix, name, branch, or `self` (ids work across projects). Run \
`loom chats --help` for more. \
Only act on other chats when the user asks you to.
</loom-harness>"""


def harness_env() -> dict[str, str]:
    """Env that lets a chat reach its loom: the API URL (the server sets LOOM_API_URL at
    `serve`; fall back to the configured host/port) and loom's own bin dir on PATH so the
    `loom` console script resolves even when it isn't installed globally."""
    bin_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    return {
        "LOOM_API_URL": os.environ.get("LOOM_API_URL") or f"http://{LOOM_API_HOST}:{LOOM_API_PORT}",
        "PATH": path if bin_dir in path.split(os.pathsep) else f"{bin_dir}{os.pathsep}{path}",
    }


def agent_note(cwd: str | None) -> str:
    """The full system-prompt note for a loom chat: the worktree runtime note (if any) + the
    harness note."""
    _, runtime_note = _loom_runtime(cwd)
    return "\n\n".join(n for n in (runtime_note, HARNESS_NOTE) if n)
