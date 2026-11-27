"""A searchable index of every loom session — built in the background (D33).

"Which chat was the one where we added that export button?" With hundreds of chats (most archived), neither titles nor GitHub can answer
it; the answer lives in the conversations. So a background worker keeps, per session, in SQLite (`~/.loom/loom.db`):

  facts    (no model, instant)  files edited, commit messages, `gh pr create` titles, your first and last messages — pulled straight from the
                                transcript's tool calls. Already enough to find "the session that touched ExportButton.tsx".
  summary  (one cheap Haiku call per session, newest first)  a goal, 2-4 sentences of what was done, keywords/synonyms, short "done" bullets
                                and an outcome — written from a compressed timeline of the conversation.

Both feed one FTS5 full-text table (porter-stemmed, bm25-ranked) that "Find a session" (core/findpr.py) queries first, before any model sees
a candidate. `snippets()` then greps the raw transcript of the few finalists for quotable evidence. Nothing here is ever required to be
complete: sessions not indexed yet are still found through their title / first prompt, and the status says how far along the index is.

Cost / safety knobs: summaries are generated only for sessions that have been quiet for 10 minutes, at most `LOOM_INDEX_DAILY` (default 200)
per day, one at a time, newest first; a session is re-summarized only after its transcript has grown ≥25% (and ≥20 KB) and 6 h have passed;
failures back off, and the whole thing can be paused (`loom index pause`, the Find modal, or `LOOM_INDEXER=0`). Summaries are made from the
compressed conversation timeline (not tool output), the same input as the status briefs, through your own `claude` login.
"""

from __future__ import annotations

import heapq
import json
import os
import re
import threading
import time
from collections import Counter, deque
from datetime import date, datetime
from pathlib import Path

from loom.core import briefs, db, sessions

