"""Agent adapters — which CLI powers a loom terminal session (claude | grok | codex).

The PTY/tmux hosts are CLI-agnostic; only argv construction, transcript discovery,
and a few env knobs differ. Each chat locks an `agent` in the sessions overlay on
first open (or at task create) and never switches under a live process.

Codex is the odd one out: it mints its own session id at launch (no --session-id
flag), so a loom chat id can't double as the codex session id. The chat's overlay
instead stores the discovered native id under `agent_session_id` (bound post-launch
by terminals.py via the notify hook / a rollout cwd-scan) and resume goes through
`codex resume <that id>`.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from loom.core.config import LOOM_HOME
from loom.core.runtime import _loom_runtime

AgentId = Literal["claude", "grok", "codex"]
AGENTS: tuple[AgentId, ...] = ("claude", "grok", "codex")
DEFAULT_AGENT: AgentId = "claude"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
# Codex rollout transcripts: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl,
# first line = session_meta (payload carries id + cwd). session_index.jsonl maps
# id → auto-generated thread_name. Verified against codex-cli 0.160.0.
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CODEX_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
NEEDS_DIR = LOOM_HOME / "needs"
CODEX_NOTIFY_DIR = LOOM_HOME / "codex-notify"


def normalize_agent(value: str | None) -> AgentId:
    """Coerce a free-form string to a known agent; unknown/empty → claude."""
    if value and value.strip().lower() in AGENTS:
        return value.strip().lower()  # type: ignore[return-value]
    return DEFAULT_AGENT


def binary(agent: AgentId) -> str:
    name = agent if agent in AGENTS else DEFAULT_AGENT
    return shutil.which(name) or name


def available(agent: AgentId) -> bool:
    return shutil.which(agent if agent in AGENTS else DEFAULT_AGENT) is not None


def _marker_path(chat_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in chat_id)[:80] or "x"
    return NEEDS_DIR / safe


def _claude_settings(chat_id: str, *, fullscreen: bool) -> dict:
    """Per-session Claude settings (via --settings): theme, needs-you hooks, optional fullscreen."""
    NEEDS_DIR.mkdir(parents=True, exist_ok=True)
    mark = shlex.quote(str(_marker_path(chat_id)))
    settings: dict = {
        "theme": "dark",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": f"touch {mark}"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": f"touch {mark}"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"rm -f {mark}"}]}],
        },
    }
    if fullscreen:
        settings["tui"] = "fullscreen"
    return settings


def ensure_grok_needs_hook() -> None:
    """Install a global Grok hook that only fires when LOOM_NEEDS_MARK is set in the env.

    Grok has no per-session --settings flag (unlike Claude), so loom drops a small
    guarded hook into ~/.grok/hooks/. Commands are no-ops unless the session was
    launched by loom (which exports LOOM_NEEDS_MARK).
    """
    hooks_dir = Path.home() / ".grok" / "hooks"
    path = hooks_dir / "zz-loom-needs.json"
    body = {
        "hooks": {
            "Stop": [{
                "hooks": [{
                    "type": "command",
                    "command": 'if [ -n "${LOOM_NEEDS_MARK:-}" ]; then touch "$LOOM_NEEDS_MARK"; fi',
                }],
            }],
            "Notification": [{
                "hooks": [{
                    "type": "command",
                    "command": 'if [ -n "${LOOM_NEEDS_MARK:-}" ]; then touch "$LOOM_NEEDS_MARK"; fi',
                }],
            }],
            "UserPromptSubmit": [{
                "hooks": [{
                    "type": "command",
                    "command": 'if [ -n "${LOOM_NEEDS_MARK:-}" ]; then rm -f "$LOOM_NEEDS_MARK"; fi',
                }],
            }],
        },
    }
    text = json.dumps(body, indent=2) + "\n"
    try:
        if path.exists() and path.read_text() == text:
            return
        hooks_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError:
        pass  # best-effort — needs-you is non-critical


# --- codex ---------------------------------------------------------------------
_CODEX_NOTIFY_SCRIPT = """#!/bin/sh
# loom-managed — rewritten on codex launch; do not edit (see loom/core/agents.py).
# codex `notify` hook: $1 = notification JSON (agent-turn-complete /
# approval-requested / async-question / ...). Env-guarded no-ops outside loom.
if [ -n "${LOOM_NEEDS_MARK:-}" ]; then touch "$LOOM_NEEDS_MARK"; fi
if [ -n "${LOOM_CODEX_NOTIFY_FILE:-}" ]; then printf '%s' "$1" > "$LOOM_CODEX_NOTIFY_FILE" 2>/dev/null; fi
"""


def ensure_codex_notify_script() -> Path | None:
    """Install loom's codex notify hook (passed per-session via `-c notify=[...]`).

    Every notify event is a "waiting on you" moment (turn complete, approval,
    question) → touch the needs marker, claude-Stop-style. The hook also dumps the
    notification JSON (which carries `thread-id`) to a per-chat file — the exact
    binding channel for the codex session id (see codex_notify_binding)."""
    path = LOOM_HOME / "codex-notify.sh"
    try:
        if not path.exists() or path.read_text() != _CODEX_NOTIFY_SCRIPT:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_CODEX_NOTIFY_SCRIPT)
        path.chmod(0o755)
        return path
    except OSError:
        return None  # best-effort — needs-you/binding degrade, the session still works


def _notify_file(chat_id: str) -> Path:
    """Where the notify hook drops this chat's latest notification JSON."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in chat_id)[:80] or "x"
    return CODEX_NOTIFY_DIR / f"{safe}.json"


