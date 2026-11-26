"""Find a PR by describing it (D31).

"In this PR I added a new UI button last week that did X" → the PR (and the chat that wrote it, even if archived).
With hundreds of PRs a week, neither scrolling GitHub nor remembering which chat it was works, so this does the
digging with a cheap Haiku pass. Stages — everything that can be deterministic is:

  1. understand  (Haiku, tiny)  the description → search keywords + synonyms + a date range ("last week").
  2. retrieve    (no model)     your PRs from GitHub in that window (`gh pr list --author @me`, per project repo;
                                 a keyword search tops up when the window is bigger than one page), and loom's chats
                                 (active AND archived) active in it. Chats and PRs are linked by the evidence loom already
                                 has (PR numbers in a chat, its branch). Each side is pre-scored on the keywords locally.
  3. rank        (Haiku)        the best ~60 PRs and ~30 chats, one compact line each → the likeliest matches.
  4. verify      (Haiku)        for the top few: the PR's changed files + fuller description (and, for chats with no PR
                                 linked, the PRs on their branch) → final suggestions, a confidence, a one-line "why", and a
                                 question that would narrow it down.

Costs about 3 small Haiku calls (a cent or two) and ~10–20 s, mostly `gh`. Only titles, first prompts, cached
brief goals, branch names and PR text/paths go to the model — never transcripts. Searches run as background jobs the dashboard
polls (phases shown); refinements re-run with the whole conversation, and PR lists are cached for a few minutes so they're quick.
Never raises: a failed Haiku call falls back to the local ranking and says so.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from loom.core import briefs, github, repos, sessions

DEFAULT_DAYS = 45
WIDEN_FACTOR = 3                 # nothing found → look this much further back, once
PR_PAGE = 300                    # newest PRs fetched per repo per search
KEYWORD_TOPUP = 3                # extra keyword searches when the window overflowed one page
MAX_PR_CANDIDATES = 60
MAX_CHAT_CANDIDATES = 30
DEEP_PRS = 8
DEEP_CHATS = 3
MAX_SUGGESTIONS = 6
PR_CACHE_TTL = 300.0
JOB_TTL = 1800.0
HAIKU_TIMEOUT = 90

_STOP = set("a an and the of to in on for with that this it its is was were be been are as at by from or but not into over about "
            "some one added add new did does made make pr pull request chat sometime last week month ago when which where what who how "
            "i we my our you your me us".split())

UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
        "synonyms": {"type": "array", "items": {"type": "string"}},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
    },
    "required": ["keywords", "synonyms", "date_from", "date_to"],
}
UNDERSTAND_PROMPT = """You turn a developer's loose description of a pull request they once made into search terms. \
Today's date is {today}. Output: `keywords` — up to 10 lowercase words/short phrases most likely to appear in the PR's title, \
branch name, description or code (feature nouns, UI element names, component/file-name fragments, domain terms — NOT filler \
like "added" or "pr"); `synonyms` — up to 10 more lowercase alternatives (e.g. button → btn, cta; modal → dialog, popup); \
`date_from` / `date_to` — YYYY-MM-DD bounds of when it was probably made if the description says so ("last week", "in October", \
"yesterday"; a vague hint gets a generous range), else empty strings."""

RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "score": {"type": "integer"}, "why": {"type": "string"}}, "required": ["id", "score", "why"]}},
    },
    "required": ["matches"],
}
RANK_PROMPT = """A developer is trying to find ONE of their pull requests (and the coding-agent chat that wrote it) from a rough \
description; they've made hundreds. You get their description (plus any refinements they typed afterwards) and numbered \
candidates: `P#` lines are PRs (title, branch, date, size, a snippet of the description, and the chat linked to it if any), `C#` \
lines are chats (title, when active, first prompt, goal summary, linked PRs). Pick the up-to-10 candidates most likely to be what \
they mean, best first, each with a 0-100 score and a one-sentence reason that quotes what matched. Judge by meaning, not just \
shared words. Never invent ids; if nothing fits, return few or no matches."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]}, "why": {"type": "string"}},
            "required": ["id", "confidence", "why"]}},
        "narrow_down": {"type": "string"},
    },
    "required": ["suggestions", "narrow_down"],
}
VERIFY_PROMPT = """A developer is finding one of their pull requests from a rough description. You now see the FINAL short-list \
with more detail: for PRs the changed file paths and a fuller description, for chats their goal and the PRs on their branch. \
Re-rank them: return up to 6 suggestions, most likely first, each with a confidence (high = the details clearly match what they \
described; medium = plausible; low = a guess) and ONE sentence saying why, citing the concrete evidence (a file path, a phrase \
from the PR). Drop candidates the details rule out. Also give `narrow_down`: ONE short question the developer could answer to \
tell the remaining candidates apart (e.g. which page it touched, roughly which day, a word in the title) — or an empty string \
if one suggestion is clearly right. Never invent ids."""

# --- jobs ------------------------------------------------------------------------------------
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def start(query: str, history: list[str] | None = None, days: int | None = None, project: str | None = None) -> str:
    """Begin a search in the background; returns a job id to poll with `get`."""
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "phase": "starting…", "done": False, "error": None, "result": None, "started": time.time()}
    with _lock:
        for k in [k for k, j in _jobs.items() if time.time() - j["started"] > JOB_TTL]:
            _jobs.pop(k, None)
        _jobs[jid] = job

    def progress(p: str) -> None:
        job["phase"] = p

    def work() -> None:
        try:
            job["result"] = search(query, history or [], days=days, project=project, progress=progress)
        except Exception as e:  # noqa: BLE001 — a job must always finish, with a message
            job["error"] = str(e)[:300]
        finally:
            job["done"] = True
            job["phase"] = "done"

    threading.Thread(target=work, daemon=True, name=f"find-pr-{jid}").start()
    return jid


def get(jid: str) -> dict | None:
    return _jobs.get(jid)


# --- helpers ---------------------------------------------------------------------------------
def _toks(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9_\-/.]*", (text or "").lower()) if t not in _STOP and len(t) > 1]


def _clip(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"


def _parse_day(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "").strip()[:10]) if s else None
    except ValueError:
        return None


def _score(text: str, terms: list[str], weight: int = 1) -> int:
    low = text.lower()
    return weight * sum(1 for t in terms if t in low)


def _haiku(system: str, text: str, schema: dict) -> dict:
    out, _meta = briefs.haiku_json(system, text, schema, timeout=HAIKU_TIMEOUT)
    return out


# --- stage 1: understand ---------------------------------------------------------------------
def understand(text: str) -> dict:
    """{keywords, synonyms, date_from, date_to} — from Haiku, or a plain tokenisation if it can't answer."""
    try:
        out = _haiku(UNDERSTAND_PROMPT.format(today=date.today().isoformat()), text, UNDERSTAND_SCHEMA)
        kw = [str(k).lower() for k in out.get("keywords") or [] if str(k).strip()][:10]
        syn = [str(k).lower() for k in out.get("synonyms") or [] if str(k).strip()][:10]
        if kw or syn:
            return {"keywords": kw, "synonyms": syn, "date_from": out.get("date_from") or "", "date_to": out.get("date_to") or "", "ai": True}
    except Exception:  # noqa: BLE001
        pass
    return {"keywords": list(dict.fromkeys(_toks(text)))[:10], "synonyms": [], "date_from": "", "date_to": "", "ai": False}


# --- stage 2: retrieve -----------------------------------------------------------------------
_pr_cache: dict[tuple, tuple[float, list]] = {}