DAILY_CAP = int(os.environ.get("LOOM_INDEX_DAILY", "200"))
ENABLED = os.environ.get("LOOM_INDEXER", "1") != "0"
SETTLE_S = 600            # a session must be quiet this long before it's summarized (never mid-work)
FACTS_SETTLE_S = 120
REGROW = 1.25             # re-summarize once the transcript is this much bigger …
REGROW_MIN_BYTES = 20_000  # … and at least this much bigger
MIN_RESUMMARY_S = 6 * 3600
RETRY_S = 1800            # after a failed summary
TICK_S = 45
START_DELAY_S = 90
FACTS_PER_TICK = 6
BUSY_TICK_S = 3           # while there's facts work waiting
AFTER_SUMMARY_S = 15      # breathing room after each Haiku call (≈4 a minute at most)
INPUT_HEAD, INPUT_TAIL = 9_000, 27_000   # chars of compressed timeline sent to Haiku
HEAD_BYTES, TAIL_BYTES = 3_000_000, 6_000_000
MAX_FAILS = 5             # consecutive failures before a one-hour pause

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_HEREDOC = re.compile(r"<<-?\s*['\"]?\w+['\"]?\s*\n(.+?)\n", re.S)
_DASH_M = re.compile(r"""-m\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")
_TITLE = re.compile(r"""--title\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")


# --- small key/value state ---------------------------------------------------------------
def meta_get(key: str, default: str = "") -> str:
    with db.connect() as con:
        r = con.execute("SELECT val FROM index_meta WHERE key = ?", (key,)).fetchone()
    return r["val"] if r else default


def meta_set(key: str, val: str) -> None:
    with db.connect() as con:
        con.execute("INSERT INTO index_meta (key, val) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET val = excluded.val", (key, str(val)))


def is_paused() -> bool:
    return meta_get("paused") == "1"


def set_paused(p: bool) -> None:
    meta_set("paused", "1" if p else "0")


# --- facts: deterministic, from the transcript's tool calls ------------------------------
def _role_in(line: str, role: str) -> bool:
    """Cheap pre-filter: does this JSONL line carry a message with that role? (compact or spaced JSON)"""
    return f'"role":"{role}"' in line or f'"role": "{role}"' in line


def _user_text(rec: dict) -> str:
    if rec.get("isMeta"):
        return ""
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    c = (c or "").strip()
    return "" if (not c or c.startswith("<")) else re.sub(r"\s+", " ", c)


def _commit_msgs(cmd: str) -> list[str]:
    out = []
    for m in re.finditer(r"git\s+(?:-C\s+\S+\s+)?commit\b", cmd):
        seg = cmd[m.start(): m.start() + 2000]
        h = _HEREDOC.search(seg)
        if h:
            out.append(h.group(1).strip())
            continue
        d = _DASH_M.search(seg)
        if d:
            out.append(((d.group(1) or d.group(2) or "").strip().splitlines() or [""])[0])
    return [x for x in out if x]


def _pr_titles(cmd: str) -> list[str]:
    if "gh pr create" not in cmd:
        return []
    return [(m.group(1) or m.group(2) or "").strip() for m in _TITLE.finditer(cmd) if (m.group(1) or m.group(2))]


def scan_facts(path: Path) -> dict:
    """Files edited, commit messages, PR titles, first/last user messages — one pass over a Claude transcript, parsing only the lines
    that can matter (most of a 100 MB transcript is tool output we never look at)."""
    files: Counter = Counter()
    commits: list[str] = []
    pr_titles: list[str] = []
    first_users: list[str] = []
    last_users: deque = deque(maxlen=3)
    n_user, first_ts = 0, None
    with path.open("r", errors="replace") as f:
        for line in f:
            if '"tool_use"' in line:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                first_ts = first_ts or rec.get("timestamp")
                for b in ((rec.get("message") or {}).get("content") or []) if isinstance((rec.get("message") or {}).get("content"), list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") in _EDIT_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            files[fp] += 1
                    elif b.get("name") == "Bash":
                        cmd = inp.get("command") or ""
                        if "git" in cmd and "commit" in cmd:
                            commits += _commit_msgs(cmd)
                        pr_titles += _pr_titles(cmd)
            elif _role_in(line, "user") and '"tool_result"' not in line:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                first_ts = first_ts or rec.get("timestamp")
                t = _user_text(rec)
                if t:
                    n_user += 1
                    if len(first_users) < 3:
                        first_users.append(t[:300])
                    last_users.append(t[:300])
    short = lambda p: "/".join(p.split("/")[-4:])  # noqa: E731
    return {
        "files": [short(p) for p, _ in files.most_common(25)],
        "commits": list(dict.fromkeys(commits))[:12],
        "pr_titles": list(dict.fromkeys(pr_titles))[:6],
        "user_first": first_users, "user_last": list(last_users) if n_user > 3 else [],
        "n_user": n_user, "first_ts": first_ts,
    }


# --- the text that gets indexed -----------------------------------------------------------
def _split_camel(s: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)


def _body(chat: dict, facts: dict, ix: dict | None) -> str:
    parts = [chat.get("display_title") or "", chat.get("name") or "", chat.get("branch") or "", " ".join(chat.get("tags") or []),
             chat.get("description") or "", chat.get("first_prompt") or "", chat.get("preview") or ""]
    if ix:
        parts += [ix.get("goal") or "", ix.get("summary") or "", ix.get("keywords") or "", " ".join(ix.get("done") or []), ix.get("outcome") or ""]
    parts += facts.get("commits", []) + facts.get("pr_titles", []) + facts.get("user_first", []) + facts.get("user_last", [])
    parts += [f"{p} {_split_camel(p)}" for p in facts.get("files", [])]
    try:  # an existing status brief is free extra signal
        b = (briefs._read(chat["id"]) or {}).get("brief") or {}
        parts += [b.get("goal") or "", *(ph.get("text", "") for ph in b.get("phases") or [] if isinstance(ph, dict))]
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(p for p in parts if p)


def _row(chat_id: str) -> dict | None:
    with db.connect() as con:
        r = con.execute("SELECT rowid AS rid, * FROM session_index WHERE chat_id = ?", (chat_id,)).fetchone()
    if not r:
        return None
    d = {k: r[k] for k in r.keys()}
    for k in ("facts", "done"):
        try:
            d[k] = json.loads(d[k] or ("{}" if k == "facts" else "[]"))
        except json.JSONDecodeError:
            d[k] = {} if k == "facts" else []
    return d


def get(chat_id: str) -> dict | None:
    """The stored summary + facts for a session, or None if it hasn't been indexed."""
    return _row(chat_id)


def _write(chat: dict, *, facts: dict | None = None, facts_size: int | None = None, summary: dict | None = None, src_size: int | None = None,
           cost: float = 0.0, error: str | None = None) -> None:
    cur = _row(chat["id"]) or {}
    f = facts if facts is not None else cur.get("facts") or {}
    s = {**(cur or {}), **(summary or {})}
    with db.connect() as con:
        con.execute("INSERT INTO session_index (chat_id) VALUES (?) ON CONFLICT(chat_id) DO NOTHING", (chat["id"],))
        sets, vals = [], []
        if facts is not None:
            sets += ["facts = ?", "facts_size = ?", "facts_at = ?"]
            vals += [json.dumps(f), facts_size if facts_size is not None else 0, time.time()]
        if summary is not None:
            sets += ["goal = ?", "summary = ?", "keywords = ?", "done = ?", "outcome = ?", "src_size = ?", "summarized = ?", "cost = cost + ?", "error = '', error_at = 0"]
            vals += [summary.get("goal", ""), summary.get("summary", ""), " ".join(summary.get("keywords") or []), json.dumps(summary.get("done") or []),
                     summary.get("outcome", ""), src_size or 0, time.time(), cost]
        if error is not None:
            sets += ["error = ?", "error_at = ?"]
            vals += [error[:300], time.time()]
        if sets:
            con.execute(f"UPDATE session_index SET {', '.join(sets)} WHERE chat_id = ?", [*vals, chat["id"]])
        rid = con.execute("SELECT rowid FROM session_index WHERE chat_id = ?", (chat["id"],)).fetchone()[0]
        if facts is not None or summary is not None:
            ix = {"goal": s.get("goal"), "summary": s.get("summary"), "keywords": s.get("keywords") if isinstance(s.get("keywords"), str) else " ".join(s.get("keywords") or []),
                  "done": s.get("done") if isinstance(s.get("done"), list) else [], "outcome": s.get("outcome")}
            con.execute("DELETE FROM session_fts WHERE rowid = ?", (rid,))
            con.execute("INSERT INTO session_fts (rowid, body) VALUES (?, ?)", (rid, _body(chat, f, ix)))


# --- search ----------------------------------------------------------------------------------
def _fts_query(terms: list[str]) -> str:
    toks: list[str] = []
    for t in terms:
        for w in re.findall(r"[A-Za-z0-9]+", t.lower()):
            if len(w) >= 2 and w not in toks:
                toks.append(w)
    return " OR ".join(f'"{w}"*' for w in toks[:24])


def search(terms: list[str], limit: int = 60) -> dict[str, float]:
    """{chat_id: relevance} for the sessions whose indexed text matches any term (bm25, best first, higher = better)."""
    q = _fts_query(terms)
    if not q:
        return {}
    try:
        with db.connect() as con:
            rows = con.execute(
                "SELECT si.chat_id AS cid, bm25(session_fts) AS s FROM session_fts JOIN session_index si ON si.rowid = session_fts.rowid "
                "WHERE session_fts MATCH ? ORDER BY s LIMIT ?", (q, limit)).fetchall()
    except Exception:  # noqa: BLE001 — a malformed query must not break the search
        return {}
    return {r["cid"]: -float(r["s"]) for r in rows}


def snippets(path: Path | None, terms: list[str], n: int = 3, width: int = 110) -> list[dict]:
    """Up to `n` quotable lines from the raw transcript that mention the terms (your messages and the agent's prose — not tool output), best
    first: [{ts, role, text}]. The "search the session logs" part: deterministic, local, and bounded for giant transcripts."""
    if not path or not terms:
        return []
    words = [w for t in terms for w in re.findall(r"[a-z0-9]{3,}", t.lower())][:20]
    if not words:
        return []
    best: list[tuple[int, int, dict]] = []
    seq = 0
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            chunks = [f.read()] if size <= HEAD_BYTES + TAIL_BYTES else [f.read(HEAD_BYTES), (f.seek(size - TAIL_BYTES), f.read())[1]]
    except OSError:
        return []
    for chunk in chunks:
        for raw in chunk.decode("utf-8", "replace").splitlines():
            low = raw.lower()
            if not any(w in low for w in words) or not (_role_in(raw, "user") or _role_in(raw, "assistant")):
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message") or {}
            c = msg.get("content")
            texts = [c] if isinstance(c, str) else [b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"]
            for t in texts:
                tl = t.lower()
                hit = [w for w in words if w in tl]
                if not hit or t.lstrip().startswith("<"):
                    continue
                at = tl.find(hit[0])
                s = max(0, at - width)
                snip = re.sub(r"\s+", " ", t[s: at + width]).strip()
                seq += 1
                heapq.heappush(best, (len(set(hit)), seq, {"ts": rec.get("timestamp"), "role": msg.get("role") or rec.get("type"),
                                                           "text": ("…" if s else "") + snip + ("…" if at + width < len(t) else "")}))
                if len(best) > n * 4:
                    heapq.heappop(best)
    return [x[2] for x in sorted(best, key=lambda x: (-x[0], x[1]))[:n]]


# --- building the index ----------------------------------------------------------------------
def _transcript(chat: dict) -> Path | None:
    if (chat.get("agent") or "claude") != "claude":
        return None
    try:
        p = sessions._find_transcript(chat["id"])
        return p if p and p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def index_facts(chat: dict) -> None:
    """The deterministic pass for one session (no model)."""
    p = _transcript(chat)
    if p is None:  # codex/grok (or no transcript yet): index what the row itself holds
        _write(chat, facts={}, facts_size=-1)
        return
    size = p.stat().st_size
    _write(chat, facts=scan_facts(p), facts_size=size)


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"}, "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}}, "done": {"type": "array", "items": {"type": "string"}},
        "outcome": {"type": "string", "enum": ["shipped", "in progress", "abandoned", "exploration", "unclear"]},
    },
    "required": ["goal", "summary", "keywords", "done", "outcome"],
}
SUMMARY_PROMPT = """You index one coding-agent session so the developer can find it again months later from a vague memory. The user \
message has FACTS pulled from the session's tool calls (commit messages, PR titles, files edited) and a compressed TIMELINE of the \
conversation (head and tail; the middle may be omitted). Write: `goal` — what the developer set out to do (one sentence); `summary` — 2-4 \
sentences on what was actually built, fixed, decided or learned, with concrete nouns (feature names, UI elements, pages, file/module names, \
bugs); `keywords` — up to 15 lowercase search terms someone might later remember this by, INCLUDING synonyms and likely wording (e.g. button → \
btn, cta; modal → dialog, popup) and domain terms; `done` — up to 8 short bullets of concrete things completed; `outcome` — shipped (merged / \
delivered), in progress, abandoned, exploration, or unclear. Be specific, not generic; never invent what the transcript doesn't show."""


