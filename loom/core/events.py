"""Agent lifecycle events — what each chat's agent itself says it is doing.

Claude runs a hook at each lifecycle moment (`agents._claude_settings`): a tiny `curl` POSTs claude's
hook JSON to `/api/chats/{id}/event`, and it lands here (`UserPromptSubmit` arrives through the
context hook instead). That gives loom exact facts instead of inferences from output silence:

    UserPromptSubmit → a turn started        Stop / StopFailure → the turn ended (the agent waits on you)
    Notification     → it needs you (e.g. a permission prompt)       SessionStart / SessionEnd

Used for: the dashboard's desktop notifications (a new Stop/Notification event for an unmuted chat),
`turn` in `/api/terminals` (so a chat is known to be mid-turn even through a long quiet tool run, and
survives loom restarts), and later an attention inbox. Only Claude has these hooks; the older
`needs/<id>` marker files still drive the "needs you" dot and the other agents.

Stored in loom.db, newest `KEEP_PER_CHAT` per chat. Never raises into a hook: callers swallow errors.
"""

from __future__ import annotations

import time

from loom.core import db

KEEP_PER_CHAT = 300
# event → the turn state it implies
_TURN = {
    "UserPromptSubmit": "running",
    "Stop": "waiting",
    "StopFailure": "waiting",
    "Notification": "waiting",
    "SessionStart": "idle",
    "SessionEnd": "ended",
}


def record(chat_id: str, event: str, session_id: str | None = None) -> int | None:
    """Append one event; returns its id. Unknown event names are ignored (None)."""
    if event not in _TURN or not chat_id:
        return None
    with db.connect() as con:
        cur = con.execute(
            "INSERT INTO chat_events (chat_id, ts, event, session_id) VALUES (?,?,?,?)", (chat_id, time.time(), event, session_id)
        )
        eid = int(cur.lastrowid)
        con.execute(
            "DELETE FROM chat_events WHERE chat_id = ? AND id <= (SELECT id FROM chat_events WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (chat_id, chat_id, KEEP_PER_CHAT),
        )
        return eid


def latest_id() -> int:
    with db.connect() as con:
        return int(con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM chat_events").fetchone()["m"])


def since(after_id: int, limit: int = 200) -> list[dict]:
    """Events newer than `after_id`, oldest first (what the dashboard's notifier polls)."""
    with db.connect() as con:
        rows = con.execute(
            "SELECT id, chat_id, ts, event FROM chat_events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        ).fetchall()
    return [{"id": r["id"], "chat_id": r["chat_id"], "ts": r["ts"], "event": r["event"]} for r in rows]


def turn_states() -> dict[str, dict]:
    """{chat id → {"turn": running|waiting|idle|ended, "since": ts, "event": name}} from each chat's
    latest event — one query for every chat."""
    with db.connect() as con:
        rows = con.execute(
            "SELECT chat_id, event, ts FROM chat_events WHERE id IN (SELECT MAX(id) FROM chat_events GROUP BY chat_id)"
        ).fetchall()
    return {r["chat_id"]: {"turn": _TURN.get(r["event"]), "since": r["ts"], "event": r["event"]} for r in rows}
