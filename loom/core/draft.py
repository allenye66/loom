"""Does this chat's input box hold text you typed but didn't send?

loom sees every byte that goes into an agent's terminal (browser keystrokes, pastes, its own `loom chats send`), so it can keep a running
picture of the composer without scraping the screen: printable characters grow it, Backspace / Ctrl+W shrink it, Enter (outside a paste),
Ctrl+C and Ctrl+U clear it; escape sequences (arrow keys, mouse and focus reports, paste markers, Alt+key) are skipped whole. The sidebar
shows a draft marker on a chat you've moved away from, so a half-typed message doesn't get forgotten.

Deliberately separate from the `/clear` guard's line tracker in core/terminals.py (that one answers "what exactly is on the line" and gives
up on cursor keys; this one only answers "is there unsent text", counting characters, so editing with the arrow keys doesn't lose it).
Pure — no I/O — so it's easy to test. Known blind spots: it only knows what was typed through loom since the session was attached (a loom
restart forgets a draft that is still in the box), history recall (↑) isn't counted, and a text-killing shortcut it doesn't model (Ctrl+K)
can leave a stale marker until the next Enter.
"""

from __future__ import annotations

import time

_TAIL_CAP = 400
_PASTE_ON, _PASTE_OFF = b"\x1b[200~", b"\x1b[201~"


class DraftTracker:
    def __init__(self) -> None:
        self.n = 0                      # characters currently in the composer (best estimate)
        self.since = 0.0                # wall-clock when the draft started (0 = no draft)
        self._tail = bytearray()        # the most recent bytes of it, for a preview
        self._carry = b""               # an escape sequence split across two writes
        self._paste = 0                 # inside a bracketed paste

    # --- state ---------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.n > 0 and bool(bytes(self._tail).strip())

    def preview(self, limit: int = 80) -> str:
        """The end of the draft (what you were last typing), single-line."""
        text = bytes(self._tail).decode("utf-8", "ignore").replace("\n", " ").strip()
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    def _reset(self) -> None:
        self.n = 0
        self.since = 0.0
        self._tail.clear()

    def _add(self, b: int) -> None:
        if (b & 0xC0) != 0x80:  # a UTF-8 continuation byte is part of the previous character
            if self.n == 0:
                self.since = time.time()
            self.n += 1
        self._tail.append(b)
        if len(self._tail) > _TAIL_CAP:
            del self._tail[: len(self._tail) - _TAIL_CAP]

    def _drop_char(self) -> bool:
        if not self._tail:
            self.n = max(0, self.n - 1)
            return False
        while self._tail and (self._tail[-1] & 0xC0) == 0x80:
            self._tail.pop()
        if self._tail:
            self._tail.pop()
        self.n = max(0, self.n - 1)
        return True

    def _back(self) -> None:
        if self.n:
            self._drop_char()
        if self.n == 0:
            self._reset()

    def _back_word(self) -> None:
        """Ctrl+W / Alt+Backspace: delete trailing spaces, then the word before them."""
        while self._tail and self._tail[-1] in b" \t\n" and self.n:
            self._drop_char()
        while self._tail and self._tail[-1] not in b" \t\n" and self.n:
            self._drop_char()
        if self.n == 0:
            self._reset()

    # --- input ---------------------------------------------------------------------------
    @staticmethod
    def _esc_end(data: bytes, i: int) -> int:
        """End (exclusive) of the escape sequence starting at data[i] == ESC; -1 if it's cut off at the end of `data`."""
        n = len(data)
        if i + 1 >= n:
            return i + 1  # a lone trailing ESC is the Esc key itself
        c = data[i + 1]
        if c == 0x5B:  # CSI: parameters/intermediates, then one final byte
            k = i + 2
            while k < n and 0x20 <= data[k] <= 0x3F:
                k += 1
            if k >= n:
                return -1
            end = k + 1
            if data[k] == 0x4D and k == i + 2:  # legacy mouse report: ESC [ M + 3 raw bytes
                end = k + 4
                if end > n:
                    return -1
            return end
        if c == 0x4F:  # SS3 (application cursor keys): ESC O + one byte
            return i + 3 if i + 2 < n else -1
        return i + 2  # ESC + one char: Alt+key

    def feed(self, data: bytes) -> None:
        """Account for bytes written into the terminal."""
        data = self._carry + data
        self._carry = b""
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == 0x1B:
                j = self._esc_end(data, i)
                if j == -1:
                    self._carry = data[i:][:64]
                    return
                seq = data[i:j]
                if seq == _PASTE_ON:
                    self._paste += 1
                elif seq == _PASTE_OFF:
                    self._paste = max(0, self._paste - 1)
                elif seq == b"\x1b\x7f" and not self._paste:  # Alt+Backspace
                    self._back_word()
                i = j
                continue
            if b in (0x0D, 0x0A):
                if self._paste:
                    self._add(0x0A)  # a newline inside a paste is part of the draft
                else:
                    self._reset()  # Enter: submitted
                i += 1
                continue
            if not self._paste:
                if b in (0x7F, 0x08):
                    self._back()
                    i += 1
                    continue
                if b in (0x03, 0x15):  # Ctrl+C / Ctrl+U clear the line
                    self._reset()
                    i += 1
                    continue
                if b == 0x17:  # Ctrl+W
                    self._back_word()
                    i += 1
                    continue
            if b >= 0x20 or b == 0x09:
                self._add(b)
            i += 1