def _timeline_text(path: Path) -> str:
    """The conversation compressed like the status briefs (your messages, the agent's prose, one line per tool call), from the head and tail of the
    file so a 150 MB session costs what a 5 MB one does."""
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(HEAD_BYTES)
        tail = b""
        if size > HEAD_BYTES + TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            tail = f.read()
    asks: set = set()

    def lines(raw: bytes, drop_first: bool, drop_last: bool) -> str:
        ls = raw.decode("utf-8", "replace").splitlines()
        ls = ls[1 if drop_first else 0: len(ls) - (1 if drop_last else 0)]
        out: list[str] = []
        for ln in ls:
            try:
                rec = json.loads(ln)
                when = datetime.fromisoformat(rec.get("timestamp", "").replace("Z", "+00:00")).astimezone()
            except (ValueError, json.JSONDecodeError):
                continue
            out.extend(briefs._record_lines(rec, when.strftime("%m-%d %H:%M"), asks) or [])
        return "\n".join(out)

    h = lines(head, False, bool(tail))
    if not tail:
        return h if len(h) <= INPUT_HEAD + INPUT_TAIL else h[:INPUT_HEAD] + "\n[… middle omitted …]\n" + h[-INPUT_TAIL:]
    t = lines(tail, True, False)
    return h[:INPUT_HEAD] + "\n[… middle omitted …]\n" + t[-INPUT_TAIL:]