def _norm_pr(slug: str, p: dict) -> dict:
    return {
        "key": f"{slug}#{p['number']}", "repo": slug, "number": p["number"], "title": p.get("title") or "",
        "body": p.get("body") or "", "created": github._iso(p.get("createdAt")), "merged": github._iso(p.get("mergedAt")),
        "state": "merged" if p.get("mergedAt") else str(p.get("state") or "").lower(), "branch": p.get("headRefName") or "",
        "url": p.get("url"), "additions": p.get("additions"), "deletions": p.get("deletions"), "files": p.get("changedFiles"),
    }


def _gh_prs(slug: str, search: str, limit: int) -> list[dict]:
    data = github._gh_json(["pr", "list", "--repo", slug, "--author", "@me", "--state", "all", "--search", search, "--limit", str(limit),
                            "--json", "number,title,body,createdAt,mergedAt,state,headRefName,url,additions,deletions,changedFiles"])
    return [_norm_pr(slug, p) for p in (data if isinstance(data, list) else []) if isinstance(p, dict) and "number" in p]


def fetch_prs(slug: str, since: date, keywords: list[str]) -> list[dict]:
    """The user's PRs in `slug` created on/after `since`: the newest page, topped up with keyword searches when the
    window held more than a page (so a busy month still surfaces older matches). Cached for PR_CACHE_TTL."""
    key = (slug, since.isoformat(), tuple(keywords[:KEYWORD_TOPUP]))
    hit = _pr_cache.get(key)
    if hit and time.time() - hit[0] < PR_CACHE_TTL:
        return hit[1]
    base = f"created:>={since.isoformat()}"
    prs = _gh_prs(slug, base, PR_PAGE)
    if len(prs) >= PR_PAGE:  # overflowed one page: the match may be older than the page reaches
        by = {p["number"]: p for p in prs}
        for kw in keywords[:KEYWORD_TOPUP]:
            for p in _gh_prs(slug, f"{kw} in:title,body {base}", 100):
                by.setdefault(p["number"], p)
        prs = list(by.values())
    _pr_cache[key] = (time.time(), prs)
    return prs


def _project_slugs(project: str | None) -> dict[str, str]:
    """{GitHub "owner/repo": loom project name} for the registered projects (one `gh` lookup each, cached)."""
    out: dict[str, str] = {}
    for r in repos.list_repos():
        if project and r["name"] != project:
            continue
        slug = github.repo_for_dir(r["root"])
        if slug:
            out[slug] = r["name"]
    return out


def _brief_goal(chat_id: str) -> str:
    try:
        return str(((briefs._read(chat_id) or {}).get("brief") or {}).get("goal") or "")
    except Exception:  # noqa: BLE001
        return ""


def gather_chats(since: date, until: date | None, project: str | None) -> list[dict]:
    """loom's task chats — archived included — that were active in the window."""
    lo = datetime.combine(since, datetime.min.time()).timestamp()
    hi = datetime.combine(until + timedelta(days=1), datetime.min.time()).timestamp() if until else None
    out = []
    for c in sessions.list_chats(scope="all", tab="all"):
        if not c.get("task") or c.get("deleted") or (project and c.get("repo") != project):
            continue
        created = github._iso(c.get("created")) or c.get("last_active") or 0
        if (c.get("last_active") or 0) < lo or (hi and created > hi):
            continue
        out.append(c)
    return out


