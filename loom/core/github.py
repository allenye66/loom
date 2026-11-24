"""GitHub PR status via the `gh` CLI, with a short in-memory TTL cache.

A chat's branch + PR numbers are already tracked from its transcript (sessions.py).
PR *state* is dynamic (open → merged), so we don't persist it — we fetch on demand
and cache briefly so the chats UI doesn't shell out to `gh` on every render.
Never raises: returns state="unknown" if gh is missing / unauthed / the call fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from loom.core.config import LOOM_HOME

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_TTL = 60.0  # seconds (open PRs: checks and mergeability change)
_TTL_BY_STATE = {"merged": 6 * 3600.0, "closed": 600.0}  # a merged PR never changes back


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


_PR_URL = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
_PR_SHORT = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")


def parse_pr_ref(ref: str, default_repo: str | None = None) -> tuple[str, int]:
    """("owner/repo", number) from a PR URL, "owner/repo#123", or a bare "#123" / "123" (→ default_repo).
    Raises ValueError with a message fit to show the user."""
    ref = ref.strip()
    for rx in (_PR_URL, _PR_SHORT):
        m = rx.search(ref)
        if m:
            return m.group(1), int(m.group(2))
    if re.fullmatch(r"#?\d+", ref):
        if not default_repo:
            raise ValueError("can't tell which repo that PR number is in — pass the full PR URL or owner/repo#N")
        return default_repo, int(ref.lstrip("#"))
    raise ValueError("expected a PR URL, owner/repo#N, or a PR number")


_branch_cache: dict[tuple[str, str], tuple[float, list]] = {}


def prs_for_branch(repo: str, branch: str, ttl: float = 45.0) -> list[dict]:
    """PRs whose HEAD is `branch` in `repo`, any state: [{number, state, created_at, title, url}]. [] if gh
    can't say. Cached briefly (the PR scout asks every minute for an open chat)."""
    key = (repo, branch)
    hit = _branch_cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    data = _gh_json(["pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--limit", "20",
                     "--json", "number,state,createdAt,headRefName,title,url"])
    out = [
        {"number": p["number"], "state": str(p.get("state") or "").lower(), "created_at": _iso(p.get("createdAt")),
         "title": p.get("title"), "url": p.get("url")}
        for p in (data if isinstance(data, list) else []) if isinstance(p, dict) and p.get("headRefName") == branch
    ]
    _branch_cache[key] = (time.monotonic(), out)
    return out


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
    base = {"number": number, "repo": repo, "url": url, "state": "unknown", "branch": None,
            "head_sha": None, "base_branch": None, "mergeable": None, "created_at": None,
            "title": None, "draft": False, "merged": False, "checks": "unknown", "status": "unknown"}
    if not repo:
        return base
    key = (repo, str(number))
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL_BY_STATE.get(hit[1].get("state"), _TTL):
        return hit[1]
    data = _gh_json(["pr", "view", str(number), "--repo", repo, "--json",
                     "number,state,title,url,isDraft,mergedAt,mergeable,mergeStateStatus,statusCheckRollup,headRefName,headRefOid,baseRefName,createdAt"])
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
            "branch": data.get("headRefName"),
            "head_sha": data.get("headRefOid"),
            "base_branch": data.get("baseRefName"),
            "mergeable": mergeable or None,  # MERGEABLE | CONFLICTING | UNKNOWN (GitHub computes it lazily)
            "created_at": _iso(data.get("createdAt")),
            "draft": draft,
            "merged": merged,
            "state": state,
            "checks": checks,
            "status": _rollup_status(state, draft, checks, mss, mergeable),
        })
    _cache[key] = (now, base)
    return base


# --- which PRs a chat CREATED -------------------------------------------------------
# A PR's createdAt never changes, so it's cached on disk forever: {"owner/repo#n": epoch},
# 0 = no such PR (bad link / no access). Only successful lookups are cached.
CREATED_CACHE = LOOM_HOME / "pr_created.json"
_SLACK = 120.0  # seconds of clock skew / gh latency allowed around a tool window
_created: dict | None = None


def _load_created() -> dict:
    global _created
    if _created is None:
        try:
            _created = json.loads(CREATED_CACHE.read_text())
        except (OSError, json.JSONDecodeError):
            _created = {}
    return _created


