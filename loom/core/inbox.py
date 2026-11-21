"""The user's inbox — notifications an AGENT decides to push.

An agent that needs the user's decision, approval or information to proceed runs `loom notify "<question>"`
and ends its turn. That lands here, pops a desktop notification (if the user enabled them and the chat
isn't muted) and shows in the dashboard's notification panel, where the user can reply inline or jump to
the chat. Nothing is inferred from "the turn ended" — the agent says when it needs a human.

An item is `open` until it is `answered` (the user replied from the panel, or typed in the chat — the
UserPromptSubmit hook notices, core/workspace.py) or `dismissed`. When the user answers, the agent is told
which question the message answers: a panel reply is prefixed with it, and a reply typed in the chat gets a
context note — so the agent never has to guess what a bare "yes" refers to.

SQLite (`inbox` in loom.db). Guards against a looping agent: one open item per identical text, and a cap on
how many open items one chat may have.
"""

from __future__ import annotations

import time

from loom.core import db

OPEN_CAP = 5       # open items one chat may have at once
MAX_TEXT = 2000


class TooMany(Exception):
    pass


def _row(r) -> dict:
    return {k: r[k] for k in ("id", "chat_id", "text", "created", "status", "resolved", "resolved_via", "reply")}


def push(chat_id: str, text: str) -> tuple[dict, bool]:
    """(item, created). An identical open item is returned as-is (created=False); too many open → TooMany."""
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        raise ValueError("empty notification")
    with db.connect() as con:
        dup = con.execute("SELECT * FROM inbox WHERE chat_id=? AND status='open' AND text=?", (chat_id, text)).fetchone()
        if dup:
            return _row(dup), False
        n = con.execute("SELECT COUNT(*) AS n FROM inbox WHERE chat_id=? AND status='open'", (chat_id,)).fetchone()["n"]
        if n >= OPEN_CAP:
            raise TooMany(f"you already have {n} unanswered notifications to the user — wait for them to answer")
        cur = con.execute("INSERT INTO inbox (chat_id, text, created) VALUES (?,?,?)", (chat_id, text, time.time()))
        return _row(con.execute("SELECT * FROM inbox WHERE id=?", (cur.lastrowid,)).fetchone()), True


def get(item_id: int) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
    return _row(r) if r else None


def list_items(status: str = "open", limit: int = 200) -> list[dict]:
    with db.connect() as con:
        if status == "all":
            rows = con.execute("SELECT * FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM inbox WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
    return [_row(r) for r in rows]


def open_for_chat(chat_id: str) -> list[dict]:
    with db.connect() as con:
        rows = con.execute("SELECT * FROM inbox WHERE chat_id=? AND status='open' ORDER BY id", (chat_id,)).fetchall()
    return [_row(r) for r in rows]


def mark(item_id: int, status: str, via: str | None = None, reply: str | None = None) -> dict | None:
    """Set an item's status ('open' reopens it — used when a panel reply couldn't be delivered)."""
    with db.connect() as con:
        if status == "open":
            con.execute("UPDATE inbox SET status='open', resolved=NULL, resolved_via=NULL, reply=NULL WHERE id=?", (item_id,))
        else:
            con.execute("UPDATE inbox SET status=?, resolved=?, resolved_via=?, reply=? WHERE id=? AND status='open'",
                        (status, time.time(), via, reply, item_id))
    return get(item_id)


def resolve_chat(chat_id: str, via: str) -> list[dict]:
    """Mark all of a chat's open items answered (the user answered in the chat itself). Returns them."""
    items = open_for_chat(chat_id)
    for it in items:
        mark(it["id"], "answered", via)
    return items


def dismiss_all(chat_id: str | None = None) -> int:
    with db.connect() as con:
        if chat_id:
            cur = con.execute("UPDATE inbox SET status='dismissed', resolved=?, resolved_via='dismiss' WHERE status='open' AND chat_id=?", (time.time(), chat_id))
        else:
            cur = con.execute("UPDATE inbox SET status='dismissed', resolved=?, resolved_via='dismiss' WHERE status='open'", (time.time(),))
        return cur.rowcount


def latest_id() -> int:
    with db.connect() as con:
        return int(con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM inbox").fetchone()["m"])


def since(after_id: int, limit: int = 100) -> list[dict]:
    """Items created after `after_id` (the live stream's feed)."""
    with db.connect() as con:
        rows = con.execute("SELECT * FROM inbox WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
    return [_row(r) for r in rows]
