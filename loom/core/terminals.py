"""Server-side persistent *terminal* sessions — the real agent TUI in the browser.

loom runs the **actual** interactive CLI (`claude` or `grok`) under a PTY and streams
its raw bytes to xterm.js, so every slash command / permission prompt / feature works
with zero reimplementation. This is the only chat surface loom exposes (see
docs/ARCHITECTURE.md). Agent choice is per-chat (`agent` in the sessions overlay).

Two interchangeable hosts keep the agent alive across browser disconnects *and* loom
restarts (there's no hot-reload — every backend edit restarts the server; a bare PTY
would die with it):

- **pty** (default — "smooth scroll"): agent under a detached `loom.core.pty_server`
  daemon on a Unix socket, using the *inline* renderer. No alternate screen, so xterm.js
  owns a real scrollback — native wheel scroll, drag-select/copy, and none of the
  three-emulator (Ink↔tmux↔xterm) width desync that garbled the tmux mode.
- **tmux** ("classic"): the original fullscreen-pinned mode — agent inside a tmux
  session (`loomx-<chat_id>`), loom attached via one PTY. Kept as a per-session fallback
  during the pty migration; also what a native `tmux attach` shares.

The per-chat host choice is persisted in the sessions overlay (`terminal_backend`) and is
switchable via `switch_backend()` — a kill + `--resume` relaunch (the transcript is the
durable state), so the conversation survives the hop.
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
import socket
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from loom.core import agents
from loom.core import sessions as sessions_mod
from loom.core.config import LOGS_DIR, LOOM_HOME
from loom.core.pty_server import (
    CMD_SNAPSHOT,
    ESCAPE,
    encode_resize,
    encode_snapshot_request,
    is_session_alive,
    kill_session,
)
from loom.core.runtime import _RESILIENCE_ENV, _loom_runtime

_registry: dict[str, "_SessionBase"] = {}

_SEND_TIMEOUT = 5.0          # max seconds to push a frame to one subscriber before dropping it
_BACKLOG_MAX = 256 * 1024    # bytes of recent output kept for replay-on-attach
_READ_CHUNK = 65536
_SOCK_DIR = LOOM_HOME / "pty-sockets"   # one AF_UNIX socket per pty-backed session
# "I'm running inside Claude Code" markers — scrub them from the child so a nested
# `claude` doesn't inherit auto-approve (see CLAUDE.md). Auth/API-key vars are untouched.
_SCRUB = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")

BACKENDS = ("pty", "tmux")
DEFAULT_BACKEND = "pty"


def _tmux() -> str | None:
    return shutil.which("tmux")


def session_name(chat_id: str) -> str:
    """Stable slug for a chat — the tmux session name (how a native terminal attaches the
    same session) and the pty socket filename base."""
    return "loomx-" + re.sub(r"[^a-zA-Z0-9_-]", "-", chat_id)[:60]


def _socket_path(chat_id: str) -> str:
    return str(_SOCK_DIR / (session_name(chat_id) + ".sock"))


NEEDS_DIR = agents.NEEDS_DIR  # per-chat "needs you" markers (written/cleared by agent hooks)


def _marker_path(chat_id: str) -> Path:
    """File whose existence means 'this chat is waiting on you'. Set by Stop/Notification
    hooks, cleared by UserPromptSubmit; read by list_active()."""
    return agents._marker_path(chat_id)


def _agent_for(chat_id: str) -> agents.AgentId:
    """Locked agent for this chat (overlay); defaults to claude for legacy chats."""
    return agents.normalize_agent(sessions_mod.get_overlay(chat_id).get("agent"))


def _agent_argv(chat_id: str, cwd: str | None, *, fullscreen: bool) -> list[str]:
    """Build argv for the chat's locked agent (resume if transcript exists, else mint id)."""
    return agents.build_argv(_agent_for(chat_id), chat_id, cwd, fullscreen=fullscreen)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    with contextlib.suppress(Exception):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))


