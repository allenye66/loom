"""loom's SQLite database (`~/.loom/loom.db`) — shared state that needs real transactions.

Today: the per-category message boards (core/board.py), the per-chat "what have I already told
this agent" markers (core/workspace.py), and the agent lifecycle event ledger (core/events.py). loom's older state (chat overlay, categories,
registry) stays in JSON files; they have a single writer (the server) and work fine, and moving
them is a separate, riskier job than adding a new feature on a solid base.

WAL mode, one short-lived autocommit connection per call (cheap, and safe across the server's
threadpool and the CLI's separate processes); writers that must read-then-write take
`BEGIN IMMEDIATE` themselves. Standard library only.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading

from loom.core.config import LOOM_HOME

DB_PATH = LOOM_HOME / "loom.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    board       TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    ts          REAL    NOT NULL,
    author      TEXT    NOT NULL,
    author_name TEXT    NOT NULL,
    recipients  TEXT    NOT NULL DEFAULT '[]',
    text        TEXT    NOT NULL,
    reply_to    INTEGER,
    UNIQUE (board, seq)
);
CREATE TABLE IF NOT EXISTS board_cursors (
    reader TEXT    NOT NULL,
    board  TEXT    NOT NULL,
    seq    INTEGER NOT NULL,
    PRIMARY KEY (reader, board)
);
CREATE TABLE IF NOT EXISTS board_settings (
    board      TEXT PRIMARY KEY,
    enabled    INTEGER NOT NULL DEFAULT 0,
    enabled_at REAL
);
CREATE TABLE IF NOT EXISTS chat_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    ts         REAL NOT NULL,
    event      TEXT NOT NULL,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS chat_events_chat ON chat_events (chat_id, id);
CREATE TABLE IF NOT EXISTS ctx_state (
    key     TEXT PRIMARY KEY,
    ws_hash TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

_init_lock = threading.Lock()
_initialised: set[str] = set()


@contextlib.contextmanager
def connect():
    """A short-lived connection in autocommit mode (we BEGIN explicitly where a write needs it)."""
    path = str(DB_PATH)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        if path not in _initialised:
            with _init_lock:
                con.execute("PRAGMA journal_mode=WAL")
                con.executescript(SCHEMA)
                _initialised.add(path)
        con.execute("PRAGMA synchronous=NORMAL")
        yield con
    finally:
        con.close()
