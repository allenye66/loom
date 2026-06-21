"""Server-side persistent *terminal* sessions — the real `claude` TUI in the browser.

loom runs the **actual** interactive `claude` CLI under a PTY and streams its raw bytes
to xterm.js, so every slash command / permission prompt / feature works with zero
reimplementation. This is the only chat surface loom exposes (see docs/ARCHITECTURE.md).

`claude` runs inside a **tmux** session (named `loomx-<chat_id>`), not a bare PTY, so the
session survives both browser disconnects *and* loom restarts (there's no hot-reload —
every backend edit restarts the server; a bare PTY would die with it). loom holds one
PTY attached to that tmux session and fans its bytes out to N WebSocket subscribers.
Reattach after a restart just re-runs `tmux attach`.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import shlex
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path

from loom.core import sessions as sessions_mod
from loom.core.runtime import _RESILIENCE_ENV, _loom_runtime

_registry: dict[str, "TerminalSession"] = {}

_SEND_TIMEOUT = 5.0          # max seconds to push a frame to one subscriber before dropping it
_BACKLOG_MAX = 256 * 1024    # bytes of recent output kept for replay-on-attach
_READ_CHUNK = 65536
# "I'm running inside Claude Code" markers — scrub them from the child so the nested
# `claude` doesn't inherit auto-approve (see CLAUDE.md). Auth/API-key vars are untouched.
_SCRUB = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def _claude() -> str:
    return shutil.which("claude") or "claude"


def _tmux() -> str | None:
    return shutil.which("tmux")


def session_name(chat_id: str) -> str:
    """tmux session name for a chat — also how a native terminal attaches the same session."""
    return "loomx-" + re.sub(r"[^a-zA-Z0-9_-]", "-", chat_id)[:60]


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(Exception):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


class TerminalSession:
    def __init__(self, chat_id: str, cwd: str | None) -> None:
        self.key = chat_id
        self.chat_id = chat_id
        self.cwd = cwd
        self.session = session_name(chat_id)
        self.cols, self.rows = 120, 32
        self.master_fd: int | None = None
        self.client_tty: str | None = None  # our tmux client id, for clean refresh-client repaints
        self.proc: subprocess.Popen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wbuf = bytearray()  # pending PTY writes — drained completely (big pastes short-write)
        self._writer_on = False
        self.subscribers: set = set()
        self.backlog = bytearray()
        self.outq: asyncio.Queue = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._closed = False
        # Wall-clock of the last PTY output. claude's TUI animates (~1/sec) while it's
        # working, so "no output for a few seconds" ≈ idle/waiting-on-you. Powers the
        # sidebar's working-pulse vs "needs you" indicator. Starts "now" so a freshly
        # opened session reads as active until it actually goes quiet.
        self.last_output = time.monotonic()

    # --- tmux + claude command -----------------------------------------------
    def _claude_argv(self) -> list[str]:
        """`claude` flags mirroring the SDK chat: max effort + the worktree runtime note.
        Resume the chat's stable id if it exists on disk, else create it with that id so
        the terminal session is the same resumable conversation loom indexes."""
        _, note = _loom_runtime(self.cwd)
        # `tui: fullscreen` = the alternate-screen renderer (avoids the inline renderer's
        # redraw-tearing — upstream bug anthropics/claude-code#29937/#37076). `theme: dark`
        # forces readable colors for loom's dark terminal: a user whose *global* theme is "light"
        # otherwise gets dark-text-on-dark, unreadable (esp. question/permission highlights). Both
        # are per-session via --settings, so the user's global ~/.claude/settings.json is untouched.
        argv = [_claude(), "--effort", "max", "--settings", '{"tui":"fullscreen","theme":"dark"}']
        if note:
            argv += ["--append-system-prompt", note]
        if sessions_mod._find_transcript(self.chat_id):
            argv += ["--resume", self.chat_id]
        else:
            argv += ["--session-id", self.chat_id]
        return argv

    def _ensure_tmux(self) -> None:
        tm = _tmux()
        if not tm:
            raise RuntimeError("tmux not found — install tmux to use terminal mode")
        has = subprocess.run([tm, "has-session", "-t", self.session], capture_output=True)
        if has.returncode != 0:  # not alive yet → create it (else just re-apply options + re-attach)
            cwd = self.cwd or os.path.expanduser("~")
            env, _ = _loom_runtime(self.cwd)
            full_env = {**_RESILIENCE_ENV, **env}
            scrub = "unset " + " ".join(_SCRUB) + " 2>/dev/null"
            # Grow scrollback BEFORE creating the pane — history-limit only applies to NEW
            # windows, so setting it after (as before) was a no-op and left it at tmux's
            # 2000-line default. 1,000,000 is effectively "infinite" for a chat; tmux stores
            # lines lazily, so memory scales with actual output, not this cap.
            subprocess.run([tm, "set-option", "-g", "history-limit", "1000000"], capture_output=True)
            # `exec claude` so the pane IS claude (no extra shell layer); `sh -c` (not a login
            # shell) avoids sourcing ~/.zshrc, whose guarded auto-`claude` would double-launch.
            pane = f"{scrub}; exec {' '.join(shlex.quote(a) for a in self._claude_argv())}"
            cmd = [tm, "new-session", "-d", "-s", self.session, "-x", str(self.cols), "-y", str(self.rows), "-c", cwd]
            for k, v in full_env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += ["sh", "-c", pane]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"tmux new-session failed: {r.stderr.strip() or r.stdout.strip()}")
        # Session options — re-applied every open (idempotent) so a session created before a
        # config change still picks them up on reconnect. `mouse on` is the key one: tmux owns
        # the screen (xterm only sees its alternate buffer), so without it the scroll wheel
        # hits xterm's empty scrollback instead of tmux's history. set-clipboard routes copies
        # to the browser clipboard via OSC 52; status off / aggressive-resize keep it clean.
        for opt in (["status", "off"], ["destroy-unattached", "off"], ["mouse", "on"], ["set-clipboard", "on"]):
            subprocess.run([tm, "set-option", "-t", self.session, *opt], capture_output=True)
        subprocess.run([tm, "setw", "-t", self.session, "aggressive-resize", "on"], capture_output=True)

    # --- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        if self.proc and self.proc.poll() is None:
            return  # already attached and alive
        loop = asyncio.get_running_loop()
        self._loop = loop
        await loop.run_in_executor(None, self._ensure_tmux)
        master, slave = pty.openpty()
        with contextlib.suppress(Exception):
            self.client_tty = os.ttyname(slave)  # how tmux identifies this client (for refresh-client)
        _set_winsize(master, self.rows, self.cols)
        # `attach -d` detaches any stale client (e.g. a dead PTY from before a loom restart)
        # so this PTY is the sole driver of the session's size.
        self.proc = subprocess.Popen(
            [_tmux(), "attach-session", "-d", "-t", self.session],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        os.set_blocking(master, False)
        self.master_fd = master
        loop.add_reader(master, self._on_readable)
        self._pump = asyncio.create_task(self._drain())

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master_fd, _READ_CHUNK)  # type: ignore[arg-type]
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:  # EOF — tmux attach exited (session ended / claude quit)
            self._detach_pty()
            asyncio.create_task(self._on_eof())
            return
        self.last_output = time.monotonic()  # bytes flowing → claude is working
        self.backlog += data
        if len(self.backlog) > _BACKLOG_MAX:
            del self.backlog[: len(self.backlog) - _BACKLOG_MAX]
        self.outq.put_nowait(data)

    async def _drain(self) -> None:
        # Single writer → bytes reach every subscriber in order (a slow socket can't
        # reorder another's stream).
        while True:
            data = await self.outq.get()
            await self._broadcast(data)

    def _detach_pty(self) -> None:
        if self.master_fd is not None:
            with contextlib.suppress(Exception):
                loop = self._loop or asyncio.get_running_loop()
                loop.remove_reader(self.master_fd)
                if self._writer_on:
                    loop.remove_writer(self.master_fd)
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = None
        self._writer_on = False
        self._wbuf.clear()
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()

    async def _on_eof(self) -> None:
        # The tmux session is gone (user ran /exit or it was killed). Tell attached tabs
        # and drop from the registry so the next open recreates a fresh session.
        await self._broadcast_json({"type": "exit"})
        _registry.pop(self.key, None)

    async def close(self) -> None:
        """Hard stop: kill the tmux session + PTY (used on delete / explicit close)."""
        self._closed = True
        self._detach_pty()
        tm = _tmux()
        if tm:
            with contextlib.suppress(Exception):
                subprocess.run([tm, "kill-session", "-t", self.session], capture_output=True)
        _registry.pop(self.key, None)

    # --- I/O from the browser ------------------------------------------------
    def write(self, data: bytes) -> None:
        if self.master_fd is None or not data:
            return
        self._wbuf += data
        self._flush_writes()

    def _flush_writes(self) -> None:
        # Drain pending input to the PTY. os.write on the non-blocking master can SHORT-write
        # (a large paste exceeds the tty buffer); the old code ignored the return value, so the
        # tail was silently dropped — that's why a big paste arrived as only ~12 lines. Loop to
        # write everything; if it can't all go now, finish via an add_writer callback once the
        # fd is writable again.
        fd = self.master_fd
        if fd is None:
            return
        while self._wbuf:
            try:
                n = os.write(fd, self._wbuf)
            except BlockingIOError:
                break  # tty buffer full — wait for writable
            except OSError:
                self._wbuf.clear()
                break
            if n <= 0:
                break
            del self._wbuf[:n]
        loop = self._loop
        if loop is None:
            return
        if self._wbuf and not self._writer_on:
            with contextlib.suppress(Exception):
                loop.add_writer(fd, self._flush_writes)
                self._writer_on = True
        elif not self._wbuf and self._writer_on:
            with contextlib.suppress(Exception):
                loop.remove_writer(fd)
                self._writer_on = False

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        if self.master_fd is not None:
            # SIGWINCH to the tmux client → with aggressive-resize the window follows it.
            _set_winsize(self.master_fd, rows, cols)

    # --- subscribers ---------------------------------------------------------
    def _repaint(self) -> None:
        """Force tmux to FULLY redraw the current screen to our PTY (→ all subscribers). A full
        redraw overwrites every cell, so it clears display corruption from claude's torn
        re-renders (rapid scroll / resize) — what a browser refresh does, but without reconnecting."""
        tm = _tmux()
        if self.client_tty and tm:
            with contextlib.suppress(Exception):
                subprocess.run([tm, "refresh-client", "-t", self.client_tty], capture_output=True)

    async def repaint(self) -> None:
        if self.client_tty and _tmux():
            await asyncio.get_running_loop().run_in_executor(None, self._repaint)

    async def subscribe(self, ws) -> None:
        # Paint the current screen for the newcomer via a tmux redraw (re-emits the exact current
        # frame), avoiding the garbling from replaying raw history bytes mid-stream. Fall back to
        # the byte backlog only if we have no client id yet.
        self.subscribers.add(ws)
        if self.client_tty and _tmux():
            await self.repaint()
        elif self.backlog:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_bytes(b"\x1b[H\x1b[2J" + bytes(self.backlog)), timeout=_SEND_TIMEOUT)

    def unsubscribe(self, ws) -> None:
        self.subscribers.discard(ws)

    async def _broadcast(self, data: bytes) -> None:
        dead = []
        for ws in list(self.subscribers):
            try:
                await asyncio.wait_for(ws.send_bytes(data), timeout=_SEND_TIMEOUT)
            except Exception:  # noqa: BLE001 — slow/half-open socket → drop it; backlog covers replay
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    async def _broadcast_json(self, obj: dict) -> None:
        for ws in list(self.subscribers):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_json(obj), timeout=_SEND_TIMEOUT)


# --- registry API -------------------------------------------------------------
def get(key: str) -> TerminalSession | None:
    return _registry.get(key)


def list_active() -> list[dict]:
    """Per live terminal session: seconds since it last produced output. The sidebar
    reads this to show a 'working' pulse (recent output) vs a 'needs you' dot (idle).
    Only includes sessions with a PTY currently attached (i.e. opened this loom run)."""
    now = time.monotonic()
    out: list[dict] = []
    for ts in _registry.values():
        if ts.proc is None or ts.proc.poll() is not None:
            continue  # PTY not attached / claude exited
        out.append({"chat_id": ts.chat_id, "idle_sec": round(now - ts.last_output, 1)})
    return out


async def open_terminal(chat_id: str, cwd: str | None, cols: int = 120, rows: int = 32) -> TerminalSession:
    ts = _registry.get(chat_id)
    if ts is None:
        ts = TerminalSession(chat_id, cwd)
        ts.cols, ts.rows = cols or 120, rows or 32
        _registry[chat_id] = ts
    await ts.open()
    return ts
