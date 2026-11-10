"""Chat categories — the sidebar's groups, kept server-side so agents can see and change them.

They used to live in the browser's localStorage, which no agent (or second browser) could
read; now `~/.loom/categories.json` is the one source of truth and the dashboard reads/writes
it through the API (it imports any legacy localStorage categories once — see `import_state`).

Shape (same as the old localStorage value, so the import is a straight merge):
    {"categories": [{"id": "cat_…", "name": "Payments", "repo": "kalendir"}, …],   # display order
     "assign": {"<chat id>": "<category id>"}}
Each category belongs to one project (`repo`, a registered repo name); categories made before
projects existed may have none until the sidebar files them. A chat in no category is
"Uncategorized".
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid

from loom.core.config import LOOM_HOME, ensure_dirs

CATEGORIES_PATH = LOOM_HOME / "categories.json"

_lock = threading.Lock()  # read-modify-write: dashboard and agent CLIs mutate concurrently


def _read() -> dict:
    try:
        raw = json.loads(CATEGORIES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    cats = [
        {"id": c["id"], "name": c["name"], **({"repo": c["repo"]} if isinstance(c.get("repo"), str) else {})}
        for c in (raw.get("categories") or [])
        if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("name"), str)
    ]
    ids = {c["id"] for c in cats}
    assign = {k: v for k, v in (raw.get("assign") or {}).items() if isinstance(v, str) and v in ids}
    return {"categories": cats, "assign": assign}


def _write(state: dict) -> None:
    ensure_dirs()
    tmp = CATEGORIES_PATH.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, CATEGORIES_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def new_id() -> str:
    """Same format the dashboard mints (`cat_<base36 ms>_<6 chars>`)."""
    ms = int(time.time() * 1000)
    b36 = ""
    while ms:
        ms, r = divmod(ms, 36)
        b36 = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + b36
    return f"cat_{b36}_{secrets.token_hex(3)}"


def get_state() -> dict:
    return _read()


def find(ref: str, repo: str | None = None) -> dict | None:
    """A category by id, else by case-insensitive name. With `repo`, names only match that
    project's categories (plus unfiled ones) — two projects may each have a "Bugs"."""
    ref = (ref or "").strip()
    if not ref:
        return None
    cats = _read()["categories"]
    for c in cats:
        if c["id"] == ref:
            return c
    low = ref.lower()
    for c in cats:
        if c["name"].lower() == low and (repo is None or c.get("repo") in (None, repo)):
            return c
    return None


def add(name: str, repo: str | None = None, cat_id: str | None = None) -> dict:
    """Make a category. It goes first in the list (so it lands just under Uncategorized in its
    project's sidebar). `cat_id` lets the dashboard mint the id itself and stay optimistic."""
    name = (name or "").strip()
    if not name:
        raise ValueError("category name is empty")
    with _lock:
        state = _read()
        if cat_id and any(c["id"] == cat_id for c in state["categories"]):
            return next(c for c in state["categories"] if c["id"] == cat_id)
        cat = {"id": cat_id or new_id(), "name": name, **({"repo": repo} if repo else {})}
        state["categories"].insert(0, cat)
        _write(state)
        return cat


def find_or_add(ref: str, repo: str | None) -> dict:
    return find(ref, repo) or add(ref, repo)


def update(cat_id: str, *, name: str | None = None, repo: str | None = None) -> dict:
    with _lock:
        state = _read()
        cat = next((c for c in state["categories"] if c["id"] == cat_id), None)
        if cat is None:
            raise KeyError(cat_id)
        if name is not None and name.strip():
            cat["name"] = name.strip()
        if repo is not None:
            cat["repo"] = repo
        _write(state)
        return cat


def remove(cat_id: str) -> None:
    """Drop a category — its chats fall back to Uncategorized."""
    with _lock:
        state = _read()
        state["categories"] = [c for c in state["categories"] if c["id"] != cat_id]
        state["assign"] = {k: v for k, v in state["assign"].items() if v != cat_id}
        _write(state)


def set_order(ids: list[str]) -> None:
    """New order for some categories (one project's, top to bottom). Every other category keeps
    its slot in the global list — the dashboard's drag-to-reorder semantics."""
    with _lock:
        state = _read()
        by_id = {c["id"]: c for c in state["categories"]}
        ordered = [by_id[i] for i in ids if i in by_id]
        moving = {c["id"] for c in ordered}
        queue = iter(ordered)
        state["categories"] = [next(queue) if c["id"] in moving else c for c in state["categories"]]
        _write(state)


def assign(chat_ids: list[str], cat_id: str | None) -> None:
    """File chats under a category, or pass None to move them back to Uncategorized."""
    with _lock:
        state = _read()
        if cat_id is not None and not any(c["id"] == cat_id for c in state["categories"]):
            raise KeyError(cat_id)
        for cid in chat_ids:
            if cat_id is None:
                state["assign"].pop(cid, None)
            else:
                state["assign"][cid] = cat_id
        _write(state)


def category_of(chat_id: str, state: dict | None = None) -> dict | None:
    state = state or _read()
    cid = state["assign"].get(chat_id)
    return next((c for c in state["categories"] if c["id"] == cid), None) if cid else None


def import_state(categories: list[dict], assignments: dict[str, str]) -> dict:
    """One-time merge of a browser's legacy localStorage categories. Idempotent and safe to run
    from several browsers: categories are added by id only if missing (appended in their own
    order), and a chat's assignment is taken only if the server has none for it yet."""
    with _lock:
        state = _read()
        have = {c["id"] for c in state["categories"]}
        for c in categories or []:
            if not (isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("name"), str)):
                continue
            if c["id"] in have or not c["name"].strip():
                continue
            state["categories"].append(
                {"id": c["id"], "name": c["name"].strip(), **({"repo": c["repo"]} if isinstance(c.get("repo"), str) else {})}
            )
            have.add(c["id"])
        for chat_id, cat_id in (assignments or {}).items():
            if isinstance(cat_id, str) and cat_id in have and chat_id not in state["assign"]:
                state["assign"][chat_id] = cat_id
        _write(state)
        return state
