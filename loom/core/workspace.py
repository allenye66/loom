"""What a chat is told about its loom workspace — delivered per message, only when it matters.

Claude chats run a `UserPromptSubmit` hook (core/agents.py `_claude_settings`) that POSTs the
submitted prompt to `/api/chats/{id}/prompt-context`; whatever this module returns is added to
the agent's context for that turn, and an empty string adds nothing. So a chat that has nothing
new costs **zero tokens**, and nothing stale is baked into a system prompt. It returns, at most:

1. a **workspace block** — which category the chat is in and who else is in it — the first time
   (per Claude session), and again only when that roster/name changes;
2. the **unread board posts** for the chat's category (and marks them read, since they're now in
   its context);
3. **resolved @mentions** — `@some-chat` in the user's message → which chat that is and how to
   reach it. Only real chats resolve, so claude's own `@file` mentions pass through untouched.

Chats outside any category get (1) and (2) never; they only get (3) if they use an `@`.
"""

from __future__ import annotations

import hashlib
import time

from loom.core import board, categories, db, manager, sessions
from loom.core.mentions import mention_tokens, resolve_token, slugify

MAX_SIBLINGS = 12
MAX_DIGEST_POSTS = 8
MAX_POST_PREVIEW = 600


def title_of(c: dict) -> str:
    return (c.get("name") or c.get("display_title") or c["id"][:8]).strip()


def chat_label(sid: str) -> str:
    """A display name for a chat id without building the whole chat list (used when posting)."""
    ov = sessions.get_overlay(sid)
    if ov.get("name"):
        return ov["name"]
    chat = sessions.get_chat(sid) or {}
    if chat.get("title"):
        return chat["title"]
    task = None
    try:
        task = manager.task_for_chat(sid)
    except Exception:  # noqa: BLE001
        pass
    return (task.branch if task else None) or sid[:8]


def _ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return f"{int(d // sec)}{unit} ago"
    return "just now"


def loom_chats(state: dict | None = None, *, include_archived: bool = False) -> list[dict]:
    """loom's task chats (what the sidebar shows) with their category id attached."""
    state = state or categories.get_state()
    rows = sessions.list_chats(scope="all", tab="all" if include_archived else "active")
    out = []
    for c in rows:
        if not c.get("task"):
            continue
        out.append({**c, "category": state["assign"].get(c["id"])})
    return out


def siblings_of(chat_id: str, cat_id: str, chats: list[dict]) -> list[dict]:
    return [c for c in chats if c.get("category") == cat_id and c["id"] != chat_id and not c.get("archived")]


def workspace_info(chat_id: str) -> dict | None:
    """For `loom whoami`: the chat's category, its siblings, and its board's state. None if the
    chat isn't in a category."""
    state = categories.get_state()
    cat = categories.category_of(chat_id, state)
    if not cat:
        return None
    sibs = siblings_of(chat_id, cat["id"], loom_chats(state))
    s = board.summary(cat["id"], chat_id)
    return {
        "category": {"id": cat["id"], "name": cat["name"], "repo": cat.get("repo")},
        "siblings": [{"id": c["id"], "name": title_of(c), "branch": c.get("branch"), "agent": c.get("agent")} for c in sibs],
        "board": {"posts": s["count"], "unread": s.get("unread", 0), "last_seq": s["last_seq"]},
    }


# --- the three pieces of prompt context ------------------------------------------------------
def _workspace_block(me_name: str, chat_id: str, cat: dict, sibs: list[dict]) -> str:
    shown = sibs[:MAX_SIBLINGS]
    roster = "; ".join(f"{title_of(c)} ({c['id'][:8]})" for c in shown) or "no one else yet"
    more = f" (+{len(sibs) - len(shown)} more)" if len(sibs) > len(shown) else ""
    return (
        f'You are "{me_name}" (chat {chat_id[:8]}) in loom category "{cat["name"]}"'
        f'{" · project " + cat["repo"] if cat.get("repo") else ""}. Others here: {roster}{more}.\n'
        f"This category has a shared message board — `loom board read|post|wait` (post findings, decisions and handoffs "
        f"there; unread posts are attached to your next user message). Message one chat directly with "
        f'`loom chats send <id> "…"`. The user may @mention chats in a message; resolved mentions are listed with it.'
    )


