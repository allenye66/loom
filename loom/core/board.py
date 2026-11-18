"""Per-category message boards — a shared, append-only log for the chats (and you) in one category.

Why: agents working a category together (try three designs, then reconvene; an orchestrator
spawns QA chats and collects their reports) need somewhere to leave findings that isn't "type
into each other's input box". A post looks like:

    {"seq": 7, "ts": 1794…, "from": "<chat id>" | "user", "from_name": "…",
     "to": ["<chat id>", …], "text": "…", "reply_to": 3 | null}

A board is **off by default** and the user turns it on, per category (`enable`; there is no way
back for now). Until then agents can't post or read it and are told so.

`seq` counts up from 1 per board (a board is a category id). Reading is cursor-based per reader
(a chat id, or "user" for the dashboard): `unread` = posts after the reader's cursor that they
didn't write. Chats that aren't in a category have no board — a board is a *group* tool, and this
keeps every existing loom workflow exactly as it was.

Storage is SQLite (`~/.loom/loom.db`, WAL) rather than another JSON file: `seq` assignment and
cursor moves are single transactions (so concurrent posters can't collide or lose a write, even
across processes), "posts since N" is an index lookup, and it's the natural home for more shared
state later. Only the standard library; transcripts and loom's other state files are untouched.
"""

from __future__ import annotations

import json
import sqlite3
import time

from loom.core import db

MAX_POST_CHARS = 4000


class BoardError(ValueError):
    pass


def _row(r: sqlite3.Row) -> dict:
    return {
        "seq": r["seq"],
        "ts": r["ts"],
        "from": r["author"],
        "from_name": r["author_name"],
        "to": json.loads(r["recipients"] or "[]"),
        "text": r["text"],
        "reply_to": r["reply_to"],
    }


def _key(key: str) -> str:
    if not key or not all(ch.isalnum() or ch in "_-" for ch in key):
        raise BoardError(f"bad board key {key!r}")
    return key


# --- opt-in: a board exists for a category only once the user has enabled it ----------------
def is_enabled(key: str) -> bool:
    with db.connect() as con:
        r = con.execute("SELECT enabled FROM board_settings WHERE board = ?", (_key(key),)).fetchone()
        return bool(r and r["enabled"])


def enable(key: str) -> dict:
    """Turn a category's board on (idempotent). Deliberately one-way for now."""
    with db.connect() as con:
        con.execute(
            "INSERT INTO board_settings (board, enabled, enabled_at) VALUES (?, 1, ?) "
            "ON CONFLICT(board) DO UPDATE SET enabled = 1, enabled_at = COALESCE(enabled_at, excluded.enabled_at)",
            (_key(key), time.time()),
        )
    return {"key": key, "enabled": True}


def enabled_keys() -> set[str]:
    with db.connect() as con:
        return {r["board"] for r in con.execute("SELECT board FROM board_settings WHERE enabled = 1")}


def last_seq(key: str) -> int:
    with db.connect() as con:
        r = con.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM board_posts WHERE board = ?", (_key(key),)).fetchone()
        return int(r["m"])


def post(key: str, author: str, author_name: str, text: str, *, to: list[str] | None = None, reply_to: int | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise BoardError("empty post")
    if len(text) > MAX_POST_CHARS:
        raise BoardError(f"post is {len(text)} chars; the limit is {MAX_POST_CHARS} — put the detail in a file and link it")
    recipients = json.dumps(list(dict.fromkeys(to or [])))
    ts = time.time()
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")  # take the write lock before reading MAX(seq) → no duplicate seq
        try:
            seq = int(con.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM board_posts WHERE board = ?", (_key(key),)).fetchone()["n"])
            con.execute(
                "INSERT INTO board_posts (board, seq, ts, author, author_name, recipients, text, reply_to) VALUES (?,?,?,?,?,?,?,?)",
                (key, seq, ts, author, author_name, recipients, text, reply_to),
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return {"seq": seq, "ts": ts, "from": author, "from_name": author_name, "to": json.loads(recipients), "text": text, "reply_to": reply_to}


def read(key: str, *, since: int = 0, limit: int | None = None) -> list[dict]:
    """Posts after `since`, oldest first; with `limit`, only the newest `limit` of them."""
    with db.connect() as con:
        rows = con.execute("SELECT * FROM board_posts WHERE board = ? AND seq > ? ORDER BY seq", (_key(key), since)).fetchall()
    posts = [_row(r) for r in rows]
    return posts[-limit:] if limit else posts


# --- per-reader cursors ---------------------------------------------------------------------
def cursor(reader: str, key: str) -> int:
    with db.connect() as con:
        r = con.execute("SELECT seq FROM board_cursors WHERE reader = ? AND board = ?", (reader, _key(key))).fetchone()
        return int(r["seq"]) if r else 0


def mark_seen(reader: str, key: str, seq: int) -> None:
    """Advance (never rewind) a reader's cursor on a board."""
    with db.connect() as con:
        con.execute(
            "INSERT INTO board_cursors (reader, board, seq) VALUES (?,?,?) "
            "ON CONFLICT(reader, board) DO UPDATE SET seq = MAX(seq, excluded.seq)",
            (reader, _key(key), seq),
        )


def unread(key: str, reader: str) -> list[dict]:
    """Posts after `reader`'s cursor that they didn't write (boards are shared reading, so a post
    addressed to someone else still counts — `addressed_to` says which ones are for the reader)."""
    return [p for p in read(key, since=cursor(reader, key)) if p.get("from") != reader]


def addressed_to(post: dict, reader: str) -> bool:
    return reader in (post.get("to") or [])


def summary(key: str, reader: str | None = None) -> dict:
    with db.connect() as con:
        r = con.execute("SELECT COUNT(*) AS n, COALESCE(MAX(seq),0) AS s, MAX(ts) AS t FROM board_posts WHERE board = ?", (_key(key),)).fetchone()
        on = con.execute("SELECT enabled FROM board_settings WHERE board = ?", (key,)).fetchone()
        out = {"key": key, "enabled": bool(on and on["enabled"]), "count": int(r["n"]), "last_seq": int(r["s"]), "last_ts": r["t"]}
        if reader:
            c = con.execute("SELECT seq FROM board_cursors WHERE reader = ? AND board = ?", (reader, key)).fetchone()
            cur = int(c["seq"]) if c else 0
            out["unread"] = int(con.execute(
                "SELECT COUNT(*) AS n FROM board_posts WHERE board = ? AND seq > ? AND author != ?", (key, cur, reader)
            ).fetchone()["n"])
        return out