# --- the search ------------------------------------------------------------------------------
def search(query: str, history: list[str] | None = None, *, days: int | None = None, project: str | None = None, progress=lambda p: None) -> dict:
    """The whole pipeline → {suggestions, narrow_down, searched, notes}."""
    history = [h for h in (history or []) if h and h.strip()]
    text = "\n".join([query.strip(), *[f"(refinement) {h.strip()}" for h in history]]).strip()
    if not text:
        raise ValueError("describe the PR you're looking for")
    notes: list[str] = []
    progress("understanding your description…")
    u = understand(text)
    if not u["ai"]:
        notes.append("couldn't reach Haiku to interpret the description — used a plain keyword match")
    terms = list(dict.fromkeys([*u["keywords"], *u["synonyms"]])) or _toks(text)
    today = date.today()
    d_from, d_to = _parse_day(u["date_from"]), _parse_day(u["date_to"])
    if days:
        since, until = today - timedelta(days=days), None
    elif d_from:
        since, until = d_from - timedelta(days=3), (d_to + timedelta(days=3)) if d_to else None
    else:
        since, until = today - timedelta(days=DEFAULT_DAYS), None

    widened = False
    for attempt in (0, 1):
        progress("reading your PRs from GitHub…")
        slugs = _project_slugs(project)
        pr_pool: list[dict] = []
        if slugs:
            with ThreadPoolExecutor(max_workers=min(6, len(slugs))) as ex:
                for lst in ex.map(lambda s: fetch_prs(s, since, u["keywords"]), slugs):
                    pr_pool += lst
        else:
            notes.append("no project has a GitHub repo `gh` can see — only chats were searched (is `gh` signed in?)")
        if until:
            hi = datetime.combine(until + timedelta(days=1), datetime.min.time()).timestamp()
            pr_pool = [p for p in pr_pool if not p["created"] or p["created"] <= hi]
        progress("matching your chats…")
        chat_pool = gather_chats(since, until, project)
        if pr_pool or chat_pool or attempt or days:
            break
        widened, since, until = True, today - timedelta(days=DEFAULT_DAYS * WIDEN_FACTOR), None
        notes.append(f"nothing in that window — widened to the last {DEFAULT_DAYS * WIDEN_FACTOR} days")

    cands = _build_candidates(pr_pool, chat_pool, terms, slugs)
    searched = {"prs": len(pr_pool), "chats": len(chat_pool), "repos": sorted(slugs), "since": since.isoformat(),
                "until": until.isoformat() if until else None, "widened": widened, "keywords": u["keywords"][:8]}
    if not cands:
        return {"suggestions": [], "narrow_down": "", "searched": searched, "notes": [*notes, "no PRs or chats in that window — try a wider time range"]}

    progress(f"ranking {len(cands)} candidates…")
    try:
        matches = _haiku(RANK_PROMPT, _rank_input(text, cands), RANK_SCHEMA).get("matches") or []
    except Exception as e:  # noqa: BLE001
        matches = []
        notes.append(f"Haiku ranking failed ({str(e)[:80]}) — showing the best keyword matches")
    by_id = {c["id"]: c for c in cands}
    ranked = [by_id[m["id"]] for m in sorted(matches, key=lambda m: -int(m.get("score") or 0)) if m.get("id") in by_id]
    why = {m["id"]: m.get("why") or "" for m in matches if m.get("id") in by_id}
    if not ranked:  # fall back to the local pre-score
        ranked = sorted(cands, key=lambda c: -c["pre"])[:MAX_SUGGESTIONS]
    short = ranked[: DEEP_PRS + DEEP_CHATS]

    progress("checking the top candidates…")
    _enrich(short)
    verdict: dict = {}
    try:
        verdict = _haiku(VERIFY_PROMPT, _verify_input(text, short), VERIFY_SCHEMA)
    except Exception as e:  # noqa: BLE001
        notes.append(f"Haiku verification failed ({str(e)[:80]}) — showing the first-pass ranking")
    out_s = []
    for s in verdict.get("suggestions") or []:
        c = next((x for x in short if x["id"] == s.get("id")), None)
        if c:
            out_s.append(_suggestion(c, s.get("confidence") or "low", s.get("why") or why.get(c["id"], "")))
    if not out_s:
        out_s = [_suggestion(c, "low", why.get(c["id"], "matched your keywords")) for c in short[:MAX_SUGGESTIONS]]
    return {"suggestions": out_s[:MAX_SUGGESTIONS], "narrow_down": (verdict.get("narrow_down") or "").strip(), "searched": searched, "notes": notes}


