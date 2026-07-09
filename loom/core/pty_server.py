"""Minimal PTY-persistence daemon for loom terminal sessions.

Runs a command under a PTY, listens on a Unix domain socket, and relays bytes
between one connected client (loom's PtyTerminalSession) and the PTY master fd.
The child persists across client disconnects AND across loom restarts (the
daemon is spawned detached with start_new_session=True), so reconnecting is
just "re-open the socket".

This replaces a bare `pty.openpty()` (which dies with loom) and tmux (which
enters alt-screen on attach, stealing xterm's scrollback). It does process
persistence and raw relay only — no screen management. xterm.js owns its own
scrollback.

Wire protocol, client -> daemon (the "escape protocol"):
    raw bytes                       terminal input, forwarded to the PTY
    \\x1c \\x01 <rows:u16><cols:u16>   resize (big-endian), 6 bytes total
    \\x1c \\x02                        CMD_SNAPSHOT request (no payload)
    \\x1c \\x1c                        a literal \\x1c byte

Wire protocol, daemon -> client:
    live PTY output                 \\x1c-escaped (\\x1c -> \\x1c\\x1c) so the
                                    client can find CMD_SNAPSHOT frames in-band
    on connect                      the replay ring, alt-screen-filtered (also
                                    \\x1c-escaped)
    snapshot response               \\x1c \\x02 <len:u32> <payload>  (payload is
                                    read verbatim by length — NOT escaped)

CLI:
    python -m loom.core.pty_server /path/to/session.sock --rows 40 --cols 120 -- claude --resume UUID
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import pty
import re
import select
import signal
import socket
import struct
import sys
import termios
import threading
import time
from pathlib import Path

log = logging.getLogger("loom.pty_server")

# --- protocol constants -------------------------------------------------------
ESCAPE = 0x1C          # ASCII File Separator (dtach convention)
CMD_RESIZE = 0x01
CMD_SNAPSHOT = 0x02
REPLAY_BUFFER_SIZE = 1_048_576   # 1 MB ring ~= 10k lines, matches xterm scrollback

# --- snapshot settle ----------------------------------------------------------
# SIGWINCH only signals the START of a resize; claude/Ink has no "redraw done"
# callback, so a snapshot taken mid-render captures a garbled half-drawn frame.
# Before answering, watch the replay buffer go quiescent (2 unchanged 20 ms
# polls), capped at 300 ms so a continuously-animating TUI still answers.
SNAPSHOT_SETTLE_POLL_S = 0.02        # 20 ms between stability checks
SNAPSHOT_SETTLE_STABLE_POLLS = 2     # this many unchanged polls => quiescent
SNAPSHOT_SETTLE_MAX_POLLS = 15       # 15 * 20 ms = 300 ms hard cap

# Alt-screen mode toggles: CSI ? {47,1047,1049} h|l. All three swap xterm
# between the main buffer (with scrollback) and the alternate buffer (none).
_ALT_SCREEN_RE = re.compile(rb'\x1b\[\?(?:47|1047|1049)[hl]')
_ALT_SCREEN_ENTER_RE = re.compile(rb'\x1b\[\?(?:47|1047|1049)h')

# `--resume` with a word boundary on both sides so we never match --resume-from,
# RESUME_DIR, a path containing "resume", etc. Gates the first-DA1 screen wipe.
_RESUME_RE = re.compile(r'(^|\s)--resume(\s|=|$)')


def _cmd_is_resume(cmd: list[str]) -> bool:
    """True iff the child cmd contains a `--resume` argument.

    Gates the first-DA1 screen wipe. For `claude --resume <uuid>`, claude
    renders conversation history BEFORE its first DA1 query — injecting a
    clear there would erase what the user needs to see. The wipe is only
    wanted for FRESH launches, where the only pre-TUI output is startup noise.
    """
    return any(_RESUME_RE.search(part) for part in cmd)


def _filter_alt_screen(data: bytes) -> bytes:
    """Keep only main-screen scrollback; drop alt-screen enter/exit pairs and
    everything between them.

    The replay buffer captures every byte the child emits, including alt-screen
    toggles and the frames claude/Ink drew inside them. If that is replayed into
    a fresh xterm, the toggles re-enter alt-screen and xterm ends up corrupted
    (new draws overlay old leftovers). The visible main-screen history — what a
    user scrolls up to read — is the bytes OUTSIDE alt-screen pairs. This keeps
    those and drops the rest.

    Truncation safety (the source is a 1 MB ring, so a pair can be split):
      - buffer starts mid-alt (enter was cut off): orphan exits are dropped
        (we default to "main" and only transition on enters)
      - buffer ends mid-alt (no exit yet): drop the unbalanced enter + tail
      - nested enters: no-op while already in alt
    """
    out = bytearray()
    pos = 0
    in_alt = False
    while True:
        m = _ALT_SCREEN_RE.search(data, pos)
        if not m:
            if not in_alt:
                out.extend(data[pos:])
            return bytes(out)
        is_enter = m.group(0).endswith(b'h')
        if not in_alt:
            out.extend(data[pos:m.start()])
            if is_enter:
                in_alt = True
            # else: orphan exit (truncation cut off the enter) -> drop the toggle
        else:
            if not is_enter:
                in_alt = False
            # else: nested enter -> stay in alt, keep dropping
        pos = m.end()


def _snapshot_tail(replay_buf: bytes) -> bytes:
    """Alt-screen-aware tail for a returning consumer.

    Scans for the most recent alt-screen ENTER. If found, returns from there to
    end: the client's xterm sees a clean `enter alt-screen` + the latest TUI
    frame(s), so it renders the current screen without first flashing pre-TUI
    shell content. If no enter is in the buffer (session is on main screen, or
    the enter scrolled out of the ring), returns the whole buffer — main-screen
    byte history paints correctly with no special handling.
    """
    if not replay_buf:
        return b""
    latest_idx = -1
    for m in _ALT_SCREEN_ENTER_RE.finditer(replay_buf):
        latest_idx = m.start()
    if latest_idx < 0:
        return bytes(replay_buf)
    return bytes(replay_buf[latest_idx:])


# --- wire helpers -------------------------------------------------------------
def encode_resize(rows: int, cols: int) -> bytes:
    return bytes([ESCAPE, CMD_RESIZE]) + struct.pack('>HH', rows, cols)


def encode_snapshot_request() -> bytes:
    return bytes([ESCAPE, CMD_SNAPSHOT])


def escape_data(data: bytes) -> bytes:
    """\\x1c -> \\x1c\\x1c so a literal \\x1c in TUI output can never be mistaken
    for the start of a CMD_RESIZE / CMD_SNAPSHOT escape."""
    return data.replace(bytes([ESCAPE]), bytes([ESCAPE, ESCAPE]))


def encode_snapshot_response(snapshot: bytes) -> bytes:
    """Frame a snapshot for daemon->client delivery.

    Wire format: \\x1c \\x02 <len:u32 BE> <payload>. The length makes it
    self-delimiting so the client reads the payload verbatim (unescaped) by
    length while normal output on this direction stays \\x1c-escaped.
    """
    return bytes([ESCAPE, CMD_SNAPSHOT]) + struct.pack('>I', len(snapshot)) + snapshot


class _SettleState:
    """Tracks replay-buffer-length stability across settle polls.

    One tick = one observe(current_len). is_quiescent flips True once the length
    has been unchanged for SNAPSHOT_SETTLE_STABLE_POLLS ticks.
    """

    def __init__(self, initial_len: int):
        self._last_len = initial_len
        self._stable_polls = 0

    def observe(self, current_len: int) -> None:
        if current_len == self._last_len:
            self._stable_polls += 1
        else:
            self._stable_polls = 0
            self._last_len = current_len

    @property
    def is_quiescent(self) -> bool:
        return self._stable_polls >= SNAPSHOT_SETTLE_STABLE_POLLS


class PtyServer:
    """A single PTY session relayed over one Unix socket, with a replay ring
    and an on-demand settled snapshot."""

    def __init__(self, socket_path: str, cmd: list[str],
                 env: dict | None = None, cwd: str | None = None,
                 rows: int = 24, cols: int = 80):
        self.socket_path = socket_path
        self.cmd = cmd
        self.env = env or os.environ.copy()
        self.cwd = cwd
        self.initial_rows = rows
        self.initial_cols = cols
        self.master_fd: int | None = None
        self.child_pid: int | None = None
        self.server_sock: socket.socket | None = None
        self.client_sock: socket.socket | None = None
        self._replay_buf = bytearray()
        self._first_da_seen = False
        # Suppress the first-DA1 clear on resume launches (see _cmd_is_resume).
        self._inject_first_da_clear = not _cmd_is_resume(cmd)
        # Carryover for a resize/snapshot escape split across recv() boundaries.
        self._pending = bytearray()
        # Input awaiting a writable PTY master (see _write_pty). Drained via the
        # select loop's write set — NEVER by a blocking write.
        self._pty_write_buf = bytearray()
        self._running = True

    # --- startup -------------------------------------------------------------
    def start(self):
        encoded = self.socket_path.encode()
        # sun_path is 104 bytes on macOS (108 on Linux) — check the stricter
        # limit so we fail loud on both, not with a cryptic ENAMETOOLONG.
        if len(encoded) >= 104:
            raise ValueError(f"socket path too long ({len(encoded)} >= 104): {self.socket_path}")

        sock_dir = Path(self.socket_path).parent
        sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        master_fd, slave_fd = pty.openpty()

        winsize = struct.pack('HHHH', self.initial_rows, self.initial_cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # Termios on the slave before fork:
        #   ICANON off  -> xterm's DA query is delivered immediately (else it
        #                  waits for Enter)
        #   ECHOCTL off -> control chars echo as raw bytes, not ^[ caret form
        #                  (needed for DA interception)
        #   ECHO left ON -> interactive prompts (ssh host-key, sudo) show input
        #   OPOST left ON -> \n -> \r\n preserved (no staircase)
        # claude/Ink set raw mode themselves on TUI startup.
        attr = termios.tcgetattr(slave_fd)
        attr[3] &= ~(termios.ICANON | termios.IEXTEN | termios.ECHOCTL | termios.ECHOKE)
        termios.tcsetattr(slave_fd, termios.TCSANOW, attr)

        pid = os.fork()
        if pid == 0:
            # Child: session leader + controlling tty, then exec.
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            if self.cwd:
                os.chdir(self.cwd)
            os.execvpe(self.cmd[0], self.cmd, self.env)
            os._exit(127)

        # Parent.
        os.close(slave_fd)
        self.master_fd = master_fd
        self.child_pid = pid

        # If exec failed the child _exits immediately. Detect BEFORE binding so
        # the launcher sees a dead process, not a transient socket. pidfd_open +
        # select waits for the death event (bounded 100 ms); the success path
        # returns instantly when select times out. On kernels without pidfd_open
        # (macOS), a short sleep + WNOHANG catches the instant-exec-failure case.
        child_died = False
        if hasattr(os, "pidfd_open"):
            try:
                pidfd = os.pidfd_open(pid)  # type: ignore[attr-defined]
                try:
                    readable, _, _ = select.select([pidfd], [], [], 0.1)
                    if readable:
                        os.waitpid(pid, 0)
                        child_died = True
                finally:
                    os.close(pidfd)
            except OSError:
                pass
        else:
            time.sleep(0.05)
        if not child_died:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid != 0:
                    child_died = True
            except ChildProcessError:
                child_died = True
        if child_died:
            self.child_pid = None
            os.close(master_fd)
            self.master_fd = None
            raise RuntimeError(f"child {self.cmd[0]!r} exited immediately (exec likely failed)")

        # Non-blocking master for the select loop.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Bind the Unix socket.
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o700)
        self.server_sock.listen(1)
        self.server_sock.setblocking(False)

        # Atomic PID sidecar (tmp+rename) so a liveness reader never sees a
        # zero-byte file mid-write.
        pid_path = self.socket_path + ".pid"
        tmp_path = pid_path + ".tmp"
        with open(tmp_path, 'w') as f:
            f.write(str(os.getpid()))
        os.rename(tmp_path, pid_path)

        log.info("pty_server up: pid=%d child=%d socket=%s cmd=%s",
                 os.getpid(), pid, self.socket_path, self.cmd)

    # --- event loop ----------------------------------------------------------
    def run(self):
        self.start()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self._shutdown())
            signal.signal(signal.SIGINT, lambda *_: self._shutdown())
        try:
            while self._running:
                self._poll_once()
        finally:
            self._cleanup()

    def _poll_once(self):
        rlist = [self.master_fd, self.server_sock]
        if self.client_sock:
            rlist.append(self.client_sock)
        # Watch the master for writability only while input is pending — a PTY master is
        # almost always writable, so registering it unconditionally would busy-spin.
        wlist = [self.master_fd] if self._pty_write_buf else []
        try:
            readable, writable, _ = select.select(rlist, wlist, [], 0.5)
        except (select.error, OSError) as e:
            if getattr(e, 'errno', None) == errno.EINTR:
                return
            raise
        except ValueError:
            self._running = False
            return

        for fd in readable:
            if fd is self.server_sock:
                self._accept_client()
            elif fd == self.master_fd:
                self._read_pty()
            elif fd is self.client_sock:
                self._read_client()
        if writable:
            self._drain_pty_write_buf()

        # Reap the child (non-blocking).
        if self.child_pid:
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
                if pid != 0:
                    self.child_pid = None
                    self._running = False
                    log.info("child exited: status=%d", status)
            except ChildProcessError:
                self.child_pid = None
                self._running = False

    # --- client I/O ----------------------------------------------------------
    def _accept_client(self):
        try:
            conn, _ = self.server_sock.accept()
        except (BlockingIOError, OSError):
            return
        if self.client_sock:  # single client; replace the old one
            try:
                self.client_sock.close()
            except OSError:
                pass
        self.client_sock = conn
        self.client_sock.setblocking(False)
        # Cap blocking sends so a dead-but-not-RST'd client can't freeze the loop.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack('ll', 10, 0))
        log.info("client connected")
        # Replay recent output so the new client repaints history. Alt-screen
        # enter/exit pairs (and everything between) are stripped — replaying them
        # would strand a fresh xterm in a corrupted alt-screen state; the client
        # follows up with CMD_SNAPSHOT for the authoritative current screen.
        # \x1c-escaped so the client's snapshot-frame detector can't trip on
        # replay bytes.
        if self._replay_buf:
            self._send_to_client(_filter_alt_screen(bytes(self._replay_buf)), framed=True)

    def _send_to_client(self, data: bytes, *, framed: bool) -> bool:
        """Single choke point for all daemon->client sends. `framed` runs data
        through escape_data (\\x1c -> \\x1c\\x1c). Live output + replay are framed;
        the snapshot RESPONSE is framed=False (already length-delimited, its
        payload must stay verbatim). Blocking send so backpressure propagates."""
        if self.client_sock is None:
            return False
        payload = escape_data(data) if framed else data
        try:
            self.client_sock.setblocking(True)
            self.client_sock.sendall(payload)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            try:
                self.client_sock.close()
            except OSError:
                pass
            self.client_sock = None
            return False
        finally:
            if self.client_sock is not None:
                self.client_sock.setblocking(False)

    def _read_client(self):
        try:
            data = self.client_sock.recv(65536)
        except BlockingIOError:
            return
        except (ConnectionResetError, OSError):
            data = b""
        if not data:
            try:
                self.client_sock.close()
            except OSError:
                pass
            self.client_sock = None
            self._pending.clear()   # a new client starts fresh
            log.info("client disconnected")
            return
        self._process_client_input(data)

    def _process_client_input(self, data: bytes):
        """Separate raw input from control escapes; buffer incomplete escapes.

        \\x1c is the escape char. \\x1c\\x01 + 4 bytes = resize. \\x1c\\x02 =
        snapshot request. \\x1c\\x1c = literal \\x1c. Anything else is raw input.

        A lone trailing \\x1c or a partial resize header is stashed in
        self._pending and completed by the next recv() — browser resize on a
        slow network regularly splits the 6-byte frame across boundaries, and
        writing the partial bytes to the PTY corrupts what the user is typing.
        """
        if self._pending:
            buf = bytes(self._pending) + data
            self._pending.clear()
        else:
            buf = data

        i = 0
        while i < len(buf):
            if buf[i] == ESCAPE:
                if i + 1 >= len(buf):
                    # Lone trailing \x1c — might be the head of an escape whose
                    # tail is in the next recv. Buffer; do NOT write to PTY.
                    self._pending.extend(buf[i:])
                    return
                elif buf[i + 1] == CMD_RESIZE:
                    if i + 6 <= len(buf):
                        rows, cols = struct.unpack('>HH', buf[i + 2:i + 6])
                        self._resize(rows, cols)
                        i += 6
                    else:
                        self._pending.extend(buf[i:])   # partial resize header
                        return
                elif buf[i + 1] == CMD_SNAPSHOT:
                    self._send_snapshot_to_client()
                    i += 2
                elif buf[i + 1] == ESCAPE:
                    self._write_pty(bytes([ESCAPE]))    # escaped literal \x1c
                    i += 2
                else:
                    self._write_pty(buf[i:i + 2])       # unknown -> literal
                    i += 2
            else:
                next_esc = buf.find(bytes([ESCAPE]), i)
                if next_esc == -1:
                    self._write_pty(buf[i:])
                    return
                self._write_pty(buf[i:next_esc])
                i = next_esc

    # --- PTY I/O -------------------------------------------------------------
    def _read_pty(self):
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self._running = False
            return
        if not data:
            self._running = False
            return
        data = self._intercept_terminal_queries(data)
        # Ring buffer.
        self._replay_buf.extend(data)
        if len(self._replay_buf) > REPLAY_BUFFER_SIZE:
            del self._replay_buf[:len(self._replay_buf) - REPLAY_BUFFER_SIZE]
        # Forward, \x1c-escaped, blocking (backpressure).
        if self.client_sock:
            self._send_to_client(data, framed=True)

    def _intercept_terminal_queries(self, data: bytes) -> bytes:
        r"""Strip DA queries from output and reply directly on the PTY.

        Handles:  \x1b[c / \x1b[0c  (DA1, Primary Device Attributes)
                  \x1b[>c / \x1b[>0c (DA2, Secondary Device Attributes)

        For each query: (a) write a fake answer back to the PTY master so the
        child sees a normal response, and (b) remove the query from the returned
        bytes so xterm never sees it and never auto-responds. xterm's auto-reply
        would otherwise loop through the child PTY and echo as literal ^[[?1;2c
        on top of claude's prompt — exactly what tmux prevents internally.

        On the FIRST DA1 of a fresh (non-resume) launch we also inject
        \x1b[2J\x1b[H to wipe startup noise before the TUI paints.
        """
        if b"\x1b[" not in data:            # cheap fast-path
            return data
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            if data[i] == 0x1b and i + 1 < n and data[i + 1] == ord("["):
                j = i + 2
                priv = b""
                if j < n and data[j] in (ord(">"), ord("=")):
                    priv = bytes([data[j]])
                    j += 1
                params_start = j
                while j < n and (chr(data[j]).isdigit() or data[j] in (ord(";"), ord(":"))):
                    j += 1
                params = data[params_start:j]
                if j >= n:                  # incomplete at buffer end — pass through
                    out.append(data[i])
                    i += 1
                    continue
                final = data[j]
                # DA1: final 'c', NO private prefix, params empty or "0".
                if final == ord("c") and priv == b"":
                    if params in (b"", b"0"):
                        if not self._first_da_seen:
                            self._first_da_seen = True
                            if self._inject_first_da_clear:
                                out.extend(b"\x1b[2J\x1b[H")
                        self._write_pty(b"\x1b[?1;2c")
                        i = j + 1
                        continue
                # DA2: final 'c', '>' prefix. (41 = "VT420 family", version 0.)
                if final == ord("c") and priv == b">":
                    if params in (b"", b"0"):
                        self._write_pty(b"\x1b[>41;0;0c")
                        i = j + 1
                        continue
                out.extend(data[i:j + 1])   # not a query we intercept
                i = j + 1
            else:
                next_esc = data.find(0x1b, i + 1)
                if next_esc == -1:
                    out.extend(data[i:])
                    break
                out.extend(data[i:next_esc])
                i = next_esc
        return bytes(out)

    # Input the daemon will buffer for a stalled child before dropping the OLDEST bytes.
    # An interactive session never legitimately accumulates this much unread typing; past
    # it, dropping input is strictly better than the alternative (see below).
    PTY_WRITE_BUF_MAX = 65536

    def _write_pty(self, data: bytes):
        """Queue input for the PTY master and drain opportunistically — NEVER block.

        This used to be a BLOCKING write ("so keystrokes are never lost"), which
        deadlocked whole sessions: claude mid-repaint fills the master's OUTPUT
        buffer while input floods the INPUT queue; the daemon parks forever in
        write(), so it stops draining output, so claude stays blocked in ITS
        write and never reads input — each side holding what the other needs
        (observed live: daemon 100% in os.write, claude 100% in write(1), 8 KB
        of keystrokes stranded in the socket Recv-Q, session frozen for good).

        Instead: buffer, attempt a non-blocking drain now, and let _poll_once
        retry via select's write set. Keystrokes still survive backpressure —
        they're just queued in the daemon instead of the kernel."""
        if not data or self.master_fd is None:
            return
        self._pty_write_buf.extend(data)
        overflow = len(self._pty_write_buf) - self.PTY_WRITE_BUF_MAX
        if overflow > 0:
            del self._pty_write_buf[:overflow]
            log.warning("pty input backlog exceeded %d bytes; dropped oldest %d",
                        self.PTY_WRITE_BUF_MAX, overflow)
        self._drain_pty_write_buf()

    def _drain_pty_write_buf(self):
        """Write as much pending input as the (non-blocking) master accepts."""
        while self._pty_write_buf and self.master_fd is not None:
            try:
                n = os.write(self.master_fd, bytes(self._pty_write_buf[:4096]))
            except BlockingIOError:
                return          # full — select's write set will call us back
            except OSError:
                self._pty_write_buf.clear()   # master gone; child-exit path handles the rest
                return
            if n <= 0:
                return
            del self._pty_write_buf[:n]

    # --- resize (TIOCSWINSZ + SIGWINCH to the child pgroup) ------------------
    def _resize(self, rows: int, cols: int):
        if self.master_fd is None:
            return
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            if self.child_pid:
                os.killpg(os.getpgid(self.child_pid), signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    # --- settled snapshot ------------------------------------------------------
    def _settle_replay_buf_for_snapshot(self):
        """Drain PTY output until the replay buffer is quiescent.

        A CMD_SNAPSHOT often arrives right after a CMD_RESIZE. claude/Ink answers
        the SIGWINCH with a redraw; snapshotting before that lands captures a
        mid-render (garbled) frame. We settle first.

        This loop is single-threaded, so a bare sleep would STARVE _read_pty and
        the buffer would never grow (false "instantly quiescent"). Instead each
        tick selects on master_fd and drains available bytes through the NORMAL
        _read_pty path — live redraw bytes still flow to the client AND append to
        _replay_buf (whose length the predicate watches). Bounded + select-driven.
        """
        if self.master_fd is None:
            return
        settle = _SettleState(len(self._replay_buf))
        for _ in range(SNAPSHOT_SETTLE_MAX_POLLS):
            try:
                readable, _, _ = select.select([self.master_fd], [], [], SNAPSHOT_SETTLE_POLL_S)
            except (select.error, OSError):
                break   # fd torn down mid-settle — snapshot what we have
            if readable:
                self._read_pty()
                if not self._running:   # child exited mid-drain
                    return
            settle.observe(len(self._replay_buf))
            if settle.is_quiescent:
                break

    def _force_child_repaint(self):
        """No-op SIGWINCH (no winsize change) nudges Ink to re-render the FULL
        screen — one authoritative frame with a single correctly-placed cursor.
        The byte-tail snapshot can't reconstruct live cursor STATE, so on a
        reconnect during active output the returning xterm would show a stale
        (double) cursor until a real repaint. No winsize change => no reflow."""
        if not self.child_pid:
            return
        try:
            os.killpg(os.getpgid(self.child_pid), signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    def _send_snapshot_to_client(self):
        """Reply to CMD_SNAPSHOT with the settled, alt-screen-aware tail.

        Always sends a frame (even empty) so the client's bounded wait resolves
        promptly on an idle session. Settles first so a snapshot requested right
        after a resize captures the SETTLED post-redraw screen.
        """
        if self.client_sock is None:
            return
        self._settle_replay_buf_for_snapshot()
        if self.client_sock is None:   # settle's _read_pty may have dropped it
            return
        snap = _snapshot_tail(bytes(self._replay_buf))
        if self._send_to_client(encode_snapshot_response(snap), framed=False):
            self._force_child_repaint()

    # --- shutdown ------------------------------------------------------------
    def _shutdown(self):
        self._running = False

    def _cleanup(self):
        """Order matters: SIGTERM the child pgroup BEFORE closing master_fd
        (closing the master delivers SIGHUP, which races SIGTERM)."""
        if self.client_sock:
            try:
                self.client_sock.close()
            except OSError:
                pass
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
        for p in (self.socket_path, self.socket_path + ".pid"):
            try:
                os.unlink(p)
            except (FileNotFoundError, PermissionError):
                pass
        if self.child_pid:
            try:
                os.killpg(os.getpgid(self.child_pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            deadline = time.monotonic() + 2.0
            reaped = False
            while time.monotonic() < deadline:
                try:
                    pid, _ = os.waitpid(self.child_pid, os.WNOHANG)
                    if pid != 0:
                        reaped = True
                        break
                except ChildProcessError:
                    reaped = True
                    break
                time.sleep(0.05)
            if not reaped:
                try:
                    os.killpg(os.getpgid(self.child_pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    os.waitpid(self.child_pid, 0)
                except ChildProcessError:
                    pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        log.info("pty_server cleaned up: socket=%s", self.socket_path)


# --- liveness (used by PtyTerminalSession to decide spawn-vs-reconnect) --------
def is_session_alive(socket_path: str | None) -> bool:
    """True iff a pty_server is running for this socket path. Read-only, safe to
    call from any thread.

    Chain (any failure => False): socket file exists -> PID sidecar readable +
    int -> PID alive -> cmdline is a pty_server (guards against PID reuse).
    """
    if not socket_path or not os.path.exists(socket_path):
        return False
    try:
        with open(socket_path + ".pid") as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return False
    if hasattr(os, "pidfd_open"):
        try:
            fd = os.pidfd_open(pid)  # type: ignore[attr-defined]
            os.close(fd)
        except ProcessLookupError:
            return False
        except OSError:
            return False
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        return not Path("/proc").is_dir()   # no /proc (macOS) -> PID-alive is enough
    except OSError:
        return True
    return b"pty_server" in cmdline


def kill_session(socket_path: str | None):
    """Stop a pty_server by socket path (SIGTERM the pgroup, wait, SIGKILL,
    unlink files). Works whether or not we're the spawning process (uses
    os.kill(pid, 0) polling, not waitpid, since the daemon is start_new_session)."""
    if not socket_path:
        return
    pid_file = socket_path + ".pid"
    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
        # Guard against PID reuse before killing.
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"pty_server" not in cmdline:
                pid = None  # not ours anymore — just clean files below
        except (FileNotFoundError, OSError):
            pass
        if pid is not None:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            terminated = False
            for _ in range(50):     # 5 s
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    terminated = True
                    break
                time.sleep(0.1)
            if not terminated:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
    except (FileNotFoundError, ValueError):
        pass
    for p in (socket_path, pid_file):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass


# --- CLI ----------------------------------------------------------------------
def _main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [pty_server] %(message)s')
    p = argparse.ArgumentParser(description='loom PTY-persistence daemon')
    p.add_argument('socket', help='Unix socket path')
    p.add_argument('--rows', type=int, default=24)
    p.add_argument('--cols', type=int, default=80)
    p.add_argument('--cwd', help='working directory for the child')

    # Split on the first `--` so argparse never sees the wrapped command's own
    # flags (e.g. claude's --resume). Without this the positional matcher
    # mis-routes `--` under nargs.
    try:
        sep = argv.index('--')
        argparse_argv, cmd_argv = argv[:sep], argv[sep + 1:]
    except ValueError:
        argparse_argv, cmd_argv = argv, []
    args = p.parse_args(argparse_argv)
    if not cmd_argv:
        p.error("no command given (expected `... -- <cmd> [args...]`)")

    server = PtyServer(args.socket, cmd_argv, rows=args.rows, cols=args.cols, cwd=args.cwd)
    server.run()


if __name__ == '__main__':
    _main(sys.argv[1:])
