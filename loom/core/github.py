"""GitHub PR status via the `gh` CLI, with a short in-memory TTL cache.

A chat's branch + PR numbers are already tracked from its transcript (sessions.py).
PR *state* is dynamic (open → merged), so we don't persist it — we fetch on demand
and cache briefly so the chats UI doesn't shell out to `gh` on every render.
Never raises: returns state="unknown" if gh is missing / unauthed / the call fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_TTL = 60.0  # seconds


def _gh_json(args: list[str]) -> dict | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        p = subprocess.run([gh, *args], capture_output=True, text=True, timeout=15)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout or "null")
    except Exception:  # noqa: BLE001 — gh missing/timeout/bad json → no status
        return None


def pr_status(repo: str | None, number: str | int) -> dict:
    """{number, repo, url, state, title, draft, merged} for a PR.

    state ∈ open | closed | merged | unknown. Cached for _TTL seconds per (repo, number).
    """
    url = f"https://github.com/{repo}/pull/{number}" if repo else None
    base = {"number": number, "repo": repo, "url": url, "state": "unknown",
            "title": None, "draft": False, "merged": False}
    if not repo:
        return base
    key = (repo, str(number))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = _gh_json(["pr", "view", str(number), "--repo", repo, "--json",
                     "number,state,title,url,isDraft,mergedAt"])
    if isinstance(data, dict):
        merged = bool(data.get("mergedAt"))
        st = (data.get("state") or "").lower()  # gh: OPEN | CLOSED | MERGED
        base.update({
            "url": data.get("url") or base["url"],
            "title": data.get("title"),
            "draft": bool(data.get("isDraft")),
            "merged": merged,
            "state": "merged" if (merged or st == "merged") else (st or "unknown"),
        })
    _cache[key] = (now, base)
    return base