def _haiku(text: str) -> tuple[dict, dict]:
    return briefs.haiku_json(SUMMARY_PROMPT, text, SUMMARY_SCHEMA, timeout=150)


def summarize(chat: dict) -> bool:
    """One Haiku summary for a session. Returns whether it worked (failures are recorded and back off)."""
    p = _transcript(chat)
    if p is None:
        return False
    facts = (_row(chat["id"]) or {}).get("facts") or {}
    try:
        size = p.stat().st_size
        facts_txt = "\n".join([
            "commits: " + "; ".join(facts.get("commits", [])[:8]), "PRs opened: " + "; ".join(facts.get("pr_titles", [])),
            "files edited: " + ", ".join(facts.get("files", [])[:15]), "branch: " + str(chat.get("branch") or "?")])
        out, meta = _haiku(f"FACTS:\n{facts_txt}\n\nTIMELINE:\n{_timeline_text(p)}")
        _write(chat, summary=out, src_size=size, cost=float((meta or {}).get("cost_usd") or 0))
        return True
    except Exception as e:  # noqa: BLE001
        _write(chat, error=str(e))
        return False


# --- the background worker -------------------------------------------------------------------
_worker: threading.Thread | None = None
_stop = threading.Event()
_state = {"running": None, "last_error": "", "ticks": 0}


