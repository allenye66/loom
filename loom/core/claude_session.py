"""Launch / resume Claude Code sessions inside a worktree.

Default launcher: a new Terminal.app window (this Mac has no tmux/iTerm). If tmux
is installed, prefer a detached tmux session. `resume_session` reopens an existing
chat by id (`claude --resume <id>`), replacing `/resume`.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path


def _launch(inner: str, label: str, prefer: str = "auto") -> str:
    if prefer in ("auto", "tmux") and shutil.which("tmux"):
        session = "loom-" + re.sub(r"[^a-zA-Z0-9_-]", "-", label)[:30]
        subprocess.run(["tmux", "new-session", "-d", "-s", session, inner], check=False)
        return f"tmux session '{session}' — attach with: tmux attach -t {session}"

    if shutil.which("osascript"):  # macOS Terminal.app
        # Drop a one-shot marker so a guarded ~/.zshrc skips its auto-Claude launch for
        # THIS window — the new Terminal shell sources ~/.zshrc (and would run that
        # launch) before our command can run. See README/handoff notes.
        with contextlib.suppress(Exception):
            marker = Path.home() / ".loom" / "skip-autoclaude"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'tell application "Terminal" to do script "{escaped}"\n'
            'tell application "Terminal" to activate'
        )
        subprocess.run(["osascript", "-e", script], check=False)
        return "Terminal.app window"

    return f"no terminal launcher found — run manually:\n  {inner}"


def _claude() -> str:
    return shutil.which("claude") or "claude"


def open_session(worktree_path: str, prompt: str | None = None, prefer: str = "auto", effort: str = "max") -> str:
    """Start a fresh Claude session in a worktree, optionally seeding a /skill."""
    inner = f"cd {shlex.quote(worktree_path)} && {shlex.quote(_claude())} --effort {shlex.quote(effort)}"
    if prompt:
        inner += f" {shlex.quote(prompt)}"
    return _launch(inner, label=Path(worktree_path).name, prefer=prefer)


def resume_session(cwd: str, session_id: str, fork: bool = False, prefer: str = "auto", effort: str = "max") -> str:
    """Reopen an existing chat by id (replaces /resume). `fork` branches it.

    Always passes `--effort` so a terminal handoff keeps the same effort as the
    loom chat (a bare `claude --resume` would fall back to the CLI default).
    """
    inner = f"cd {shlex.quote(cwd)} && {shlex.quote(_claude())} --effort {shlex.quote(effort)} --resume {shlex.quote(session_id)}"
    if fork:
        inner += " --fork-session"
    return _launch(inner, label=session_id, prefer=prefer)