# --- candidates ------------------------------------------------------------------------------
def _linked_chats(pr: dict, chat_pool: list[dict], proj_of_slug: dict[str, str]) -> list[dict]:
    """The loom chats tied to a PR: its number is among the chat's PRs (same repo), or it's the chat's branch."""
    out = []
    for c in chat_pool:
        slug = c.get("pr_repo") or next((s for s, p in proj_of_slug.items() if p == c.get("repo")), None)
        if slug == pr["repo"] and pr["number"] in {int(x) for x in c.get("prs") or [] if str(x).isdigit()}:
            out.append(c)
        elif pr["branch"] and c.get("branch") == pr["branch"] and proj_of_slug.get(pr["repo"]) == c.get("repo"):
            out.append(c)
    return out


def _build_candidates(pr_pool: list[dict], chat_pool: list[dict], terms: list[str], proj_of_slug: dict[str, str]) -> list[dict]:
    cands: list[dict] = []
    linked_chat_ids: set[str] = set()
    for p in pr_pool:
        chats = _linked_chats(p, chat_pool, proj_of_slug)
        linked_chat_ids |= {c["id"] for c in chats}
        chat_text = " ".join(f"{c.get('display_title', '')} {c.get('first_prompt') or ''}" for c in chats)
        pre = _score(p["title"], terms, 3) + _score(p["branch"], terms, 2) + _score(p["body"], terms) + _score(chat_text, terms)
        cands.append({"kind": "pr", "pr": p, "chats": chats, "pre": pre})
    # chats nothing links to a fetched PR (the PR may be older, unlinked, or never opened) compete on their own text
    for c in chat_pool:
        text = " ".join([c.get("display_title") or "", c.get("first_prompt") or "", c.get("preview") or "", c.get("branch") or "",
                         c.get("description") or "", " ".join(c.get("tags") or [])])
        pre = _score(text, terms, 2) + (_score(_brief_goal(c["id"]), terms) if terms else 0)
        cands.append({"kind": "chat", "pr": None, "chats": [c], "pre": pre, "solo": c["id"] not in linked_chat_ids})
    prs = sorted([c for c in cands if c["kind"] == "pr"], key=lambda c: (-c["pre"], -(c["pr"]["created"] or 0)))
    chats = sorted([c for c in cands if c["kind"] == "chat" and c["solo"] and c["pre"] > 0], key=lambda c: -c["pre"])
    top_prs = [c for c in prs if c["pre"] > 0][:MAX_PR_CANDIDATES]
    if len(top_prs) < 25:  # too few keyword hits: pad with the newest, so the model still sees recent work
        seen = {c["pr"]["key"] for c in top_prs}
        top_prs += [c for c in sorted(prs, key=lambda c: -(c["pr"]["created"] or 0)) if c["pr"]["key"] not in seen][: 25 - len(top_prs)]
    chosen = top_prs + chats[:MAX_CHAT_CANDIDATES]
    for i, c in enumerate([c for c in chosen if c["kind"] == "pr"], 1):
        c["id"] = f"P{i}"
    for i, c in enumerate([c for c in chosen if c["kind"] == "chat"], 1):
        c["id"] = f"C{i}"
    return chosen


def _chat_line(c: dict, goal: bool = True) -> str:
    g = _clip(_brief_goal(c["id"]), 160) if goal else ""
    prs = ",".join(f"#{n}" for n in (c.get("prs") or [])[:4]) or "none linked"
    return (f'chat "{_clip(c.get("display_title"), 70)}" ({"archived" if c.get("archived") else "active"}, {c.get("repo") or "?"}, '
            f'last active {_day(c.get("last_active"))}, branch {c.get("branch") or "?"}, PRs: {prs})'
            + (f' | first prompt: "{_clip(c.get("first_prompt"), 140)}"' if c.get("first_prompt") else "") + (f' | goal: "{g}"' if g else ""))


