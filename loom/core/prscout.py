"""PR scout — catches the PRs a chat opened that the transcript scraper missed (D29).

Linking PRs to chats from the transcript alone (sessions._PrEvidence, D19) misses PRs whose URL never
showed up in the agent's tool output: `gh pr create … > /dev/null`, a PR opened through an API/MCP call,
a PR the agent mentions as "#123" without a link, one opened in a shell the chat didn't run. While you're
ON a chat the dashboard asks for a scan every minute; each scan has two tiers, cheapest first:

  1. branch  (free, deterministic) — GitHub says which PRs have this chat's branch as their head. Covers
     the common case with no model at all. Counted if the PR is open, or was created after the chat's task
     (an old merged PR on a reused branch name is not this chat's work).
  2. read    (one cheap Haiku call, only when there is something to read) — PRs the recent conversation
     mentions that no evidence links yet. Candidates are filtered by GitHub first (the PR must exist and
     have been created after this chat's task began), and Haiku is asked only: did THIS agent open it, or
     merely mention / review / get told about it? Its "yes" is then re-checked against GitHub's facts.
     Gated hard: only when new candidates exist, at most one read per 3 minutes and 40 a day per chat.

Found PRs are stored on the chat (`prs_scouted`) and show as normal PR chips (hover: "found by loom's PR
scout"; × removes one for good — it won't be re-added). Never raises; a missing `gh` or `claude` just means
fewer finds. Claude transcripts only for tier 2 (codex/grok chats still get tier 1).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from datetime import datetime

from loom.core import briefs, db, github, manager, prlink, sessions

THROTTLE_S = 45.0          # never scan one chat more often than this (several tabs, restarts…)
HAIKU_MIN_GAP_S = 180.0
HAIKU_DAILY_CAP = 40
TASK_SLACK_S = 300.0       # a PR "created after the task" may predate it by this much (clock skew)
TAIL_BYTES = 160_000       # how much of the transcript's end is looked at
TAIL_CHARS = 14_000        # …compressed to this much text for the model
MAX_CANDIDATES = 8
BASE_BRANCHES = {"main", "master", "develop", "HEAD", ""}

_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
_SHORT_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d{1,6})\b")
_NUM_RE = re.compile(r"(?:\bPR\s*#?|\bpull request\s*#?|(?<![\w/])#)(\d{1,6})\b", re.I)
_inflight: set[str] = set()
_lock = threading.Lock()

SCHEMA = {
    "type": "object",
    "properties": {"prs": {"type": "array", "items": {
        "type": "object",
        "properties": {"ref": {"type": "string"}, "created_by_this_agent": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["ref", "created_by_this_agent", "reason"],
    }}},
    "required": ["prs"],
}
PROMPT = """You examine the recent timeline of ONE coding agent's session. It mentions some GitHub pull requests. \
For each candidate PR listed first in the user message, decide whether THIS agent created (opened) it during this \
session — it ran `gh pr create`, opened it through an API/tool, or pushed a branch and says it opened/created the PR — \
as opposed to merely mentioning, reading, reviewing, checking, merging or being told about a PR that someone else \
(or another agent) opened. When the timeline doesn't clearly show this agent creating it, answer false. \
Answer with one entry per candidate, echoing its `ref` exactly, and a short reason."""


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


# --- per-chat bookkeeping (loom.db `pr_scout`) ------------------------------------------------
def _state(chat_id: str) -> dict:
    with db.connect() as con:
        r = con.execute("SELECT * FROM pr_scout WHERE chat_id=?", (chat_id,)).fetchone()
    if not r:
        return {"scanned": 0.0, "haiku_at": 0.0, "haiku_day": "", "haiku_n": 0, "judged": {}}
    d = {k: r[k] for k in r.keys()}
    try:
        d["judged"] = json.loads(d["judged"] or "{}")
    except json.JSONDecodeError:
        d["judged"] = {}
    return d


def _save(chat_id: str, st: dict) -> None:
    with db.connect() as con:
        con.execute(
            "INSERT INTO pr_scout (chat_id, scanned, haiku_at, haiku_day, haiku_n, judged) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET scanned=excluded.scanned, haiku_at=excluded.haiku_at, "
            "haiku_day=excluded.haiku_day, haiku_n=excluded.haiku_n, judged=excluded.judged",
            (chat_id, st["scanned"], st["haiku_at"], st["haiku_day"], st["haiku_n"], json.dumps(st["judged"])),
        )


# --- reading the conversation (tier 2 input) --------------------------------------------------
def _tail_text(path) -> str:
    """The end of a Claude transcript, compressed with the same rules as the status briefs (your messages,
    the agent's prose, one line per tool call; tool output dropped), newest last, capped."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines: list[str] = []
    asks: set = set()
    for i, ln in enumerate(raw.splitlines()):
        if i == 0 and size > TAIL_BYTES:
            continue  # a partial first line
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        try:
            when = datetime.fromisoformat(rec.get("timestamp", "").replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        lines.extend(briefs._record_lines(rec, when.strftime("%m-%d %H:%M"), asks) or [])
    text = "\n".join(lines)
    return text[-TAIL_CHARS:]


def _raw_signals(path) -> str:
    """Text to mine for PR mentions: the compressed tail PLUS tool output that mentions a PR (the compressed
    view drops tool output, but `gh pr create` prints the URL there)."""
    out = [_tail_text(path)]
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            for ln in f.read().decode("utf-8", "replace").splitlines():
                if "/pull/" in ln:
                    out.extend(m.group(0) for m in _URL_RE.finditer(ln))
    except OSError:
        pass
    return "\n".join(out)


def _candidates(text: str, default_repo: str) -> list[str]:
    """"owner/repo#n" keys the text mentions (full PR URLs, and bare #N / "PR N" in the chat's repo)."""
    keys: list[str] = []
    for repo, n in _URL_RE.findall(text):
        keys.append(f"{repo}#{int(n)}")
    for repo, n in _SHORT_RE.findall(text):
        keys.append(f"{repo}#{int(n)}")
    for n in _NUM_RE.findall(text):
        keys.append(f"{default_repo}#{int(n)}")
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _ask_haiku(timeline: str, cands: list[dict]) -> dict[str, bool]:
    """{key: did this agent create it?} from one Haiku read; {} if the call failed."""
    listing = "\n".join(f"- {c['key']}  \"{c['title'] or ''}\"" for c in cands)
    answer, _meta = briefs.haiku_json(PROMPT, f"CANDIDATE PRs:\n{listing}\n\nRECENT TIMELINE:\n{timeline}", SCHEMA, timeout=120)
    verdicts: dict[str, bool] = {}
    for e in answer.get("prs") or []:
        if isinstance(e, dict) and e.get("ref"):
            verdicts[str(e["ref"]).strip()] = bool(e.get("created_by_this_agent"))
    return verdicts


# --- the scan -----------------------------------------------------------------------------
def scan(chat_id: str, *, force: bool = False) -> dict:
    """One scout pass over a chat → {"found": [{key, number, title, source}], "haiku": bool, "skipped": str|None}."""
    with _lock:
        if chat_id in _inflight:
            return {"found": [], "haiku": False, "skipped": "already running"}
        _inflight.add(chat_id)
    try:
        return _scan(chat_id, force)
    except Exception as e:  # noqa: BLE001 — a scout must never take anything down with it
        return {"found": [], "haiku": False, "skipped": f"error: {str(e)[:200]}"}
    finally:
        with _lock:
            _inflight.discard(chat_id)


def _scan(chat_id: str, force: bool) -> dict:
    now = time.time()
    st = _state(chat_id)
    if not force and now - st["scanned"] < THROTTLE_S:
        return {"found": [], "haiku": False, "skipped": "throttled"}
    st["scanned"] = now
    _save(chat_id, st)

    task = manager.task_for_chat(chat_id)
    raw = sessions.get_chat(chat_id) or {}
    ov = sessions.get_overlay(chat_id)
    wt = (task.worktree_path if task else None) or raw.get("cwd")
    repo = github.repo_for_dir(wt)
    if not repo:
        return {"found": [], "haiku": False, "skipped": "no GitHub repo for this chat's directory"}
    start = (_epoch(task.created_at) if task else None) or _epoch(raw.get("created")) or 0.0
    start -= TASK_SLACK_S
    hidden = set(ov.get("prs_hidden") or [])
    known = set(prlink.chat_pr_keys(raw, ov)) | hidden
    found: list[dict] = []

    def add(key: str, source: str, title: str | None) -> None:
        if key in known or any(f["key"] == key for f in found):
            return
        found.append({"key": key, "number": int(key.rpartition("#")[2]), "title": title, "source": source})

    # tier 1 — PRs whose head is this chat's branch (free, deterministic)
    branches = {b for b in ((task.branch if task else None), raw.get("branch"), _current_branch(wt)) if b}
    branches -= BASE_BRANCHES | ({task.base_branch} if task else set())
    for br in sorted(branches):
        for p in github.prs_for_branch(repo, br):
            if p["state"] == "open" or (p["created_at"] or 0) >= start:
                add(f"{repo}#{p['number']}", "branch", p.get("title"))

    # tier 2 — read the recent conversation, only when there is something new to judge
    haiku = False
    path = sessions._find_transcript(chat_id) if (raw.get("agent") or "claude") == "claude" else None
    if path and path.exists():
        pending = [k for k in _candidates(_raw_signals(path), repo) if k not in known and k not in st["judged"] and not any(f["key"] == k for f in found)]
        cands = []
        for k in pending[:MAX_CANDIDATES * 2]:
            r, _, n = k.rpartition("#")
            ps = github.pr_status(r, int(n))
            if ps.get("state") == "unknown":
                continue  # not a PR (an issue / commit number), or gh can't say
            if (ps.get("created_at") or 0) < start:
                st["judged"][k] = "predates-chat"  # an older PR it merely mentions — never this chat's
                continue
            cands.append({"key": k, "title": ps.get("title"), "status": ps})
        cands = cands[:MAX_CANDIDATES]
        today = datetime.now().strftime("%Y-%m-%d")
        if st["haiku_day"] != today:
            st["haiku_day"], st["haiku_n"] = today, 0
        if cands and now - st["haiku_at"] >= HAIKU_MIN_GAP_S and st["haiku_n"] < HAIKU_DAILY_CAP:
            st["haiku_at"], st["haiku_n"] = now, st["haiku_n"] + 1
            _save(chat_id, st)  # count it before the call, so a crash can't turn this into a loop
            haiku = True
            try:
                verdicts = _ask_haiku(_tail_text(path), cands)
            except Exception:  # noqa: BLE001 — retry on a later scan; nothing judged
                verdicts = {}
            for c in cands:
                if c["key"] not in verdicts:
                    continue
                st["judged"][c["key"]] = "created" if verdicts[c["key"]] else "mentioned"
                if verdicts[c["key"]]:
                    add(c["key"], "read", c["title"])

    _save(chat_id, st)
    if found:
        sessions.set_overlay(chat_id, {"prs_scouted": sorted(set(ov.get("prs_scouted") or []) | {f["key"] for f in found})})
    return {"found": found, "haiku": haiku, "skipped": None}


def _current_branch(wt: str | None) -> str | None:
    if not wt:
        return None
    try:
        p = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() or None if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
