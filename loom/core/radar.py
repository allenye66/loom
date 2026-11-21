"""PR conflict awareness — behind the scenes, for the agents (the user never sees it).

Parallel agents in one category often work on the same area. Their open PRs then collide at merge time:
whichever merges second has to rebase, and finding that out late wastes a whole review cycle. So for
each category this looks at the OPEN PRs that its chats created (core/prlink.py evidence: transcript,
PR scout, pinned) and tests, with `git merge-tree --write-tree` (no checkout, nothing touched), whether
any two of them would conflict with each other. It also notes a chat's own open PR that GitHub already
reports as conflicting with its base (e.g. after a sibling's PR merged).

The result is never shown in the dashboard. Each affected agent is told once per change through its
per-message context (`overlap_note` ← core/workspace.py), so it can coordinate with the other agent
(`loom chats send`) or plan its rebase *before* the merge — no human in the loop. Lockfiles are ignored
(they conflict trivially and are simply regenerated). Textual conflicts only: a clean merge can still
be wrong. Computed in the background (~once a minute) and cached; a hook never computes it inline.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from loom.core import categories, github, prlink, registry, sessions

TTL_S = 40.0
LOCKFILES = {
    "bun.lock", "bun.lockb", "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
}
MAX_FILES_SHOWN = 8

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


def _has_commit(cwd: str, sha: str) -> bool:
    return _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd)[0] == 0


def merge_conflicts(repo_dir: str, a: str, b: str) -> tuple[bool | None, list[str]]:
    """Would merging commit `a` with `b` conflict? (True/False, conflicted files) — None if git can't say
    (e.g. a commit isn't in this repo's object store). Worktrees share one object store, so any loom
    chat's directory can answer for any of the repo's commits."""
    if not a or not b or not _has_commit(repo_dir, a) or not _has_commit(repo_dir, b):
        return None, []
    rc, out = _git(["merge-tree", "--write-tree", "--name-only", a, b], repo_dir, timeout=30.0)
    if rc == 0:
        return False, []
    if rc != 1:
        return None, []
    files: list[str] = []
    for ln in out.splitlines()[1:]:  # first line is the tree id
        if not ln.strip():
            break  # a blank line ends the file list; informational messages follow
        files.append(ln.strip())
    # Lockfiles conflict whenever two branches both touch them and are simply regenerated.
    files = [f for f in files if os.path.basename(f) not in LOCKFILES]
    return (True, files) if files else (False, [])


def _open_prs(members: list[dict], tasks: dict, raw: dict) -> list[dict]:
    """Every open PR owned (created / scouted / pinned) by one of the category's chats."""
    out: list[dict] = []
    for c in members:
        t = tasks.get(c["id"])
        if not t or not t.worktree_path or not os.path.isdir(t.worktree_path):
            continue
        keys = prlink.chat_pr_keys(raw.get(c["id"]), sessions.get_overlay(c["id"]))
        for key in keys:
            repo, _, n = key.rpartition("#")
            st = github.pr_status(repo, int(n))
            if st.get("state") != "open":
                continue
            out.append({"chat": c["id"], "chat_name": _title(c), "wt": t.worktree_path, "repo_root": t.repo_root, "key": key, "pr": st})
    return out


def _compute_category(cat: dict, members: list[dict], tasks: dict, raw: dict) -> dict:
    prs = _open_prs(members, tasks, raw)
    conflicts = []
    for i, a in enumerate(prs):
        for b in prs[i + 1:]:
            if a["chat"] == b["chat"] or a["key"] == b["key"] or a["repo_root"] != b["repo_root"]:
                continue
            bad, files = merge_conflicts(a["wt"], a["pr"].get("head_sha") or "", b["pr"].get("head_sha") or "")
            if bad:
                conflicts.append({
                    "a": {"chat": a["chat"], "name": a["chat_name"], "key": a["key"], "number": a["pr"]["number"], "title": a["pr"].get("title")},
                    "b": {"chat": b["chat"], "name": b["chat_name"], "key": b["key"], "number": b["pr"]["number"], "title": b["pr"].get("title")},
                    "files": files[:MAX_FILES_SHOWN], "n_files": len(files),
                })
    base_conflicts = [
        {"chat": p["chat"], "key": p["key"], "number": p["pr"]["number"], "title": p["pr"].get("title"), "base": p["pr"].get("base_branch")}
        for p in prs if p["pr"].get("mergeable") == "CONFLICTING"
    ]
    return {
        "key": cat["id"], "category": cat["name"], "computed_at": time.time(),
        "open_prs": [{"chat": p["chat"], "name": p["chat_name"], "key": p["key"], "title": p["pr"].get("title")} for p in prs],
        "conflicts": conflicts, "base_conflicts": base_conflicts,
    }


def compute_all() -> dict[str, dict]:
    """Recompute every category with two or more chats (one chat-list build for all of them). Stores and
    returns {category id → result}."""
    state = categories.get_state()
    chats = _loom_chats(state)
    tasks = {t.chat_id: t for t in registry.list_tasks() if t.chat_id}
    raw = {s["id"]: s for s in sessions.build_index()}
    out: dict[str, dict] = {}
    for cat in state["categories"]:
        members = [c for c in chats if c.get("category") == cat["id"] and not c.get("archived")]
        if len(members) >= 2:
            out[cat["id"]] = _compute_category(cat, members, tasks, raw)
    with _lock:
        _cache.clear()
        _cache.update(out)
    return out


def get(cat_id: str, *, max_age: float = TTL_S) -> dict | None:
    """One category's result, from the cache when fresh, else recomputed."""
    with _lock:
        hit = _cache.get(cat_id)
    if hit and time.time() - hit["computed_at"] <= max_age:
        return hit
    return compute_all().get(cat_id)


def cached(cat_id: str) -> dict | None:
    with _lock:
        return _cache.get(cat_id)


def overlap_note(chat_id: str, cat_id: str) -> str:
    """What to tell one chat about its open PRs' conflicts (from the cache; "" if none)."""
    r = cached(cat_id)
    if not r:
        return ""
    lines = []
    for o in r["conflicts"]:
        mine, other = (o["a"], o["b"]) if o["a"]["chat"] == chat_id else (o["b"], o["a"]) if o["b"]["chat"] == chat_id else (None, None)
        if not mine:
            continue
        shown = ", ".join(o["files"][:5]) + (f" (+{o['n_files'] - 5} more)" if o["n_files"] > 5 else "")
        lines.append(
            f'  - your PR #{mine["number"]} and PR #{other["number"]} "{other["title"] or ""}" '
            f'(chat "{other["name"]}", {other["chat"][:8]}) both change {shown or "the same code"} — they would conflict once either merges'
        )
    for b in r["base_conflicts"]:
        if b["chat"] == chat_id:
            lines.append(f'  - your PR #{b["number"]} already conflicts with {b["base"] or "its base branch"} on GitHub — rebase it')
    if not lines:
        return ""
    return (
        "Heads-up — conflicts between open PRs in your category:\n" + "\n".join(lines) +
        '\n  Whoever merges second must rebase. Coordinate with the other agent (`loom chats send <id> "…"`) '
        "or plan your rebase for when theirs lands."
    )