def _today_used() -> int:
    return int(meta_get("day_n", "0")) if meta_get("day") == date.today().isoformat() else 0


def _bump_today() -> None:
    d = date.today().isoformat()
    meta_set("day_n", str(_today_used() + 1))
    meta_set("day", d)


def _chats() -> list[dict]:
    return sorted((c for c in sessions.list_chats(scope="all", tab="all") if c.get("task") and not c.get("deleted")),
                  key=lambda c: -(c.get("last_active") or 0))


def _all_index_rows() -> dict[str, dict]:
    with db.connect() as con:
        rs = con.execute("SELECT chat_id, facts_size, facts_at, src_size, summarized, error_at FROM session_index").fetchall()
    return {r["chat_id"]: {k: r[k] for k in r.keys()} for r in rs}


def tick() -> dict:
    """One pass: refresh a few sessions' facts, then (if allowed) summarize the best waiting session. Returns what it did."""
    did = {"facts": 0, "summarized": 0}
    chats = _chats()
    rows = _all_index_rows()
    meta_set("total", str(len(chats)))
    now = time.time()
    # 1) facts — cheap and deterministic, newest sessions first
    todo = []
    for c in chats:
        ix = rows.get(c["id"])
        size = c.get("size") or 0
        if ix is None or not ix["facts_at"]:
            todo.append(c)
        elif ix["facts_size"] < 0 and (c.get("agent") or "claude") == "claude" and size > 0 and now - ix["facts_at"] > 600:
            todo.append(c)  # had no transcript when first seen (a brand-new chat) — look again
        elif ix["facts_size"] >= 0 and now - (c.get("last_active") or 0) > FACTS_SETTLE_S and size >= ix["facts_size"] * 1.3 and size - ix["facts_size"] >= 50_000:
            todo.append(c)
        if len(todo) >= FACTS_PER_TICK:
            break
    for c in todo:
        try:
            index_facts(c)
            did["facts"] += 1
        except Exception as e:  # noqa: BLE001
            _state["last_error"] = f"facts {c['id'][:8]}: {e}"[:200]
            _write(c, facts={}, facts_size=-1)  # don't retry a transcript that can't be read every tick
    # 2) a Haiku summary
    if not is_paused() and _today_used() < DAILY_CAP and now >= float(meta_get("backoff_until", "0") or 0):
        rows = _all_index_rows()
        for c in chats:
            ix = rows.get(c["id"])
            if not ix or not ix["facts_at"] or ix["facts_size"] < 0 or (c.get("agent") or "claude") != "claude":
                continue
            if (c.get("n_user") or 0) < 2 or now - (c.get("last_active") or 0) < SETTLE_S or (ix["error_at"] and now - ix["error_at"] < RETRY_S):
                continue
            size = c.get("size") or 0
            if ix["summarized"] and not (size >= ix["src_size"] * REGROW and size - ix["src_size"] >= REGROW_MIN_BYTES and now - ix["summarized"] >= MIN_RESUMMARY_S):
                continue
            _state["running"] = c.get("display_title") or c["id"][:8]
            ok = summarize(c)
            _state["running"] = None
            _bump_today()
            if ok:
                meta_set("fails", "0")
                did["summarized"] += 1
            else:
                fails = int(meta_get("fails", "0")) + 1
                meta_set("fails", str(fails))
                _state["last_error"] = (_row(c["id"]) or {}).get("error", "")
                if fails >= MAX_FAILS:
                    meta_set("backoff_until", str(now + 3600))
                    meta_set("fails", "0")
            break  # one per tick
    _state["ticks"] += 1
    return did