def codex_notify_binding(chat_id: str) -> str | None:
    """Native codex session id from the chat's notify-file JSON (exact, no heuristics).
    Only lands after codex fires its first notification (≈ end of the first turn)."""
    try:
        data = json.loads(_notify_file(chat_id).read_text())
    except (OSError, ValueError):
        return None
    sid = data.get("thread-id") or data.get("session-id") or data.get("thread_id")
    return str(sid) if sid else None


def find_codex_rollout(session_id: str) -> Path | None:
    """Rollout jsonl for a codex session id (the filename ends with the id)."""
    if not session_id or not CODEX_SESSIONS.exists():
        return None
    try:
        return next(iter(CODEX_SESSIONS.glob(f"*/*/*/rollout-*-{session_id}.jsonl")), None)
    except OSError:
        return None


def codex_rollout_meta(path: Path) -> dict | None:
    """The session_meta payload (id, cwd, timestamp, …) from a rollout's first line.
    None while codex hasn't flushed it yet (it lands around the first prompt)."""
    try:
        with path.open("r", errors="replace") as f:
            line = f.readline().strip()
        rec = json.loads(line) if line else None
    except (OSError, ValueError):
        return None
    if isinstance(rec, dict) and rec.get("type") == "session_meta":
        payload = rec.get("payload")
        return payload if isinstance(payload, dict) else None
    return None


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()
    except OSError:
        return a.rstrip("/") == b.rstrip("/")


def discover_codex_session(cwd: str, claimed: set[str], after_ts: float | None = None) -> str | None:
    """Heuristic binding: the newest unclaimed rollout whose session_meta cwd is `cwd`.

    Sound because worktree↔chat is strict 1:1 in loom — a codex rollout in this
    task's worktree belongs to this chat unless some other chat already claimed it.
    `after_ts` (file-mtime floor) narrows a fresh launch's scan to rollouts being
    written *now*, so a brand-new chat can't grab a stale thread; the recovery path
    (lost binding, resume after restart) passes None and takes the newest."""
    if not cwd or not CODEX_SESSIONS.exists():
        return None
    best: tuple[float, str] | None = None
    try:
        for f in CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if after_ts is not None and mtime < after_ts - 5.0:
                continue
            if best is not None and mtime <= best[0]:
                continue
            meta = codex_rollout_meta(f)
            if not meta:
                continue
            sid = str(meta.get("id") or meta.get("session_id") or "")
            if not sid or sid in claimed:
                continue
            if _same_dir(meta.get("cwd"), cwd):
                best = (mtime, sid)
    except OSError:
        return None
    return best[1] if best else None


def _encode_cwd(cwd: str) -> str:
    """Grok groups sessions by URL-encoded absolute cwd (empty safe set → %2F… style)."""
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except OSError:
        resolved = cwd
    return quote(resolved, safe="")


def find_claude_transcript(chat_id: str) -> Path | None:
    if not CLAUDE_PROJECTS.exists():
        return None
    for proj in CLAUDE_PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        f = proj / f"{chat_id}.jsonl"
        if f.exists():
            return f
    return None


def find_grok_session(chat_id: str, cwd: str | None = None) -> Path | None:
    """Return the session directory under ~/.grok/sessions if it exists."""
    if not GROK_SESSIONS.exists():
        return None
    if cwd:
        group = GROK_SESSIONS / _encode_cwd(cwd)
        sess = group / chat_id
        if sess.is_dir() and (sess / "summary.json").exists():
            return sess
        # trailing-slash variants show up depending on how cwd was recorded
        try:
            resolved = str(Path(cwd).expanduser().resolve())
        except OSError:
            resolved = cwd
        for variant in (resolved.rstrip("/") + "/", resolved.rstrip("/")):
            alt = GROK_SESSIONS / quote(variant, safe="") / chat_id
            if alt.is_dir() and (alt / "summary.json").exists():
                return alt
    # Fallback: scan all cwd groups (resume after path moves, or cwd unknown).
    try:
        for group in GROK_SESSIONS.iterdir():
            if not group.is_dir() or group.name.startswith("."):
                continue
            sess = group / chat_id
            if sess.is_dir() and (sess / "summary.json").exists():
                return sess
    except OSError:
        pass
    return None


