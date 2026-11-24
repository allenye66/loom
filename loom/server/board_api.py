"""Message-board + workspace-context endpoints (see core/board.py, core/workspace.py).

    GET  /api/boards                       every category's board: enabled?, counts, last post, unread for ?reader=
    GET  /api/boards/{key}                 posts (?since, ?limit, ?reader + ?unread_only, ?mark)
    POST /api/boards/{key}                 post (author = X-Loom-Chat, else "user"); `to` and `@name` tags in the text nudge those chats
    GET  /api/boards/{key}/wait            long-poll: returns as soon as someone else posts (or times out)
    POST /api/boards/{key}/seen            move a reader's cursor
    POST /api/boards/{key}/enable          turn a category's board on — the USER only (see below)
    GET  /api/chats/{sid}/workspace        the chat's category, siblings, board state (`loom whoami`)
    POST /api/chats/{sid}/prompt-context   the UserPromptSubmit hook: text to attach to this message
    POST /api/chats/{sid}/event            the Stop / Notification / SessionStart / SessionEnd hooks (core/events.py)
    GET  /api/events?since=N · /api/events/stream   new lifecycle events (poll, or server-sent) — the notifier's feed
    GET  /api/categories/{id}/pr-conflicts   open-PR conflicts between a category's agents (diagnostics; agents are told via context)

A board key is a category id. Chats outside any category have no board, and a category's board is
off until the user enables it (post / wait answer 409 with what to do; reads show `enabled: false`).
There is no way to turn it off again for now.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from loom.core import board, categories, events, inbox, radar, terminals, workspace
from loom.core.mentions import mention_tokens, resolve_token
from loom.server.inbox_api import _enrich
from loom.server.api import _chat_cwd

router = APIRouter()

# At most this many nudges from one author to one chat per window — keeps two agents that reply
# to each other's pings from forming a runaway loop (the board itself is unaffected: posts always land).
NUDGE_LIMIT = 5
NUDGE_WINDOW_S = 60.0
_nudges: dict[tuple[str, str], deque] = defaultdict(deque)


def _category(key: str) -> dict:
    cat = next((c for c in categories.get_state()["categories"] if c["id"] == key), None)
    if not cat:
        raise HTTPException(404, f"no board '{key}' (a board is a category id)")
    return cat


def _require_enabled(key: str, cat: dict) -> None:
    if not board.is_enabled(key):
        raise HTTPException(
            409,
            f'the message board for category "{cat["name"]}" isn\'t enabled — only the user can turn it on '
            "(dashboard → the ▤ on the category's header). Until then, message chats directly with `loom chats send`.",
        )


def _require_live(cat: dict) -> None:
    """An archived category's board is kept but read-only — restore the category (or any one of its chats) first."""
    if cat.get("archived"):
        raise HTTPException(
            409,
            f'category "{cat["name"]}" is archived, so its board is read-only. Restore the category (or one of its chats) '
            "to post again.",
        )


def _rate_limited(author: str, target: str) -> bool:
    q = _nudges[(author, target)]
    now = time.monotonic()
    while q and now - q[0] > NUDGE_WINDOW_S:
        q.popleft()
    if len(q) >= NUDGE_LIMIT:
        return True
    q.append(now)
    return False


@router.get("/boards")
def list_boards(reader: str | None = None) -> dict:
    state = categories.get_state()
    members: dict[str, int] = defaultdict(int)
    for k in state["assign"].values():
        members[k] += 1
    out = []
    for c in state["categories"]:
        s = board.summary(c["id"], reader)
        out.append({**s, "name": c["name"], "repo": c.get("repo"), "members": members.get(c["id"], 0)})
    return {"boards": out}


@router.get("/boards/{key}")
def get_board(
    key: str,
    since: int = 0,
    limit: int | None = None,
    reader: str | None = None,
    unread_only: bool = False,
    mark: bool = False,
) -> dict:
    cat = _category(key)
    if not board.is_enabled(key):
        return {"board": {"key": key, "name": cat["name"], "repo": cat.get("repo"), "enabled": False},
                "posts": [], "last_seq": 0, "cursor": None}
    if unread_only:
        if not reader:
            raise HTTPException(400, "unread_only needs ?reader=")
        posts = board.unread(key, reader)
        if limit:
            posts = posts[-limit:]
    else:
        posts = board.read(key, since=since, limit=limit)
    if mark and reader and posts:
        board.mark_seen(reader, key, max(p["seq"] for p in posts))
    return {
        "board": {"key": key, "name": cat["name"], "repo": cat.get("repo"), "enabled": True},
        "posts": posts,
        "last_seq": board.last_seq(key),
        "cursor": board.cursor(reader, key) if reader else None,
    }