class _SessionBase:
    """Common surface both hosts implement: subscriber fan-out, write buffering, and the
    async queue that serializes output to N WebSockets. Subclasses provide the transport
    (`_write_fd`), lifecycle (`open`/`close`), `resize`, `repaint`, and `subscribe`."""

    backend = "?"  # "tmux" | "pty" — mirrored to the browser so it picks the right wheel path

    def __init__(self, chat_id: str, cwd: str | None) -> None:
        self.key = chat_id
        self.chat_id = chat_id
        self.cwd = cwd
        self.cols, self.rows = 120, 32
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wbuf = bytearray()  # pending input writes — drained completely (big pastes short-write)
        self._writer_on = False
        # /clear guard (see _guard_input): reconstruction of the composer's current line.
        # bytearray = known contents; None = unknowable (cursor keys / history recall) → fail open.
        self._line_buf: bytearray | None = bytearray()
        self._paste_depth = 0        # inside bracketed paste (ESC[200~ … ESC[201~)
        self._guard_carry = b""      # partial paste marker split across write() chunks
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

    # --- transport hooks -------------------------------------------------------
    @property
    def attached(self) -> bool:
        raise NotImplementedError

    def _write_fd(self) -> int | None:
        """fd input is flushed to (PTY master for tmux, AF_UNIX socket for pty)."""
        raise NotImplementedError

    def _frame_input(self, data: bytes) -> bytes:
        """Transport framing for terminal input (pty escapes \\x1c; tmux is raw)."""
        return data

    # Submissions loom must NEVER forward to the CLI. /clear is client-side in claude: it
    # wipes the conversation and hops to a NEW session id, orphaning loom's chat↔task
    # mapping — and the only way it ever arrived through loom was keystrokes typed into a
    # stale-rendering tab (2026-07-09: five /clears silently executed against a live
    # session mid-work). Exact-line match on submit; prose merely containing "/clear"
    # is unaffected.
    _BLOCKED_SUBMITS = (b"/clear",)
    _PASTE_ON, _PASTE_OFF = b"\x1b[200~", b"\x1b[201~"
    _GUARD_LINE_CAP = 256  # longer lines can't equal a blocked command — stop tracking

    def _guard_input(self, data: bytes) -> bytes:
        """Swallow the Enter that would submit a blocked local command (today: /clear).

        Tracks the composer's current line from the raw keystroke stream: printable
        bytes append, backspace trims, ^C/^U reset, Enter submits. Bracketed-paste
        payload counts as typed text (so paste-then-Enter is guarded); any OTHER
        escape sequence makes the line unknowable (cursor moves, history recall) and
        the guard fails OPEN — never false-block real work. On a blocked submit the
        Enter is dropped and a BEL is fanned to subscribers: the terminal beeps, the
        typed text stays put unsent.
        """
        data = self._guard_carry + data
        self._guard_carry = b""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == 0x1B:
                rest = data[i:i + 6]
                if data.startswith(self._PASTE_ON, i):
                    self._paste_depth += 1
                    out += self._PASTE_ON
                    i += 6
                    continue
                if data.startswith(self._PASTE_OFF, i):
                    self._paste_depth = max(0, self._paste_depth - 1)
                    out += self._PASTE_OFF
                    i += 6
                    continue
                # Split paste marker at chunk end? Hold ≥2-byte prefixes for the next
                # chunk. A LONE trailing ESC is forwarded immediately — it's claude's
                # interrupt key and must never be delayed (costs us: taint, fail open).
                if 2 <= len(rest) < 6 and (self._PASTE_ON.startswith(rest) or self._PASTE_OFF.startswith(rest)):
                    self._guard_carry = bytes(data[i:])
                    return bytes(out)
                self._line_buf = None  # real escape sequence → composer state unknowable
                out.append(b)
                i += 1
                continue
            if b in (0x0D, 0x0A):
                if self._paste_depth:  # literal newline inside a paste → composer newline
                    self._line_buf = bytearray()
                elif self._line_buf is not None and bytes(self._line_buf).strip() in self._BLOCKED_SUBMITS:
                    with contextlib.suppress(Exception):  # beep, best-effort
                        self.outq.put_nowait(("data", b"\x07"))
                    i += 1
                    continue  # swallow the Enter; typed text stays in the composer
                else:
                    self._line_buf = bytearray()  # normal submit → fresh line
                out.append(b)
                i += 1
                continue
            if b in (0x7F, 0x08) and not self._paste_depth:  # backspace
                if self._line_buf:
                    del self._line_buf[-1:]
                out.append(b)
                i += 1
                continue
            if b in (0x03, 0x15) and not self._paste_depth:  # ^C / ^U clear the line
                self._line_buf = bytearray()
                out.append(b)
                i += 1
                continue
            if self._line_buf is not None:
                if b >= 0x20:  # printable ASCII + any UTF-8 continuation
                    self._line_buf.append(b)
                    if len(self._line_buf) > self._GUARD_LINE_CAP:
                        self._line_buf = None
                else:
                    self._line_buf = None  # other control byte → unknowable
            out.append(b)
            i += 1
        return bytes(out)

    # --- I/O from the browser ------------------------------------------------
    def write(self, data: bytes) -> None:
        if self._write_fd() is None or not data:
            return
        data = self._guard_input(data)
        if not data:
            return
        self._wbuf += self._frame_input(data)
        self._flush_writes()

    def _raw_send(self, framed_bytes: bytes) -> None:
        """Queue already-framed bytes (control frames bypass _frame_input)."""
        if self._write_fd() is None or not framed_bytes:
            return
        self._wbuf += framed_bytes
        self._flush_writes()

    def _flush_writes(self) -> None:
        # Drain pending input. os.write on a non-blocking fd can SHORT-write (a large
        # paste exceeds the buffer); ignoring the return value silently dropped the tail —
        # that's why a big paste arrived as only ~12 lines. Loop to write everything; if it
        # can't all go now, finish via an add_writer callback once the fd is writable again.
        fd = self._write_fd()
        if fd is None:
            return
        while self._wbuf:
            try:
                n = os.write(fd, self._wbuf)
            except BlockingIOError:
                break  # buffer full — wait for writable
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

    # --- fan-out ---------------------------------------------------------------
    async def _drain(self) -> None:
        # Single writer → bytes reach every subscriber in order (a slow socket can't
        # reorder another's stream).
        while True:
            kind, payload = await self.outq.get()
            if kind == "data":
                await self._broadcast(payload)
            else:  # "snapshot" (pty only)
                await self._broadcast_snapshot(payload)

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

    async def _broadcast_snapshot(self, payload: bytes) -> None:
        """Deliver a settled daemon snapshot to all subscribers, bracketed with JSON
        markers so the client buffers it and applies it atomically (reset + one write —
        flicker-free, and idempotent: the snapshot IS the full authoritative state)."""
        await self._broadcast_json({"type": "snapshot-start"})
        # \x1b[2J\x1b[H = clear screen + home cursor (clean canvas for the repaint).
        await self._broadcast(b"\x1b[2J\x1b[H" + payload)
        await self._broadcast_json({"type": "snapshot-end"})

    async def _broadcast_json(self, obj: dict) -> None:
        for ws in list(self.subscribers):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_json(obj), timeout=_SEND_TIMEOUT)