def session_exists(agent: AgentId, chat_id: str, cwd: str | None = None) -> bool:
    if agent == "grok":
        return find_grok_session(chat_id, cwd) is not None
    if agent == "codex":
        return find_codex_rollout(chat_id) is not None
    return find_claude_transcript(chat_id) is not None


def build_argv(
    agent: AgentId,
    chat_id: str,
    cwd: str | None,
    *,
    fullscreen: bool,
    agent_session_id: str | None = None,
) -> list[str]:
    """CLI argv for a new or resumed session under the given agent.

    `agent_session_id` is the agent-native session id when it differs from the loom
    chat id — only codex today (codex mints its own id; terminals.py binds it into
    the overlay post-launch and passes it here for resume)."""
    _, note = _loom_runtime(cwd)
    exists = session_exists(agent, agent_session_id or chat_id, cwd)

    if agent == "codex":
        sid = agent_session_id or chat_id
        argv = [binary("codex")]
        if exists:
            # Resume the bound native session. There is no --session-id for NEW
            # sessions — codex picks the uuid; loom discovers + binds it after launch.
            argv += ["resume", sid]
        argv += [
            # loom's `--effort max` analogue (config key verified in 0.160).
            "-c", "model_reasoning_effort=\"xhigh\"",
            # Parity with claude's bypassPermissions (loom chats live in disposable
            # worktrees). Soften to ["-s", "workspace-write", "-a", "on-request"]
            # if codex should sandbox + ask instead.
            "--dangerously-bypass-approvals-and-sandbox",
            "--search",
        ]
        script = ensure_codex_notify_script()
        if script:
            # json.dumps output is valid TOML here (basic string array) — the value
            # part of -c is parsed as TOML.
            argv += ["-c", f"notify={json.dumps([str(script)])}"]
        if not fullscreen:
            # pty host wants native scrollback (inline renderer); tmux keeps the
            # default alt-screen fullscreen.
            argv += ["--no-alt-screen"]
        if note:
            # Best-effort injection of the per-worktree runtime note; the LOOM_* env
            # vars carry the same facts regardless.
            argv += ["-c", f"developer_instructions={json.dumps(note)}"]
        return argv

    if agent == "grok":
        ensure_grok_needs_hook()
        argv = [
            binary("grok"),
            "--effort", "max",
            "--permission-mode", "acceptEdits",
        ]
        # pty host wants native scrollback (no alt-screen); tmux keeps default fullscreen.
        if not fullscreen:
            argv += ["--no-alt-screen", "--minimal"]
        if note:
            argv += ["--rules", note]
        if exists:
            argv += ["--resume", chat_id]
        else:
            argv += ["--session-id", chat_id]
        return argv

    # --- claude ---
    settings = _claude_settings(chat_id, fullscreen=fullscreen)
    argv = [
        binary("claude"),
        "--effort", "max",
        "--permission-mode", "bypassPermissions",
        "--settings", json.dumps(settings),
    ]
    if note:
        argv += ["--append-system-prompt", note]
    if exists:
        argv += ["--resume", chat_id]
    else:
        argv += ["--session-id", chat_id]
    return argv


def child_env(agent: AgentId, chat_id: str, base: dict[str, str]) -> dict[str, str]:
    """Env extras for the child process (on top of os.environ + loom runtime)."""
    env = dict(base)
    env["TERM"] = env.get("TERM") or "xterm-256color"
    env["LOOM_AGENT"] = agent
    env["LOOM_CHAT_ID"] = chat_id
    if agent == "grok":
        NEEDS_DIR.mkdir(parents=True, exist_ok=True)
        env["LOOM_NEEDS_MARK"] = str(_marker_path(chat_id))
        # Avoid inheriting a nested Claude auto-approve context if loom was started
        # from inside Claude Code (harmless for grok, keeps parity with claude launch).
    if agent == "codex":
        # Both read by the env-guarded notify hook (ensure_codex_notify_script):
        # the marker drives the sidebar's "needs you" dot; the notify file carries
        # the thread-id that binds the native session id to this chat.
        NEEDS_DIR.mkdir(parents=True, exist_ok=True)
        CODEX_NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
        env["LOOM_NEEDS_MARK"] = str(_marker_path(chat_id))
        env["LOOM_CODEX_NOTIFY_FILE"] = str(_notify_file(chat_id))
    return env


def label(agent: AgentId) -> str:
    return {"grok": "Grok", "codex": "Codex"}.get(agent, "Claude")
