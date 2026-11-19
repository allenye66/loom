"""Conflict radar — which chats in a category are changing the same files.

Parallel worktrees don't prevent collisions, they move them to merge time: two chats edit the same
file and the second PR conflicts. The radar looks *within a category* (the chats the user grouped as
working on related things) and, for each pair of chats whose worktrees changed a common file, says so
— and whether their committed work would actually conflict (`git merge-tree --write-tree`, no
checkout, nothing touched). Changed files = commits since the fork point from the task's base branch
plus uncommitted edits; lockfiles are ignored (they conflict trivially and nobody edits them by hand).

Computed in the background (`compute_all`, ~once a minute) and on demand; the dashboard reads the
cache, and each affected chat is told once per change through its per-message context
(`overlap_note` ← core/workspace.py), so two agents learn they're colliding *before* they both finish.
Read-only with respect to the worktrees. Textual conflicts only — a clean merge can still be wrong.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from loom.core import categories, registry, sessions

TTL_S = 40.0
LOCKFILES = {
    "bun.lock", "bun.lockb", "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
}
MAX_FILES_SHOWN = 20

_cache: dict[str, dict] = {}
_lock = threading.Lock()


def _title(c: dict) -> str:
    return (c.get("name") or c.get("display_title") or c["id"][:8]).strip()


def _loom_chats(state: dict) -> list[dict]:
    """loom's active task chats with their category id (same view the sidebar has)."""
    return [{**c, "category": state["assign"].get(c["id"])} for c in sessions.list_chats(scope="all", tab="active") if c.get("task")]


def _git(args: list[str], cwd: str, timeout: float = 20.0) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return p.returncode, p.stdout


def changed_files(worktree: str, base: str) -> set[str]:
    """Paths this worktree changed relative to where it forked from `base`, committed or not."""
    files: set[str] = set()
    for ref in (base, f"origin/{base}"):  # a local base branch may be stale or absent
        rc, out = _git(["diff", "--name-only", "-z", f"{ref}...HEAD"], worktree)
        if rc == 0:
            files.update(p for p in out.split("\0") if p)
            break
    rc, out = _git(["status", "--porcelain=v1", "-z", "-uall"], worktree)
    if rc == 0:
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            rec = parts[i]
            i += 1
            if len(rec) > 3:
                files.add(rec[3:])
                if rec[0] in "RC":  # rename/copy: the next NUL-field is the old path
                    i += 1
    return {f for f in files if os.path.basename(f) not in LOCKFILES}


def _head(worktree: str) -> str | None:
    rc, out = _git(["rev-parse", "HEAD"], worktree)
    return out.strip() if rc == 0 and out.strip() else None


def merge_conflicts(repo_dir: str, a: str, b: str) -> tuple[bool | None, list[str]]:
    """Would merging commit `a` with `b` conflict? (True/False, conflicted files) — None if git can't say."""
    if not a or not b:
        return None, []
    rc, out = _git(["merge-tree", "--write-tree", "--name-only", a, b], repo_dir, timeout=30.0)
    if rc == 0:
        return False, []
    if rc != 1:
        return None, []
    lines = out.splitlines()[1:]  # first line is the tree id
    files: list[str] = []
    for ln in lines:
        if not ln.strip():
            break  # a blank line ends the file list; informational messages follow
        files.append(ln.strip())
    # Lockfiles conflict whenever two branches both touch them and are simply regenerated — not a
    # collision worth flagging (the overlap check ignores them for the same reason).
    files = [f for f in files if os.path.basename(f) not in LOCKFILES]
    return (True, files) if files else (False, [])


def _compute_category(cat: dict, members: list[dict], tasks: dict) -> dict:
    infos = []
    for c in members:
        t = tasks.get(c["id"])
        if not t or not t.worktree_path or not os.path.isdir(t.worktree_path):
            continue
        infos.append({
            "id": c["id"], "name": _title(c), "wt": t.worktree_path, "repo": t.repo_root,
            "files": changed_files(t.worktree_path, t.base_branch or "main"), "head": _head(t.worktree_path),
        })
    overlaps = []
    for i, a in enumerate(infos):
        for b in infos[i + 1:]:
            if a["repo"] != b["repo"]:
                continue
            common = sorted(a["files"] & b["files"])
            if not common:
                continue
            conflict, cfiles = merge_conflicts(a["wt"], a["head"], b["head"])
            overlaps.append({
                "a": {"id": a["id"], "name": a["name"]}, "b": {"id": b["id"], "name": b["name"]},
                "files": common[:MAX_FILES_SHOWN], "n_files": len(common),
                "conflicts": conflict, "conflict_files": cfiles[:MAX_FILES_SHOWN],
            })
    return {
        "key": cat["id"], "category": cat["name"], "computed_at": time.time(),
        "members": [{"id": x["id"], "name": x["name"], "files": len(x["files"])} for x in infos],
        "overlaps": overlaps,
    }


def compute_all() -> dict[str, dict]:
    """Recompute every category that has two or more chats with a worktree (one chat-list build for
    all of them). Stores and returns {category id → result}."""
    state = categories.get_state()
    chats = _loom_chats(state)
    tasks = {t.chat_id: t for t in registry.list_tasks() if t.chat_id}
    out: dict[str, dict] = {}
    for cat in state["categories"]:
        members = [c for c in chats if c.get("category") == cat["id"] and not c.get("archived")]
        if len(members) >= 2:
            out[cat["id"]] = _compute_category(cat, members, tasks)
    with _lock:
        _cache.clear()
        _cache.update(out)
    return out


def get(cat_id: str, *, max_age: float = TTL_S) -> dict | None:
    """One category's radar, from the cache when fresh, else recomputed."""
    with _lock:
        hit = _cache.get(cat_id)
    if hit and time.time() - hit["computed_at"] <= max_age:
        return hit
    return compute_all().get(cat_id)


def cached(cat_id: str) -> dict | None:
    with _lock:
        return _cache.get(cat_id)


def overview(max_age: float = TTL_S) -> dict[str, dict]:
    """{category id → {pairs, conflicts, computed_at}} for the sidebar badges (cheap; recomputes when stale)."""
    with _lock:
        fresh = bool(_cache) and all(time.time() - v["computed_at"] <= max_age for v in _cache.values())
        snap = dict(_cache)
    if not fresh:
        snap = compute_all()
    return {
        k: {"pairs": len(v["overlaps"]), "conflicts": sum(1 for o in v["overlaps"] if o["conflicts"]), "computed_at": v["computed_at"]}
        for k, v in snap.items()
    }


def overlap_note(chat_id: str, cat_id: str) -> str:
    """What to tell one chat about its overlaps in its category (from the cache; "" if none)."""
    r = cached(cat_id)
    if not r:
        return ""
    lines = []
    for o in r["overlaps"]:
        if chat_id not in (o["a"]["id"], o["b"]["id"]):
            continue
        other = o["b"] if o["a"]["id"] == chat_id else o["a"]
        shown = ", ".join(o["files"][:5]) + (f" (+{o['n_files'] - 5} more)" if o["n_files"] > 5 else "")
        verdict = {True: "their committed work would CONFLICT with yours", False: "no textual conflict yet", None: "conflict status unknown"}[o["conflicts"]]
        lines.append(f'  - "{other["name"]}" (chat {other["id"][:8]}) also changed {shown} — {verdict}')
    if not lines:
        return ""
    return (
        "Heads-up — chats in your category are changing the same files as you:\n" + "\n".join(lines) +
        '\n  Coordinate before you both finish (`loom chats send <id> "…"`), and expect to rebase/merge in turn.'
    )