def _run() -> None:
    if _stop.wait(START_DELAY_S):
        return
    while not _stop.is_set():
        did = {}
        try:
            did = tick()
        except Exception as e:  # noqa: BLE001 — the worker must outlive any one bad session
            _state["last_error"] = str(e)[:200]
        # keep moving while there's a backlog, but gently: a pause after every Haiku call, a short one between fact batches
        _stop.wait(AFTER_SUMMARY_S if did.get("summarized") else BUSY_TICK_S if did.get("facts") else TICK_S)


def start() -> None:
    global _worker
    if not ENABLED or (_worker and _worker.is_alive()):
        return
    _stop.clear()
    _worker = threading.Thread(target=_run, daemon=True, name="session-indexer")
    _worker.start()


def stop() -> None:
    _stop.set()


def status() -> dict:
    with db.connect() as con:
        r = con.execute("SELECT COUNT(*) AS n, SUM(facts_at > 0) AS f, SUM(summarized > 0) AS s, COALESCE(SUM(cost), 0) AS cost FROM session_index").fetchone()
    return {
        "enabled": ENABLED, "paused": is_paused(), "total": int(meta_get("total", "0") or 0), "indexed": int(r["f"] or 0), "summarized": int(r["s"] or 0),
        "today": _today_used(), "cap": DAILY_CAP, "running": _state["running"], "last_error": _state["last_error"],
        "cost_usd": round(float(r["cost"] or 0), 4), "backoff": max(0, float(meta_get("backoff_until", "0") or 0) - time.time()) > 0,
    }


def index_now(chat: dict, *, haiku: bool = True) -> dict:
    """Index one session immediately (facts, then a summary) regardless of the quiet-time rule — `loom index run <chat>`."""
    index_facts(chat)
    ok = summarize(chat) if haiku else None
    return {"facts": True, "summarized": ok}