class PostIn(BaseModel):
    text: str
    to: list[str] = []  # chat ids to nudge (the post itself is visible to the whole category)
    reply_to: int | None = None
    wake: bool = False  # also start the agent of a stopped (not archived) target, so the tag reaches it now


def _tagged(key: str, text: str, author: str) -> tuple[list[str], list[str]]:
    """(chat ids, unmatched tokens) for the `@name` tags in a post. Only the category's own members can be
    tagged — they're the ones who can read the board — and never the author. Tokens that match nobody (a
    typo, or just an `@decorator` in a code snippet) come back so the poster can be told nothing was sent."""
    members = [c for c in workspace.loom_chats() if c.get("category") == key and not c.get("archived") and c["id"] != author]
    ids: list[str] = []
    missed: list[str] = []
    for tok in mention_tokens(text):
        c = resolve_token(tok, members, prefer_category=key)
        if c:
            if c["id"] not in ids:
                ids.append(c["id"])
        elif len(tok) >= 3:
            missed.append(tok)
    return ids, missed


async def _nudge(entry: dict, cat_name: str, targets: list[str], *, wake: bool = False) -> dict:
    """Type a one-line nudge into each target that's currently running. The content isn't
    repeated: the target's UserPromptSubmit hook attaches the unread posts to that very message
    (and chats without hooks are told to run `loom board read`). A stopped chat isn't launched (the
    user closed it) unless the poster passes `wake` — it sees the post with its next message instead."""
    result: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    for tid in targets:
        if tid == entry["from"]:
            continue
        if _rate_limited(entry["from"], tid):
            result[tid] = "rate-limited (posted, not nudged)"
            continue
        if not wake and not await loop.run_in_executor(None, terminals.process_alive, tid):
            result[tid] = "not running — will see it with their next message (--wake starts it)"
            continue
        cwd = await run_in_threadpool(_chat_cwd, tid)
        text = f'[board · {cat_name}] {entry["from_name"]} posted #{entry["seq"]} → you. Unread posts are attached; if not, run: loom board read'
        try:
            await terminals.send_text(tid, cwd, text, submit=True, force=False)
            result[tid] = "nudged"
        except terminals.ComposerBusy as e:
            result[tid] = f"not nudged ({e}) — will see it with their next message"
        except Exception as e:  # noqa: BLE001
            result[tid] = f"not nudged ({e})"
    return result


@router.post("/boards/{key}")
async def post_to_board(key: str, body: PostIn, x_loom_chat: str | None = Header(None)) -> dict:
    cat = _category(key)
    _require_live(cat)
    _require_enabled(key, cat)
    author = x_loom_chat or "user"
    # Stored as "user" (the human), not "you": agents read these names, and to an agent "you" is itself.
    # The dashboard renders author "user" as "you" on its own.
    name = "user" if author == "user" else await run_in_threadpool(workspace.chat_label, author)
    # `@name` tags in the text notify exactly like `--to`: the tagged agent is nudged and the post is
    # addressed to it (so its digest marks it "→ you").
    tagged, unmatched = await run_in_threadpool(_tagged, key, body.text, author)
    to = list(dict.fromkeys([*body.to, *tagged]))
    try:
        entry = await run_in_threadpool(
            lambda: board.post(key, author, name, body.text, to=to, reply_to=body.reply_to)
        )
    except board.BoardError as e:
        raise HTTPException(400, str(e)) from e
    nudged = await _nudge(entry, cat["name"], to, wake=body.wake) if to else {}
    return {"post": entry, "nudged": nudged, "tagged": tagged, "unmatched_tags": unmatched}


@router.get("/boards/{key}/wait")
async def wait_for_posts(
    key: str, since: int | None = None, timeout: float = 100.0, reader: str | None = None, mark: bool = True,
) -> dict:
    """Block until a post from someone other than `reader` lands after `since` (default: after the
    reader's cursor), or `timeout` seconds pass. Lets an orchestrator wait for its workers in one
    tool call instead of polling."""
    _require_enabled(key, _category(key))
    timeout = max(0.0, min(timeout, 600.0))
    start = since if since is not None else (board.cursor(reader, key) if reader else board.last_seq(key))
    deadline = time.monotonic() + timeout
    while True:
        posts = [p for p in board.read(key, since=start) if p.get("from") != reader]
        if posts or time.monotonic() >= deadline:
            if posts and mark and reader:
                board.mark_seen(reader, key, max(p["seq"] for p in posts))
            return {"posts": posts, "timed_out": not posts, "last_seq": board.last_seq(key)}
        await asyncio.sleep(0.4)