def _save_created(data: dict) -> None:
    tmp = CREATED_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, CREATED_CACHE)


def _iso(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def pr_created_at(keys: list[str]) -> dict[str, float]:
    """{"owner/repo#n": createdAt epoch (0 = no such PR)} for the keys GitHub could answer.
    Uncached keys are fetched in one GraphQL call per repo (chunks of 50). Keys missing from
    the result couldn't be checked (gh missing / offline) — the caller decides."""
    cache = _load_created()
    missing: dict[str, list[int]] = {}
    for k in keys:
        if k not in cache:
            repo, _, n = k.rpartition("#")
            missing.setdefault(repo, []).append(int(n))
    gh = shutil.which("gh")
    dirty = False
    for repo, nums in missing.items() if gh else ():
        owner, _, name = repo.partition("/")
        for i in range(0, len(nums), 50):
            chunk = nums[i:i + 50]
            fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ createdAt }}" for n in chunk)
            q = f'query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {fields} }} }}'
            try:
                # Not-found PRs make gh exit 1 but still print the partial data — read it anyway.
                p = subprocess.run([gh, "api", "graphql", "-f", f"query={q}"],
                                   capture_output=True, text=True, timeout=20)
                data = (json.loads(p.stdout or "null") or {}).get("data")
            except Exception:  # noqa: BLE001 — offline/timeout → leave uncached, retry later
                continue
            if not isinstance(data, dict):
                continue
            node = data.get("repository") or {}
            for n in chunk:
                pr = node.get(f"p{n}") if isinstance(node, dict) else None
                cache[f"{repo}#{n}"] = (_iso(pr.get("createdAt")) or 0.0) if isinstance(pr, dict) else 0.0
            dirty = True
    if dirty:
        try:
            _save_created(cache)
        except OSError:
            pass
    return {k: cache[k] for k in keys if k in cache}


def created_prs_cached(seen: dict, windows: list) -> list[str]:
    """Like `created_prs`, but never asks GitHub: only what the on-disk createdAt cache already knows (plus
    Claude's own pr-link records, as the offline fallback). For hot paths — listing / searching hundreds of
    chats — where a network call per chat is out of the question; a PR not cached yet simply isn't included."""
    cache = _load_created()
    out = []
    for k in sorted(seen, key=lambda k: seen[k][0] or 0):
        c = cache.get(k)
        if c is None:
            if seen[k][1] == "pr-link":
                out.append(k)
        elif c and any(s - _SLACK <= c <= e + _SLACK for s, e in windows):
            out.append(k)
    return out


def created_prs(seen: dict, windows: list) -> list[str]:
    """The "owner/repo#n" keys of PRs the chat itself created, in first-seen order.

    `seen` / `windows` are the transcript evidence from sessions._PrEvidence: PR URLs that
    appeared on the agent's side, and the spans its tool calls ran. Created here ⇔ GitHub's
    createdAt falls inside a tool window (± _SLACK). A pasted/looked-up old PR fails that. If
    GitHub can't be asked, fall back to trusting Claude's own pr-link records (the pre-check
    behaviour), so an offline loom still shows the obvious ones."""
    order = sorted(seen, key=lambda k: seen[k][0] or 0)
    created = pr_created_at(order)
    out = []
    for k in order:
        c = created.get(k)
        if c is None:
            if seen[k][1] == "pr-link":
                out.append(k)
        elif c and any(s - _SLACK <= c <= e + _SLACK for s, e in windows):
            out.append(k)
    return out


_repo_for_dir: dict[str, str | None] = {}


def repo_for_dir(path: str | None) -> str | None:
    """"owner/repo" of the GitHub repo a directory belongs to (cached per path)."""
    if not path or not Path(path).is_dir():
        return None
    if path not in _repo_for_dir:
        gh = shutil.which("gh")
        repo = None
        if gh:
            try:
                p = subprocess.run([gh, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                                   cwd=path, capture_output=True, text=True, timeout=15)
                repo = (p.stdout.strip() or None) if p.returncode == 0 else None
            except Exception:  # noqa: BLE001
                repo = None
        _repo_for_dir[path] = repo
    return _repo_for_dir[path]
