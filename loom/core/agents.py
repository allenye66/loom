"""Agent adapters — which CLI powers a loom terminal session (claude | grok).

The PTY/tmux hosts are CLI-agnostic; only argv construction, transcript discovery,
and a few env knobs differ. Each chat locks an `agent` in the sessions overlay on
first open (or at task create) and never switches under a live process.
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

AgentId = Literal["claude", "grok"]
AGENTS: tuple[AgentId, ...] = ("claude", "grok")
DEFAULT_AGENT: AgentId = "claude"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
GROK_SESSIONS = Path.home() / ".grok" / "sessions"
NEEDS_DIR = LOOM_HOME / "needs"


def normalize_agent(value: str | None) -> AgentId:
    """Coerce a free-form string to a known agent; unknown/empty → claude."""
    if value and value.strip().lower() in AGENTS:
        return value.strip().lower()  # type: ignore[return-value]
    return DEFAULT_AGENT


def binary(agent: AgentId) -> str:
    if agent == "grok":
        return shutil.which("grok") or "grok"
    return shutil.which("claude") or "claude"


def available(agent: AgentId) -> bool:
    name = "grok" if agent == "grok" else "claude"
    return shutil.which(name) is not None


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
    return find_claude_transcript(chat_id) is not None


def build_argv(agent: AgentId, chat_id: str, cwd: str | None, *, fullscreen: bool) -> list[str]:
    """CLI argv for a new or resumed session under the given agent."""
    _, note = _loom_runtime(cwd)
    exists = session_exists(agent, chat_id, cwd)

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
        "--permission-mode", "acceptEdits",
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
    return env


def label(agent: AgentId) -> str:
    return "Grok" if agent == "grok" else "Claude"