def _digest(cat: dict, posts: list[dict], me: str) -> str:
    shown = posts[-MAX_DIGEST_POSTS:]
    lines = [f'Board "{cat["name"]}" — {len(posts)} new post{"s" if len(posts) != 1 else ""} since you last looked:']
    if len(posts) > len(shown):
        lines.append(f"  (… {len(posts) - len(shown)} older unread omitted — `loom board read --all` shows everything)")
    for p in shown:
        who = p.get("from_name") or p.get("from", "?")
        direct = " → you" if board.addressed_to(p, me) else (" → " + ", ".join(t[:8] for t in p["to"]) if p.get("to") else "")
        reply = f" (re #{p['reply_to']})" if p.get("reply_to") else ""
        text = " ".join((p.get("text") or "").split())
        if len(text) > MAX_POST_PREVIEW:
            text = text[: MAX_POST_PREVIEW - 1] + f"… [truncated — `loom board read --seq {p['seq']}`]"
        lines.append(f"  #{p['seq']} {who}{direct}{reply} · {_ago(p['ts'])}: {text}")
    return "\n".join(lines)


def _mentions_block(tokens: list[str], chats: list[dict], me: str, prefer_cat: str | None, cats: dict[str, str]) -> str:
    lines = []
    for tok in tokens:
        c = resolve_token(tok, [x for x in chats if x["id"] != me], prefer_category=prefer_cat)
        if not c:
            continue
        cname = cats.get(c.get("category") or "", None)
        where = ", ".join(x for x in (f"category {cname}" if cname else None, f"project {c.get('repo')}" if c.get("repo") else None) if x)
        lines.append(f'  @{tok} = "{title_of(c)}" (chat {c["id"][:8]}{" · " + where if where else ""})')
    if not lines:
        return ""
    return (
        "Mentioned loom chats:\n" + "\n".join(lines) +
        "\n  (reach one with `loom chats send <id> \"…\"`, read it with `loom chats show <id>`, or post to the board with `--to <id>`)"
    )


def _ws_changed(key: str, sig: str) -> bool:
    h = hashlib.sha1(sig.encode()).hexdigest()[:16]
    with db.connect() as con:
        r = con.execute("SELECT ws_hash FROM ctx_state WHERE key = ?", (key,)).fetchone()
        if r and r["ws_hash"] == h:
            return False
        con.execute(
            "INSERT INTO ctx_state (key, ws_hash, updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET ws_hash = excluded.ws_hash, updated = excluded.updated",
            (key, h, time.time()),
        )
        return True


def prompt_context(chat_id: str, prompt: str, *, session_id: str | None = None) -> str:
    """The context to attach to one user message for `chat_id` ("" = attach nothing)."""
    state = categories.get_state()
    cat = categories.category_of(chat_id, state)
    tokens = mention_tokens(prompt)
    if not cat and not tokens:
        return ""  # the common case for an uncategorised chat: free

    chats = loom_chats(state, include_archived=bool(tokens))
    me_row = next((c for c in chats if c["id"] == chat_id), None)
    me_name = title_of(me_row) if me_row else chat_label(chat_id)
    parts: list[str] = []

    if cat:
        sibs = siblings_of(chat_id, cat["id"], chats if not tokens else [c for c in chats if not c.get("archived")])
        sig = "|".join([cat["id"], cat["name"], me_name, *sorted(f"{c['id']}:{title_of(c)}" for c in sibs)])
        if _ws_changed(f"{chat_id}:{session_id or '-'}", sig):
            parts.append(_workspace_block(me_name, chat_id, cat, sibs))
        posts = board.unread(cat["id"], chat_id)
        if posts:
            parts.append(_digest(cat, posts, chat_id))
            board.mark_seen(chat_id, cat["id"], posts[-1]["seq"])

    if tokens:
        names = {c["id"]: c["name"] for c in state["categories"]}
        m = _mentions_block(tokens, chats, chat_id, cat["id"] if cat else None, names)
        if m:
            parts.append(m)

    if not parts:
        return ""
    return "<loom-context>\n" + "\n\n".join(parts) + "\n</loom-context>"


def mention_slug(name: str) -> str:
    """What the `@@` picker types for a chat (see core/mentions.py)."""
    return slugify(name)
