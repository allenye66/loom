"""Chat/session index over Claude Code transcripts + a local organization overlay.

Claude writes append-only transcripts to ~/.claude/projects/<slug>/<id>.jsonl with
ai-title / last-prompt / gitBranch / pr-link records. loom READS those (never edits)
to build a fast, cached index, and keeps its OWN overlay (~/.loom/chats.json) for
user-controlled state: star / archive / hide / name / tags / description.

Delete = soft-trash (move the file to ~/.loom/trash, restorable).
Search depth = metadata + prompts (title, branch, PR, tags, description, first/last
prompt) — not full message bodies.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from loom.core import registry
from loom.core import repos as repos_mod
from loom.core.config import LOOM_HOME, ensure_dirs

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
TRASH_DIR = LOOM_HOME / "trash"
OVERLAY_PATH = LOOM_HOME / "chats.json"
INDEX_CACHE = LOOM_HOME / "sessions_index.json"


# --- small json helpers -------------------------------------------------------
def _read_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _write_json(path: Path, data) -> None:
    ensure_dirs()
    # Unique temp per writer: FastAPI serves these reads from a threadpool, so several
    # build_index() calls can write the cache at once. A shared "<name>.tmp" lets one
    # os.replace rename it away before another's runs → FileNotFoundError.
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --- overlay (user-owned state) ----------------------------------------------
def overlay_all() -> dict:
    return _read_json(OVERLAY_PATH, {})


def get_overlay(sid: str) -> dict:
    return overlay_all().get(sid, {})


def set_overlay(sid: str, patch: dict) -> dict:
    data = overlay_all()
    cur = data.get(sid, {})
    cur.update(patch)
    data[sid] = cur
    _write_json(OVERLAY_PATH, data)
    return cur


# --- transcript parsing (metadata + prompts only) -----------------------------
def _extract_text(message) -> str | None:
    if not message:
        return None
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content[:240]
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return (block.get("text") or "")[:240]
            if isinstance(block, str):
                return block[:240]
    return None


def _pr_from_record(rec: dict):
    for key in ("prNumber", "pr", "number"):
        if rec.get(key):
            return rec[key]
    url = rec.get("url") or rec.get("prUrl") or ""
    if "/pull/" in str(url):
        tail = str(url).rstrip("/").split("/pull/")[-1].split("/")[0]
        if tail.isdigit():
            return int(tail)
    return None


def _parse_transcript(path: Path) -> dict:
    title = preview = first_prompt = branch = cwd = created = pr_repo = None
    prs: set = set()
    n_user = n_assistant = 0
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t == "ai-title":
                    title = rec.get("aiTitle") or title
                elif t == "last-prompt":
                    preview = rec.get("lastPrompt") or preview
                elif t == "pr-link":
                    pr = _pr_from_record(rec)
                    if pr:
                        prs.add(pr)
                    if rec.get("prRepository"):
                        pr_repo = rec["prRepository"]
                elif t in ("user", "assistant"):
                    if t == "user":
                        n_user += 1
                        if first_prompt is None:
                            first_prompt = _extract_text(rec.get("message"))
                    else:
                        n_assistant += 1
                    if cwd is None and rec.get("cwd"):
                        cwd = rec.get("cwd")
                    if rec.get("gitBranch"):
                        branch = rec["gitBranch"]
                    if created is None and rec.get("timestamp"):
                        created = rec["timestamp"]
    except OSError:
        pass
    st = path.stat()
    return {
        "id": path.stem,
        "title": title,
        "preview": preview,
        "first_prompt": first_prompt,
        "branch": branch,
        "cwd": cwd,
        "prs": sorted(prs, key=str),
        "pr_repo": pr_repo,
        "created": created,
        "last_active": st.st_mtime,
        "size": st.st_size,
        "n_user": n_user,
        "n_assistant": n_assistant,
    }


def build_index(force: bool = False) -> list[dict]:
    """Re-parse only transcripts whose size/mtime changed; cache the rest."""
    cache = _read_json(INDEX_CACHE, {})
    out: dict[str, dict] = {}
    changed = force
    if CLAUDE_PROJECTS.exists():
        for proj in CLAUDE_PROJECTS.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                sid = f.stem
                st = f.stat()
                cached = cache.get(sid)
                unchanged = (
                    cached
                    and not force
                    and cached.get("size") == st.st_size
                    and abs(cached.get("last_active", 0) - st.st_mtime) < 0.001
                )
                if unchanged:
                    out[sid] = cached
                else:
                    out[sid] = _parse_transcript(f)
                    changed = True
    # Only rewrite the cache when a transcript was added / removed / re-parsed. Under the
    # sidebar's polling this is usually a no-op, which avoids a concurrent-write storm.
    if changed or out.keys() != cache.keys():
        _write_json(INDEX_CACHE, out)
    return list(out.values())


# --- repo / task linkage ------------------------------------------------------
def _repo_task_for_cwd(cwd: str | None) -> tuple[str | None, str | None]:
    if not cwd:
        return None, None
    for t in registry.list_tasks():
        wp = t.worktree_path.rstrip("/")
        if cwd == wp or cwd.startswith(wp + "/"):
            return t.repo, t.id
    for r in repos_mod.list_repos():
        root = r["root"].rstrip("/")
        if cwd == root or cwd.startswith(root + "/"):
            return r["name"], None
    return None, None


def _merge(s: dict) -> dict:
    ov = get_overlay(s["id"])
    repo_name, task_id = _repo_task_for_cwd(s.get("cwd"))
    title = ov.get("name") or s.get("title") or (s.get("first_prompt") or "")[:64] or s["id"][:8]
    return {
        **s,
        "repo": repo_name,
        "task": task_id,
        "name": ov.get("name"),
        "display_title": title,
        "tags": ov.get("tags", []),
        "description": ov.get("description"),
        "pr_manual": ov.get("pr"),
        "starred": ov.get("starred", False),
        "archived": ov.get("archived", False),
        "hidden": ov.get("hidden", False),
        "mode": ov.get("mode"),  # "chat" | "terminal" | None (not yet chosen)
    }


def _haystack(r: dict) -> str:
    parts = [
        r.get("display_title", ""),
        r.get("title") or "",
        r.get("preview") or "",
        r.get("first_prompt") or "",
        r.get("branch") or "",
        r.get("description") or "",
        " ".join(map(str, r.get("tags", []))),
        " ".join(map(str, r.get("prs", []))),
        str(r.get("pr_manual") or ""),
    ]
    return " ".join(parts).lower()


def list_chats(
    repo: str | None = None,
    scope: str = "repo",
    tab: str = "active",
    q: str | None = None,
    branch: str | None = None,
    starred: bool | None = None,
) -> list[dict]:
    rows = [_merge(s) for s in build_index()]

    if scope == "repo" and repo:
        rows = [r for r in rows if r["repo"] == repo]
    if tab == "active":
        rows = [r for r in rows if not r["archived"] and not r["hidden"]]
    elif tab == "archived":
        rows = [r for r in rows if r["archived"]]
    elif tab == "hidden":
        rows = [r for r in rows if r["hidden"]]
    if starred:
        rows = [r for r in rows if r["starred"]]
    if branch:
        rows = [r for r in rows if (r.get("branch") or "") == branch]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in _haystack(r)]

    rows.sort(key=lambda r: (not r["starred"], -r["last_active"]))
    return rows


def get_chat(sid: str) -> dict | None:
    return next((s for s in build_index() if s["id"] == sid), None)


def get_transcript(sid: str) -> list[dict]:
    """Reconstruct a session's conversation as render-ready chat items.

    user(string) -> user message; assistant -> text + thinking + tool_use blocks;
    user(tool_result) -> attaches result to the matching tool by tool_use_id.
    """
    path = _find_transcript(sid)
    if not path or not path.exists():
        return []
    items: list[dict] = []
    tool_index: dict = {}
    with path.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t == "user":
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, str):
                    if content.strip():
                        items.append({"kind": "user", "text": content})
                elif isinstance(content, list):
                    tool_results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                    if tool_results:
                        for b in tool_results:
                            tool = tool_index.get(b.get("tool_use_id"))
                            if tool is not None:
                                c = b.get("content")
                                if isinstance(c, list):
                                    c = "\n".join(
                                        (x.get("text") or "") if isinstance(x, dict) else str(x) for x in c
                                    )
                                tool["result"] = c if isinstance(c, str) else ("" if c is None else str(c))
                                tool["isError"] = b.get("is_error")
                    else:
                        # A real user message with text/image blocks (e.g. a pasted image).
                        txt = "\n".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                        )
                        imgs = []
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "image":
                                src = b.get("source") or {}
                                if src.get("type") == "base64" and src.get("data"):
                                    imgs.append({"media_type": src.get("media_type") or "image/png", "data": src["data"]})
                        if txt or imgs:
                            item = {"kind": "user", "text": txt}
                            if imgs:
                                item["images"] = imgs
                            items.append(item)
            elif t == "assistant":
                content = (rec.get("message") or {}).get("content") or []
                if rec.get("isApiErrorMessage"):
                    # Claude Code records a mid-turn API failure (timeout, dropped socket,
                    # overload) as a synthetic assistant message — surface it as a clear
                    # error on resume/refresh, not as a normal reply.
                    etxt = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ).strip()
                    items.append({"kind": "error", "text": "⚠ API error — the turn stopped early: " + (etxt or "unknown error")})
                else:
                    texts, thinks, tools = [], [], []
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text":
                            texts.append(b.get("text", ""))
                        elif bt == "thinking":
                            thinks.append(b.get("thinking", ""))
                        elif bt == "tool_use":
                            tool = {"id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
                            tools.append(tool)
                            tool_index[b.get("id")] = tool
                    if texts or thinks or tools:
                        items.append({
                            "kind": "assistant",
                            "text": "".join(texts),
                            "thinking": "".join(thinks),
                            "tools": tools,
                        })
    return items


# --- trash (soft delete) ------------------------------------------------------
def _find_transcript(sid: str) -> Path | None:
    if CLAUDE_PROJECTS.exists():
        for proj in CLAUDE_PROJECTS.iterdir():
            f = proj / f"{sid}.jsonl"
            if f.exists():
                return f
    return None


def trash_chat(sid: str) -> None:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    f = _find_transcript(sid)
    patch = {"deleted": True}
    if f:
        shutil.move(str(f), str(TRASH_DIR / f"{sid}.jsonl"))
        patch["orig_dir"] = str(f.parent)
    set_overlay(sid, patch)


def restore_chat(sid: str) -> None:
    src = TRASH_DIR / f"{sid}.jsonl"
    orig = get_overlay(sid).get("orig_dir")
    if src.exists() and orig:
        Path(orig).mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(Path(orig) / f"{sid}.jsonl"))
    set_overlay(sid, {"deleted": False})


def list_trash() -> list[str]:
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    return [p.stem for p in TRASH_DIR.glob("*.jsonl")]


def empty_trash() -> None:
    if TRASH_DIR.exists():
        for p in TRASH_DIR.glob("*.jsonl"):
            p.unlink()
