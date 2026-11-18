"""Message-board + workspace-context endpoints (see core/board.py, core/workspace.py).

    GET  /api/boards                       every category's board: counts, last post, unread for ?reader=
    GET  /api/boards/{key}                 posts (?since, ?limit, ?reader + ?unread_only, ?mark)
    POST /api/boards/{key}                 post (author = X-Loom-Chat, else "user"); `to` nudges those chats
    GET  /api/boards/{key}/wait            long-poll: returns as soon as someone else posts (or times out)
    POST /api/boards/{key}/seen            move a reader's cursor
    GET  /api/chats/{sid}/workspace        the chat's category, siblings, board state (`loom whoami`)
    POST /api/chats/{sid}/prompt-context   the UserPromptSubmit hook: text to attach to this message

A board key is a category id. Chats outside any category have no board.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from loom.core import board, categories, terminals, workspace
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
        "board": {"key": key, "name": cat["name"], "repo": cat.get("repo")},
        "posts": posts,
        "last_seq": board.last_seq(key),
        "cursor": board.cursor(reader, key) if reader else None,
    }


class PostIn(BaseModel):
    text: str
    to: list[str] = []  # chat ids to nudge (the post itself is visible to the whole category)
    reply_to: int | None = None


async def _nudge(entry: dict, cat_name: str, targets: list[str]) -> dict:
    """Type a one-line nudge into each target that's currently running. The content isn't
    repeated: the target's UserPromptSubmit hook attaches the unread posts to that very message
    (and chats without hooks are told to run `loom board read`). Never launches a stopped chat —
    it will see the post with its next message instead."""
    result: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    for tid in targets:
        if tid == entry["from"]:
            continue
        if _rate_limited(entry["from"], tid):
            result[tid] = "rate-limited (posted, not nudged)"
            continue
        if not await loop.run_in_executor(None, terminals.process_alive, tid):
            result[tid] = "not running — will see it with their next message"
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
    author = x_loom_chat or "user"
    # Stored as "user" (the human), not "you": agents read these names, and to an agent "you" is itself.
    # The dashboard renders author "user" as "you" on its own.
    name = "user" if author == "user" else await run_in_threadpool(workspace.chat_label, author)
    try:
        entry = await run_in_threadpool(
            lambda: board.post(key, author, name, body.text, to=body.to, reply_to=body.reply_to)
        )
    except board.BoardError as e:
        raise HTTPException(400, str(e)) from e
    nudged = await _nudge(entry, cat["name"], body.to) if body.to else {}
    return {"post": entry, "nudged": nudged}


@router.get("/boards/{key}/wait")
async def wait_for_posts(
    key: str, since: int | None = None, timeout: float = 100.0, reader: str | None = None, mark: bool = True,
) -> dict:
    """Block until a post from someone other than `reader` lands after `since` (default: after the
    reader's cursor), or `timeout` seconds pass. Lets an orchestrator wait for its workers in one
    tool call instead of polling."""
    _category(key)
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
    try:
        return await run_in_threadpool(lambda: workspace.prompt_context(sid, prompt, session_id=session_id))
    except Exception:  # noqa: BLE001
        return ""