class TmuxTerminalSession(_SessionBase):
    """The classic fullscreen host: `claude` inside tmux, loom attached via one PTY.
    tmux owns the screen (xterm only sees its alternate buffer), so scroll is forwarded
    as SGR wheel events and repaints are `refresh-client` redraws."""

    backend = "tmux"

    def __init__(self, chat_id: str, cwd: str | None) -> None:
        super().__init__(chat_id, cwd)
        self.session = session_name(chat_id)
        self.master_fd: int | None = None
        self.client_tty: str | None = None  # our tmux client id, for clean refresh-client repaints
        self.proc: subprocess.Popen | None = None

    @property
    def attached(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write_fd(self) -> int | None:
        return self.master_fd

    # --- tmux session ----------------------------------------------------------
    def _ensure_tmux(self) -> None:
        tm = _tmux()
        if not tm:
            raise RuntimeError("tmux not found — install tmux to use terminal mode")
        has = subprocess.run([tm, "has-session", "-t", self.session], capture_output=True)
        if has.returncode != 0:  # not alive yet → create it (else just re-apply options + re-attach)
            cwd = self.cwd or os.path.expanduser("~")
            agent = _agent_for(self.chat_id)
            env, _ = _loom_runtime(self.cwd)
            # Claude-only resilience knobs; grok ignores unknown vars but keep env clean.
            res = _RESILIENCE_ENV if agent == "claude" else {}
            full_env = agents.child_env(agent, self.chat_id, {**res, **env})
            scrub = "unset " + " ".join(_SCRUB) + " 2>/dev/null"
            # Grow scrollback BEFORE creating the pane — history-limit only applies to NEW
            # windows, so setting it after (as before) was a no-op and left it at tmux's
            # 2000-line default. 1,000,000 is effectively "infinite" for a chat; tmux stores
            # lines lazily, so memory scales with actual output, not this cap.
            subprocess.run([tm, "set-option", "-g", "history-limit", "1000000"], capture_output=True)
            # `exec <agent>` so the pane IS the CLI (no extra shell layer); `sh -c` (not a login
            # shell) avoids sourcing ~/.zshrc, whose guarded auto-`claude` would double-launch.
            argv = _agent_argv(self.chat_id, self.cwd, fullscreen=True)
            pane = f"{scrub}; exec {' '.join(shlex.quote(a) for a in argv)}"
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
        # `window-size latest` = the pane follows the most-recently-active client (the browser PTY
        # while you type), so a stale/smaller second client (e.g. a native `tmux attach`) can't pin
        # the width and desync it from xterm — a width mismatch is what garbles the input line.
        for opt in (["status", "off"], ["destroy-unattached", "off"], ["mouse", "on"],
                    ["set-clipboard", "on"], ["window-size", "latest"]):
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
        # Force claude to re-read the terminal width once it's up — see _resync_size.
        loop.call_later(2.5, self._resync_size)

    def _resync_size(self) -> None:
        """Nudge the PTY winsize by one column (→ two SIGWINCHes via tmux) so claude's Ink TUI
        recomputes line-wrap at the *current* width. A resize that lands before claude installs its
        SIGWINCH handler at startup is silently lost, leaving Ink laying out for a stale width — the
        cause of the input line wrapping at the wrong column. A plain repaint can't fix that; only a
        real size change makes Ink recompute. Cheap + idempotent; safe to call on every attach."""
        fd = self.master_fd
        if fd is None or self._closed:
            return
        _set_winsize(fd, self.rows, max(self.cols - 1, 1))  # shrink 1 col → SIGWINCH
        if self._loop:
            self._loop.call_later(0.15, self._restore_size)  # then restore → second SIGWINCH

    def _restore_size(self) -> None:
        if self.master_fd is not None and not self._closed:
            _set_winsize(self.master_fd, self.rows, self.cols)

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
        self.outq.put_nowait(("data", data))

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
        with contextlib.suppress(Exception):
            _marker_path(self.chat_id).unlink(missing_ok=True)
        _registry.pop(self.key, None)

    async def close(self) -> None:
        """Hard stop: kill the tmux session + PTY (used on delete / backend switch)."""
        self._closed = True
        self._detach_pty()
        tm = _tmux()
        if tm:
            with contextlib.suppress(Exception):
                subprocess.run([tm, "kill-session", "-t", self.session], capture_output=True)
        with contextlib.suppress(Exception):
            _marker_path(self.chat_id).unlink(missing_ok=True)
        _registry.pop(self.key, None)

    def resize(self, cols: int, rows: int) -> None:
        # Ignore degenerate sizes: a collapsed/hidden browser pane can propose e.g. 11x6, and
        # applying it makes claude's Ink TUI reflow to a few columns — corruption that persists
        # even after the pane grows back. Keep the last good size instead. (The browser guards this
        # too, but native `tmux attach` clients and startup races come through here as well.)
        if cols < 20 or rows < 5:
            return
        self.cols, self.rows = cols, rows
        if self.master_fd is not None:
            # SIGWINCH to the tmux client → with aggressive-resize the window follows it.
            _set_winsize(self.master_fd, rows, cols)

    # --- subscribers ---------------------------------------------------------
    def _repaint(self) -> None:
        """Force tmux to FULLY redraw the current screen to our PTY (→ all subscribers). A full
        redraw overwrites every cell, so it clears display corruption from claude's torn
        re-renders (rapid scroll / resize) — what a browser refresh does, but without reconnecting.

        Waits for the TUI to go briefly quiet first: a refresh-client fired *mid* Ink re-render
        re-captures a torn frame, so we poll last_output until it's been idle ~40ms (2×20ms),
        capped at 300ms so a continuously-animating TUI still eventually repaints."""
        tm = _tmux()
        if not (self.client_tty and tm):
            return
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            if time.monotonic() - self.last_output >= 0.04:
                break
            time.sleep(0.02)
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


class _SnapshotStreamDecoder:
    """Pull CMD_SNAPSHOT frames out of the daemon's \\x1c-escaped byte stream.

    The daemon escapes live output (\\x1c -> \\x1c\\x1c) and delivers snapshots as
    \\x1c\\x02 <len:u32> <payload> (payload NOT escaped). This decoder walks the
    incoming bytes and yields ("data", bytes) for normal (unescaped) output and
    ("snapshot", bytes) for a complete snapshot frame. It buffers partial frames
    across recv() boundaries.

    Yields are emitted in order so a snapshot that interrupts live output is
    surfaced at the right point in the stream.
    """

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        out = []
        i = 0
        n = len(self._buf)
        pending = bytearray()   # accumulates unescaped normal output

        def flush_pending():
            if pending:
                out.append(("data", bytes(pending)))
                pending.clear()

        while i < n:
            b = self._buf[i]
            if b != ESCAPE:
                # Fast span of normal bytes up to the next ESCAPE.
                nxt = self._buf.find(bytes([ESCAPE]), i)
                if nxt == -1:
                    pending.extend(self._buf[i:n])
                    i = n
                    break
                pending.extend(self._buf[i:nxt])
                i = nxt
                continue
            # b == ESCAPE
            if i + 1 >= n:
                break                      # need the next byte
            nb = self._buf[i + 1]
            if nb == ESCAPE:
                pending.append(ESCAPE)     # \x1c\x1c -> literal \x1c
                i += 2
                continue
            if nb == CMD_SNAPSHOT:
                if i + 6 > n:
                    break                  # need the 4-byte length
                (length,) = struct.unpack('>I', self._buf[i + 2:i + 6])
                if i + 6 + length > n:
                    break                  # need the full payload
                flush_pending()            # emit output before the snapshot
                out.append(("snapshot", bytes(self._buf[i + 6:i + 6 + length])))
                i += 6 + length
                continue
            # Unknown escape — treat the ESCAPE as a literal byte and move on.
            pending.append(ESCAPE)
            i += 1

        flush_pending()
        del self._buf[:i]
        return out


class PtyTerminalSession(_SessionBase):
    """The smooth-scroll host: `claude` (inline renderer) under a detached
    `loom.core.pty_server` daemon. The daemon owns the PTY on a Unix socket and outlives
    loom restarts — the same persistence tmux gave, minus the alternate screen, so
    xterm.js keeps a real scrollback and there's no reflow desync to garble."""

    backend = "pty"

    def __init__(self, chat_id: str, cwd: str | None) -> None:
        super().__init__(chat_id, cwd)
        self.socket_path = _socket_path(chat_id)
        self.sock: socket.socket | None = None
        self._decoder = _SnapshotStreamDecoder()

    @property
    def attached(self) -> bool:
        return self.sock is not None

    def _write_fd(self) -> int | None:
        return None if self.sock is None else self.sock.fileno()

    def _frame_input(self, data: bytes) -> bytes:
        # A literal \x1c in user input must not be parsed as a daemon control escape.
        return data.replace(bytes([ESCAPE]), bytes([ESCAPE, ESCAPE]))

    # --- daemon spawn-or-reconnect (the pty analogue of _ensure_tmux) ----------
    def _ensure_daemon(self) -> None:
        """Spawn the detached pty_server if its socket isn't live, else reuse it.

        Blocking; called via run_in_executor. Spawns with start_new_session=True
        so the daemon (and the claude session) outlive loom restarts.
        """
        _SOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_session_alive(self.socket_path):
            return                          # running daemon — just reconnect
        # Stale socket file with no live daemon behind it — clear it.
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(self.socket_path)

        cwd = self.cwd or os.path.expanduser("~")
        agent = _agent_for(self.chat_id)
        env, _ = _loom_runtime(self.cwd)
        res = _RESILIENCE_ENV if agent == "claude" else {}
        child_env = agents.child_env(agent, self.chat_id, {**os.environ, **res, **env})
        for k in _SCRUB:
            child_env.pop(k, None)

        full_cmd = [
            sys.executable, "-m", "loom.core.pty_server", self.socket_path,
            "--rows", str(self.rows), "--cols", str(self.cols),
            "--cwd", cwd, "--",
            *_agent_argv(self.chat_id, self.cwd, fullscreen=False),
        ]
        # Daemon logs (its stderr) go to a per-session file — with DEVNULL a failed
        # launch would be undiagnosable.
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(LOGS_DIR / f"{session_name(self.chat_id)}-pty.log", "ab")
        try:
            subprocess.Popen(
                full_cmd, env=child_env,
                start_new_session=True,          # <-- survives loom restarts
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=logf,
            )
        finally:
            logf.close()
        # Wait (bounded) for the socket + PID sidecar to appear.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if is_session_alive(self.socket_path):
                return
            time.sleep(0.05)
        raise RuntimeError("pty_server daemon did not come up within 5s "
                           f"(see {LOGS_DIR / (session_name(self.chat_id) + '-pty.log')})")

    # --- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        if self.sock is not None:
            return                           # already connected
        loop = asyncio.get_running_loop()
        self._loop = loop
        await loop.run_in_executor(None, self._ensure_daemon)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        await loop.run_in_executor(None, s.connect, self.socket_path)
        s.setblocking(False)
        self.sock = s
        # Seed the daemon with our current size, then reader + pump.
        self._raw_send(encode_resize(self.rows, self.cols))
        loop.add_reader(s.fileno(), self._on_readable)
        self._pump = asyncio.create_task(self._drain())
        # Ask for an authoritative settled snapshot so a reconnect after a loom
        # restart repaints the current screen (not just the raw ring).
        self._raw_send(encode_snapshot_request())

    def _on_readable(self) -> None:
        try:
            data = os.read(self.sock.fileno(), _READ_CHUNK)  # type: ignore[union-attr]
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:                         # daemon gone / socket closed
            self._detach_socket()
            asyncio.create_task(self._on_eof())
            return
        self.last_output = time.monotonic()
        for kind, payload in self._decoder.feed(data):
            if kind == "data":
                self.backlog += payload
                if len(self.backlog) > _BACKLOG_MAX:
                    del self.backlog[: len(self.backlog) - _BACKLOG_MAX]
                self.outq.put_nowait(("data", payload))
            else:  # "snapshot" — bracketed to the browser by _broadcast_snapshot
                self.outq.put_nowait(("snapshot", payload))

    def _detach_socket(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(Exception):
                loop = self._loop or asyncio.get_running_loop()
                loop.remove_reader(self.sock.fileno())
                if self._writer_on:
                    loop.remove_writer(self.sock.fileno())
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None
        self._writer_on = False
        self._wbuf.clear()
        if self._pump:
            self._pump.cancel()
            self._pump = None

    async def _on_eof(self) -> None:
        await self._broadcast_json({"type": "exit"})
        with contextlib.suppress(Exception):
            _marker_path(self.chat_id).unlink(missing_ok=True)
        _registry.pop(self.key, None)

    async def close(self) -> None:
        """Hard stop: kill the daemon (SIGTERM its pgroup) + detach. Used on delete /
        backend switch. A plain browser-tab close is just unsubscribe — the daemon (and
        claude) keep running."""
        self._closed = True
        self._detach_socket()
        await asyncio.get_running_loop().run_in_executor(None, kill_session, self.socket_path)
        with contextlib.suppress(Exception):
            _marker_path(self.chat_id).unlink(missing_ok=True)
        _registry.pop(self.key, None)

    def resize(self, cols: int, rows: int) -> None:
        """Send a resize frame to the daemon (which does TIOCSWINSZ + SIGWINCH).
        Same degenerate-size floor as the tmux host — a collapsed pane (e.g. 11x6)
        makes Ink reflow to a few columns; keep the last good size instead."""
        if cols < 20 or rows < 5:
            return
        self.cols, self.rows = cols, rows
        if self.sock is not None:
            self._raw_send(encode_resize(rows, cols))

    # --- repaint (replaces tmux refresh-client) ------------------------------
    async def repaint(self) -> None:
        """Request an authoritative settled snapshot from the daemon. The daemon waits
        for the TUI to go quiescent, then sends the alt-screen-aware tail; _on_readable
        surfaces it as a ("snapshot", ...) item and _broadcast_snapshot brackets it to
        subscribers. This is what heals resize/reconnect artifacts without a refresh."""
        if self.sock is not None:
            self._raw_send(encode_snapshot_request())

    # --- subscribers ---------------------------------------------------------
    async def subscribe(self, ws) -> None:
        self.subscribers.add(ws)
        # Seed the newcomer with the byte backlog immediately so there's content on
        # screen, then request a fresh settled snapshot (arrives bracketed via the pump)
        # as the authoritative repaint.
        if self.backlog:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    ws.send_bytes(b"\x1b[H\x1b[2J" + bytes(self.backlog)),
                    timeout=_SEND_TIMEOUT,
                )
        await self.repaint()


# --- registry API -------------------------------------------------------------
def get(key: str) -> _SessionBase | None:
    return _registry.get(key)


def list_active() -> list[dict]:
    """Per live terminal session: seconds since last output (`idle_sec`) + whether claude has
    signaled it's waiting on you (`needs`, from the hook marker). The sidebar shows a 'working'
    pulse from `idle_sec` and a 'needs you' dot from `needs`. Only sessions currently
    attached (i.e. opened this loom run)."""
    now = time.monotonic()
    out: list[dict] = []
    for ts in _registry.values():
        if not ts.attached:
            continue  # not attached / claude exited
        out.append({
            "chat_id": ts.chat_id,
            "idle_sec": round(now - ts.last_output, 1),
            "needs": _marker_path(ts.chat_id).exists(),
        })
    return out


def active_sessions() -> list[_SessionBase]:
    """Live terminal sessions (transport attached) — used by the dev-stack supervisor (monitor.py)."""
    return [ts for ts in list(_registry.values()) if ts.attached]


def terminal_backend(chat_id: str) -> str:
    """Effective host for a chat: the persisted per-chat choice, else tmux when a live
    pre-backend-era tmux session already holds this chat (spawning a pty daemon too would
    run TWO claudes on one session id), else the pty default. Blocking (subprocess) —
    call via run_in_executor on the loop."""
    pref = sessions_mod.get_overlay(chat_id).get("terminal_backend")
    if pref in BACKENDS:
        return pref
    tm = _tmux()
    if tm and subprocess.run([tm, "has-session", "-t", session_name(chat_id)],
                             capture_output=True).returncode == 0:
        return "tmux"
    return DEFAULT_BACKEND


async def open_terminal(chat_id: str, cwd: str | None, cols: int = 120, rows: int = 32) -> _SessionBase:
    loop = asyncio.get_running_loop()
    backend = await loop.run_in_executor(None, terminal_backend, chat_id)
    ts = _registry.get(chat_id)
    if ts is not None and ts.backend != backend:
        # The persisted choice changed under a live session (e.g. switched from another
        # tab) — retire the old host before opening the new one.
        await ts.close()
        ts = None
    if ts is None:
        cls: type[_SessionBase] = PtyTerminalSession if backend == "pty" else TmuxTerminalSession
        ts = cls(chat_id, cwd)
        ts.cols, ts.rows = cols or 120, rows or 32
        _registry[chat_id] = ts
        # Persist the effective choice so this session keeps its host even if the
        # default changes later (idempotent re-write on reopen).
        await loop.run_in_executor(
            None, sessions_mod.set_overlay, chat_id, {"terminal_backend": backend}
        )
    await ts.open()
    return ts


async def switch_backend(chat_id: str, target: str) -> None:
    """Move a chat to the other terminal host: kill the live host (tmux session or pty
    daemon), persist the choice. NOT a live flip — the next open_terminal relaunches
    claude under the new host with `--resume <chat_id>`, so the conversation (the on-disk
    transcript) carries over; only the live process restarts. Any in-flight turn is
    interrupted, so the UI should offer this while the session is idle."""
    if target not in BACKENDS:
        raise ValueError(f"unknown terminal backend {target!r} (expected one of {BACKENDS})")
    ts = _registry.pop(chat_id, None)
    if ts is not None:
        await ts.close()

    def _kill_other_hosts() -> None:
        # Sweep BOTH hosts (idempotent no-ops when absent) — covers a host left over from
        # a previous loom run that this process never attached.
        kill_session(_socket_path(chat_id))
        tm = _tmux()
        if tm:
            subprocess.run([tm, "kill-session", "-t", session_name(chat_id)], capture_output=True)
        sessions_mod.set_overlay(chat_id, {"terminal_backend": target})

    await asyncio.get_running_loop().run_in_executor(None, _kill_other_hosts)
