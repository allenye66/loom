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


_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def _rollup_checks(rollup) -> str:
    """Aggregate a PR's statusCheckRollup → pass | fail | pending | none.

    Entries are either CheckRun (`status` COMPLETED/IN_PROGRESS/QUEUED + `conclusion`) or
    StatusContext (`state` SUCCESS/FAILURE/PENDING/ERROR). fail dominates, then pending.
    """
    if not isinstance(rollup, list) or not rollup:
        return "none"
    states: set[str] = set()
    for c in rollup:
        if not isinstance(c, dict):
            continue
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if concl in _FAIL:
            states.add("fail")
        elif status in ("IN_PROGRESS", "QUEUED", "PENDING") or concl == "PENDING" or (not concl and status != "COMPLETED"):
            states.add("pending")
        elif concl in _PASS:
            states.add("pass")
        else:
            states.add("pending")
    if "fail" in states:
        return "fail"
    if "pending" in states:
        return "pending"
    return "pass" if states else "none"


def _rollup_status(state: str, draft: bool, checks: str, mss: str, mergeable: str) -> str:
    """Coarse PR status for the UI: merged | closed | draft | error | ready | passing | running |
    unmerged. `mss` = mergeStateStatus (CLEAN means GitHub considers it ready to merge)."""
    if state == "merged":
        return "merged"
    if state == "closed":
        return "closed"
    if draft:
        return "draft"
    if checks == "fail" or mss == "DIRTY" or mergeable == "CONFLICTING":
        return "error"
    if mss == "CLEAN":
        return "ready"
    if checks == "pass":
        return "passing"      # tests pass but not mergeable yet (needs review / behind base)
    if checks == "pending":
        return "running"      # CI in flight
    return "unmerged"         # open, no/unknown checks


def pr_status(repo: str | None, number: str | int) -> dict:
    """{number, repo, url, state, title, draft, merged, checks, status} for a PR.

    state  ∈ open | closed | merged | unknown
    checks ∈ pass | fail | pending | none | unknown   (CI rollup)
    status ∈ merged | closed | draft | error | ready | passing | running | unmerged | unknown
             (the coarse merge-readiness shown in the UI)
    Cached for _TTL seconds per (repo, number).
    """
    url = f"https://github.com/{repo}/pull/{number}" if repo else None
    base = {"number": number, "repo": repo, "url": url, "state": "unknown",
            "title": None, "draft": False, "merged": False, "checks": "unknown", "status": "unknown"}
    if not repo:
        return base
    key = (repo, str(number))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    data = _gh_json(["pr", "view", str(number), "--repo", repo, "--json",
                     "number,state,title,url,isDraft,mergedAt,mergeable,mergeStateStatus,statusCheckRollup"])
    if isinstance(data, dict):
        merged = bool(data.get("mergedAt"))
        st = (data.get("state") or "").lower()  # gh: OPEN | CLOSED | MERGED
        state = "merged" if (merged or st == "merged") else (st or "unknown")
        draft = bool(data.get("isDraft"))
        checks = _rollup_checks(data.get("statusCheckRollup"))
        mss = (data.get("mergeStateStatus") or "").upper()        # CLEAN/BLOCKED/BEHIND/DIRTY/UNKNOWN
        mergeable = (data.get("mergeable") or "").upper()          # MERGEABLE/CONFLICTING/UNKNOWN
        base.update({
            "url": data.get("url") or base["url"],
            "title": data.get("title"),
            "draft": draft,
            "merged": merged,
            "state": state,
            "checks": checks,
            "status": _rollup_status(state, draft, checks, mss, mergeable),
        })
    _cache[key] = (now, base)
    return base
