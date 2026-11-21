"""The user's inbox of agent-pushed notifications (core/inbox.py, D28).

    POST /api/chats/{sid}/notify     an agent pushes a question for the user (`loom notify`)
    GET  /api/inbox                  open items, enriched with the chat's name / project / category (?status=all)
    POST /api/inbox/{id}/reply       the user's answer — typed into that chat, marked as the reply to this question
    POST /api/inbox/{id}/dismiss     drop it without answering
    POST /api/inbox/dismiss-all      clear every open item (?chat= limits it to one chat)
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from loom.core import categories, inbox, sessions, terminals
from loom.server.api import _chat_cwd, _known_chat

router = APIRouter()


class NotifyIn(BaseModel):
    text: str


class ReplyIn(BaseModel):
    text: str
    force: bool = False  # type it even over an unsent draft in the chat's input box


def _enrich(items: list[dict]) -> list[dict]:
    """Attach what the panel shows: the chat's name, project, category, agent, muted/archived."""
    if not items:
        return []
    state = categories.get_state()
    cat_names = {c["id"]: c["name"] for c in state["categories"]}
    chats = {c["id"]: c for c in sessions.list_chats(scope="all", tab="all")}
    out = []
    for it in items:
        c = chats.get(it["chat_id"]) or {}
        cid = state["assign"].get(it["chat_id"])
        out.append({
            **it,
            "chat_name": c.get("name") or c.get("display_title") or it["chat_id"][:8],
            "repo": c.get("repo"),
            "category": cat_names.get(cid) if cid else None,
            "agent": c.get("agent"),
            "muted": bool(c.get("muted")),
            "archived": bool(c.get("archived")),
            "cwd": c.get("cwd"),
            "mode": c.get("mode"),
        })
    return out


@router.post("/chats/{sid}/notify")
async def notify(sid: str, body: NotifyIn) -> dict:
    """An agent pushes a notification: it needs the user's decision/approval/information."""
    sid = sessions.canonical_id(sid)
    await run_in_threadpool(_known_chat, sid)
    try:
        item, created = await run_in_threadpool(inbox.push, sid, body.text)
    except inbox.TooMany as e:
        raise HTTPException(429, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"id": item["id"], "created": created}


@router.get("/inbox")
async def list_inbox(status: str = "open") -> dict:
    items = await run_in_threadpool(inbox.list_items, status)
    return {"items": await run_in_threadpool(_enrich, items)}


@router.post("/inbox/{item_id}/reply")
async def reply(item_id: int, body: ReplyIn) -> dict:
    """Answer a notification from the panel: the text is typed into that chat (resuming its agent if
    needed), prefixed with the question it answers so the agent knows what it's replying to."""
    it = await run_in_threadpool(inbox.get, item_id)
    if not it:
        raise HTTPException(404, "no such notification")
    if not body.text.strip():
        raise HTTPException(400, "empty reply")
    if it["status"] != "open":
        raise HTTPException(409, f"already {it['status']}")
    sid = it["chat_id"]
    cwd = await run_in_threadpool(_chat_cwd, sid)
    q = " ".join(it["text"].split())
    msg = f'[reply to your notification — "{q[:300]}{"…" if len(q) > 300 else ""}"]\n{body.text.strip()}'
    # Mark it answered BEFORE typing: the chat's UserPromptSubmit hook would otherwise see an open item and
    # add a second "the user is replying to…" note on top of this prefix. Undone if the text can't be typed.
    await run_in_threadpool(inbox.mark, item_id, "answered", "panel", body.text.strip())
    try:
        out = await terminals.send_text(sid, cwd, msg, submit=True, force=body.force)
    except terminals.ComposerBusy as e:
        await run_in_threadpool(inbox.mark, item_id, "open")
        raise HTTPException(409, str(e)) from e
    except Exception:
        await run_in_threadpool(inbox.mark, item_id, "open")
        raise
    return {"ok": True, "chat_id": sid, **out}


@router.post("/inbox/{item_id}/dismiss")
async def dismiss(item_id: int) -> dict:
    it = await run_in_threadpool(inbox.get, item_id)
    if not it:
        raise HTTPException(404, "no such notification")
    await run_in_threadpool(inbox.mark, item_id, "dismissed", "dismiss")
    return {"ok": True}


@router.post("/inbox/dismiss-all")
async def dismiss_all(chat: str | None = None) -> dict:
    return {"dismissed": await run_in_threadpool(inbox.dismiss_all, sessions.canonical_id(chat) if chat else None)}