def _rank_input(text: str, cands: list[dict]) -> str:
    lines = []
    for c in cands:
        if c["kind"] == "pr":
            p = c["pr"]
            ch = f' | chat: {_chat_line(c["chats"][0], goal=False)}' if c["chats"] else ""
            lines.append(f'{c["id"]} | PR {p["key"]} "{_clip(p["title"], 90)}" | branch {p["branch"] or "?"} | {_day(p["created"])} {p["state"]} '
                         f'| +{p["additions"] or 0}/-{p["deletions"] or 0} in {p["files"] or "?"} files | "{_clip(p["body"], 160)}"{ch}')
        else:
            lines.append(f'{c["id"]} | {_chat_line(c["chats"][0])}')
    return f"WHAT THE DEVELOPER REMEMBERS:\n{text}\n\nCANDIDATES:\n" + "\n".join(lines)


def _pr_detail(pr: dict) -> dict:
    data = github._gh_json(["pr", "view", str(pr["number"]), "--repo", pr["repo"], "--json", "title,body,files"])
    if isinstance(data, dict):
        pr["body_full"] = data.get("body") or pr["body"]
        pr["paths"] = [f.get("path") for f in (data.get("files") or []) if isinstance(f, dict) and f.get("path")][:25]
    return pr


def _enrich(short: list[dict]) -> None:
    """Fuller detail for the short-list: a PR's changed files + description; for a chat with no PR, the PRs on its branch."""
    def one(c: dict) -> None:
        if c["kind"] == "pr":
            _pr_detail(c["pr"])
        elif c["chats"]:
            ch = c["chats"][0]
            slug = ch.get("pr_repo") or next((s for s, p in _project_slugs(None).items() if p == ch.get("repo")), None)
            if slug and ch.get("branch"):
                c["branch_prs"] = github.prs_for_branch(slug, ch["branch"])[:3]
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(one, short))


def _verify_input(text: str, short: list[dict]) -> str:
    out = []
    for c in short:
        if c["kind"] == "pr":
            p = c["pr"]
            ch = f'\n    chat: {_chat_line(c["chats"][0])}' if c["chats"] else ""
            out.append(f'{c["id"]} | PR {p["key"]} "{p["title"]}" | branch {p["branch"] or "?"} | {_day(p["created"])} {p["state"]}\n'
                       f'    description: "{_clip(p.get("body_full") or p["body"], 700)}"\n    changed files: {", ".join(p.get("paths") or []) or "(unknown)"}{ch}')
        else:
            bp = "; ".join(f'#{x["number"]} "{_clip(x.get("title"), 60)}"' for x in c.get("branch_prs") or []) or "none found"
            out.append(f'{c["id"]} | {_chat_line(c["chats"][0])}\n    PRs on its branch: {bp}')
    return f"WHAT THE DEVELOPER REMEMBERS:\n{text}\n\nSHORT-LIST:\n" + "\n".join(out)


def _suggestion(c: dict, confidence: str, why: str) -> dict:
    chats = [{"id": x["id"], "name": x.get("name") or x.get("display_title") or x["id"][:8], "archived": bool(x.get("archived")),
              "project": x.get("repo"), "branch": x.get("branch"), "last_active": x.get("last_active"), "agent": x.get("agent"),
              "cwd": x.get("cwd"), "mode": x.get("mode")} for x in c["chats"][:3]]
    pr = None
    if c["pr"]:
        p = c["pr"]
        pr = {k: p.get(k) for k in ("key", "repo", "number", "title", "url", "state", "branch", "created", "merged", "additions", "deletions", "files")}
    elif c.get("branch_prs"):
        p = c["branch_prs"][0]
        pr = {"key": None, "number": p["number"], "title": p.get("title"), "url": p.get("url"), "state": p.get("state"), "created": p.get("created_at")}
    return {"kind": c["kind"], "confidence": confidence, "why": why, "pr": pr, "chats": chats, "id": c["id"]}