@router.post("/boards/{key}/enable")
def enable_board(key: str, x_loom_chat: str | None = Header(None)) -> dict:
    """Turn a category's board on. Only the user does this (dashboard, or `loom board enable` in a
    plain shell): a request that carries an agent's `X-Loom-Chat` is refused — whether a group of
    chats shares a channel is the user's call, not theirs. Idempotent; no off switch for now."""
    cat = _category(key)
    if x_loom_chat:
        raise HTTPException(403, "only the user can enable a message board (it's the ▤ on the category in the dashboard)")
    _require_live(cat)
    out = board.enable(key)
    return {**out, "name": cat["name"]}


class SeenIn(BaseModel):
    reader: str
    seq: int | None = None  # default: everything up to now


@router.post("/boards/{key}/seen")
def mark_seen(key: str, body: SeenIn) -> dict:
    _category(key)
    board.mark_seen(body.reader, key, body.seq if body.seq is not None else board.last_seq(key))
    return {"cursor": board.cursor(body.reader, key)}


@router.get("/chats/{sid}/workspace")
def chat_workspace(sid: str) -> dict:
    return {"workspace": workspace.workspace_info(sid)}


@router.post("/chats/{sid}/prompt-context", response_class=PlainTextResponse)
async def prompt_context(sid: str, request: Request) -> str:
    """The UserPromptSubmit hook's body is claude's hook JSON (`prompt`, `session_id`, …).
    Best-effort by design: any failure returns "" (attach nothing) rather than disturb the turn."""
    raw = await request.body()
    prompt, session_id = "", None
    with contextlib.suppress(Exception):
        data = json.loads(raw or b"{}")
        prompt, session_id = str(data.get("prompt") or ""), data.get("session_id")
    with contextlib.suppress(Exception):
        await run_in_threadpool(events.record, sid, "UserPromptSubmit", session_id)
    try:
        return await run_in_threadpool(lambda: workspace.prompt_context(sid, prompt, session_id=session_id))
    except Exception:  # noqa: BLE001
        return ""


@router.post("/chats/{sid}/event", response_class=PlainTextResponse)
async def chat_event(sid: str, request: Request) -> str:
    """A lifecycle hook (`hook_event_name` in claude's hook JSON) → the event ledger. Always answers
    with an empty body: a SessionStart hook's stdout is injected into the conversation."""
    with contextlib.suppress(Exception):
        data = json.loads(await request.body() or b"{}")
        await run_in_threadpool(events.record, sid, str(data.get("hook_event_name") or ""), data.get("session_id"))
    return ""


@router.get("/events")
def events_feed(since: int | None = None, limit: int = 200) -> dict:
    """New lifecycle events after `since` (omit it to just learn the newest id — a client's baseline)."""
    latest = events.latest_id()
    if since is None:
        return {"events": [], "latest": latest}
    return {"events": events.since(since, max(1, min(limit, 500))), "latest": latest}


@router.get("/events/stream")
async def events_stream(since: int | None = None):
    """Server-sent events: each new lifecycle event the moment it lands (a background tab's timers are
    throttled; a network stream isn't). First message `hello` carries the newest id (the baseline)."""
    async def gen():
        last = since if since is not None else await run_in_threadpool(events.latest_id)
        last_in = await run_in_threadpool(inbox.latest_id)
        yield f"event: hello\ndata: {json.dumps({'latest': last})}\n\n"
        quiet = 0
        while True:
            evs = await run_in_threadpool(events.since, last, 200)
            new_in = await run_in_threadpool(inbox.since, last_in, 50)  # notifications agents just pushed
            if evs:
                last = evs[-1]["id"]
                yield f"data: {json.dumps({'events': evs})}\n\n"
            if new_in:
                last_in = new_in[-1]["id"]
                yield f"data: {json.dumps({'inbox': await run_in_threadpool(_enrich, new_in)})}\n\n"
            if evs or new_in:
                quiet = 0
            else:
                quiet += 1
                if quiet % 15 == 0:
                    yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/categories/{key}/pr-conflicts")
async def category_pr_conflicts(key: str, refresh: bool = False) -> dict:
    """Diagnostics only (no dashboard UI — D24/D28): the open-PR conflicts agents in this category are told
    about through their per-message context."""
    _category(key)
    r = await run_in_threadpool(lambda: radar.get(key, max_age=0 if refresh else radar.TTL_S))
    return r or {"key": key, "category": _category(key)["name"], "open_prs": [], "conflicts": [], "base_conflicts": [], "computed_at": None}
