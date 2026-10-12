"""Status briefs — a model-written, high-level "what is this agent doing" per chat.

A long autonomous run is hard to follow from the terminal. A brief folds the whole
transcript into Goal / Status / Time (working vs idle) / phase timeline / Decisions
(you vs agent) / Unasked work / Needs you. Pipeline:

  transcript jsonl → timeline()  (deterministic: your messages, the agent's prose, one
                                  line per tool call, computed idle gaps; tool outputs dropped)
                   → one headless `claude -p --model haiku` call with a JSON schema
                   → ~/.loom/briefs/<chat>.json  (cache + running cost)

Runs through the user's own `claude` login (no API key), with --no-session-persistence
so the summarizer leaves no transcript for the chat index to pick up. Measured: a 9 h
session ≈ 65k input tokens ≈ 1.5¢ at list price, ~25 s.

What reaches the model mattered more than the model (prototype, 2026-10-11): the
developer's mid-turn messages live in `queued_command` attachments, AskUserQuestion
answers live in tool_result blocks, isMeta records are harness noise (except
cross-session handoffs), and idle time must be computed — the model won't notice a
3-hour timestamp gap on its own. Claude transcripts only for now (codex/grok: TODO).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from loom.core import agents
from loom.core.config import LOOM_HOME

BRIEFS_DIR = LOOM_HOME / "briefs"
MODEL = "haiku"
GAP_SEC = 15 * 60  # quiet stretches at least this long become explicit idle markers
MIN_AGE_SEC = 60  # never regenerate a brief more often than this
REFRESH_SEC = 10 * 60  # while the transcript grows, auto-refresh at most this often
ERROR_BACKOFF_SEC = 5 * 60  # after a failed run, wait before auto-retrying
MAX_CHARS = 180_000  # ≈ 85k tokens — Haiku's base price covers prompts up to 100k
TIMEOUT_SEC = 300

_inflight: set[str] = set()
_lock = threading.Lock()

PROMPT = """You write a high-level progress brief for a developer who left a coding agent running \
autonomously and has lost track of what it is doing. They want the big picture, not tool-level detail.

The user message is a compressed transcript of one agent session. Line labels:
- USER = the developer's own messages. "USER (sent while the agent was working)" = also the developer, \
typed mid-task; these are often redirections, so treat them as instructions. USER ANSWERED = the \
developer's picks on multiple-choice questions the agent asked (AGENT ASKED). USER INTERRUPTED = the \
developer stopped the agent.
- HANDOFF FROM ANOTHER AGENT SESSION = the original task brief, sent by another agent on the developer's behalf.
- CONTEXT RECAP = the agent's own summary of everything before that point, written when its memory \
filled up. Use it, but it is the agent's self-report.
- "--- NO ACTIVITY FOR XhYYm ---" = computed gaps where nothing happened. Use the line right before it \
to say why (waiting on the developer's answer, crashed on an error, a command hung, finished its turn). \
These are facts; report them.
- BACKGROUND TASK FINISHED / (skill instructions loaded) = system events, not the developer.
- AGENT = the agent's prose. "tool" lines = one line per tool call; tool outputs were removed.

Fill the JSON schema:
- goal: 1–3 sentences, the task AS IT STANDS NOW, folding in the developer's answers and later instructions.
- status + reason: reason is one short clause.
- time: span, active and idle as compact durations like "9h07m"; note = one short clause per idle gap \
of 15+ min and its cause ("" if none).
- phases: 3–12 entries covering the whole session in order, start/end as HH:MM; the last one's end is \
"now" if the agent may still be working. Each idle gap of 15+ min is its own entry with idle=true. \
text = one plain-English sentence: what that phase was and its outcome.
- decisions: up to 5 key choices or scope changes. by="you" if it traces to any USER / USER ANSWERED / \
USER (sent while...) line; by="agent" only if no developer line asked for it.
- unasked: work the developer never asked for, each with a rough duration ([] if none).
- needs_you: what the agent is waiting on the developer for, or questions the developer asked that it \
hasn't answered ("nothing" if none).

Rules: address the developer as "you" and the agent as "it"; a busy person should be able to scan it \
in 15 seconds; no tool names; name concrete things only when it helps; only state numbers that \
literally appear in the transcript or that you compute from its timestamps; do not invent facts."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["goal", "status", "reason", "time", "phases", "decisions", "unasked", "needs_you"],
    "properties": {
        "goal": {"type": "string"},
        "status": {"type": "string", "enum": ["on track", "possibly drifted", "blocked", "waiting on you", "done"]},
        "reason": {"type": "string"},
        "time": {
            "type": "object",
            "additionalProperties": False,
            "required": ["span", "active", "idle", "note"],
            "properties": {k: {"type": "string"} for k in ("span", "active", "idle", "note")},
        },
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end", "idle", "text"],
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "idle": {"type": "boolean"},
                    "text": {"type": "string"},
                },
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["by", "text"],
                "properties": {"by": {"type": "string", "enum": ["you", "agent"]}, "text": {"type": "string"}},
            },
        },
        "unasked": {"type": "array", "items": {"type": "string"}},
        "needs_you": {"type": "string"},
    },
}


# --- timeline (deterministic compression) ----------------------------------------
def _clip(s, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_line(block: dict) -> str:
    name, inp = block.get("name", "?"), block.get("input") or {}
    for k in ("description", "activeForm", "file_path", "pattern", "url", "query", "prompt", "command"):
        if inp.get(k):
            return f"{name}: {_clip(inp[k], 140)}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return "TodoWrite: " + "; ".join(f"[{t.get('status')}] {_clip(t.get('content', ''), 60)}" for t in todos)
    return f"{name}: {_clip(json.dumps(inp), 140)}"


def _user_text_line(rec: dict, text: str, stamp: str) -> str | None:
    """One line for a user-role text block, labelled by who actually wrote it."""
    handoff = text.startswith("Another Claude session sent a message")
    if rec.get("isMeta") and not rec.get("isCompactSummary") and not handoff:
        return None  # harness-injected (screenshot labels etc.), not the developer
    if text.startswith("<") and any(k in text[:40] for k in ("command-", "local-command", "system-reminder")):
        return None
    if rec.get("isCompactSummary") or text.startswith("This session is being continued"):
        return f"\n[{stamp}] CONTEXT RECAP (written by the agent when its context filled up): {_clip(text, 5000)}"
    if text.startswith("<task-notification>"):
        m = re.search(r"<summary>(.*?)</summary>", text, re.S)
        return f"[{stamp}] BACKGROUND TASK FINISHED: {_clip(m.group(1) if m else '', 200)}"
    if text.startswith("Base directory for this skill") or text.startswith("(Re-invocation of /"):
        return f"[{stamp}] (skill instructions loaded)"
    if handoff:
        return f"\n[{stamp}] HANDOFF FROM ANOTHER AGENT SESSION: {_clip(text, 2500)}"
    if text.startswith("[Request interrupted by user"):
        return f"\n[{stamp}] USER INTERRUPTED THE AGENT"
    return f"\n[{stamp}] USER: {_clip(text, 1500)}"


def _record_lines(rec: dict, stamp: str, asks: set) -> list[str]:
    t = rec.get("type")
    att = rec.get("attachment") or {}
    if t == "attachment":
        # Messages typed while the agent is mid-turn land as queued_command attachments,
        # not user records — that's where most mid-run steering lives.
        if att.get("type") == "queued_command" and (att.get("origin") or {}).get("kind") == "human":
            return [f"\n[{stamp}] USER (sent while the agent was working): {_clip(att.get('prompt', ''), 1500)}"]
        return []
    if t not in ("user", "assistant"):
        return []
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    out: list[str] = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if t == "user" and bt == "text":
            line = _user_text_line(rec, b.get("text", ""), stamp)
            if line:
                out.append(line)
        elif t == "user" and bt == "tool_result" and b.get("tool_use_id") in asks:
            res = b.get("content")
            if not isinstance(res, str):
                res = " ".join(x.get("text", "") for x in res or [] if isinstance(x, dict))
            out.append(f"\n[{stamp}] USER ANSWERED: {_clip(res, 1200)}")
        elif t == "assistant" and bt == "text" and b.get("text", "").strip():
            out.append(f"[{stamp}] AGENT: {_clip(b['text'], 600)}")
        elif t == "assistant" and bt == "tool_use":
            if b.get("name") == "AskUserQuestion":
                asks.add(b.get("id"))  # its tool_result is the developer's answer
                for q in (b.get("input") or {}).get("questions") or []:
                    opts = " | ".join(o.get("label", "") for o in q.get("options") or [])
                    out.append(f"[{stamp}] AGENT ASKED: {_clip(q.get('question', ''), 300)} [options: {_clip(opts, 300)}]")
            else:
                out.append(f"[{stamp}]   tool {_tool_line(b)}")
    return out


def timeline(path: Path) -> str:
    """Compress a Claude transcript into a scannable timeline with computed idle gaps."""
    lines: list[str] = []
    asks: set = set()
    prev: datetime | None = None
    with path.open(errors="replace") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                when = datetime.fromisoformat(rec.get("timestamp", "").replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            new = _record_lines(rec, when.strftime("%m-%d %H:%M"), asks)
            if not new:
                continue
            if prev and (when - prev).total_seconds() >= GAP_SEC:
                mins = int((when - prev).total_seconds() // 60)
                lines.append(
                    f"\n[{prev:%m-%d %H:%M}] --- NO ACTIVITY FOR {mins // 60}h{mins % 60:02d}m "
                    f"(until {when:%H:%M}); the last event before it is the line above ---\n"
                )
            prev = when
            lines.extend(new)
    return _fit(lines)


def _fit(lines: list[str]) -> str:
    """Keep the timeline under MAX_CHARS: drop tool lines oldest-first (your messages, the
    agent's prose and idle markers carry the story), then cut the middle as a last resort."""
    text = "\n".join(lines)
    if len(text) <= MAX_CHARS:
        return text
    over = len(text) - MAX_CHARS
    kept: list[str] = []
    for line in lines:
        if over > 0 and "]   tool " in line:
            over -= len(line) + 1
            continue
        kept.append(line)
    text = "\n".join(kept)
    if len(text) > MAX_CHARS:
        head, tail = MAX_CHARS // 5, MAX_CHARS - MAX_CHARS // 5
        text = text[:head] + "\n\n[… middle of the session omitted for length …]\n\n" + text[-tail:]
    return text


# --- cache --------------------------------------------------------------------------
def _cache_path(chat_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in chat_id)[:80] or "x"
    return BRIEFS_DIR / f"{safe}.json"


def _read(chat_id: str) -> dict:
    try:
        return json.loads(_cache_path(chat_id).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write(chat_id: str, data: dict) -> None:
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(chat_id)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def get(chat_id: str) -> dict:
    """The cached brief + whether the transcript has grown since (`stale`) and whether a
    refresh is running. Never calls the model."""
    path = agents.find_claude_transcript(chat_id)
    data = _read(chat_id)
    size = path.stat().st_size if path else None
    return {
        "supported": path is not None,
        "brief": data.get("brief"),
        "generated_at": data.get("generated_at"),
        "cost_usd": data.get("cost_usd"),
        "total_cost_usd": data.get("total_cost_usd"),
        "duration_ms": data.get("duration_ms"),
        "error": data.get("error"),
        "stale": bool(path and data.get("brief") and size != data.get("source_size")),
        "running": chat_id in _inflight,
    }


# --- refresh ------------------------------------------------------------------------
def _due(chat_id: str, path: Path) -> bool:
    """Auto-refresh policy: first brief; then only when the transcript grew, and either
    the agent's turn ended since the last brief (needs-you marker) or REFRESH_SEC passed."""
    data = _read(chat_id)
    now = time.time()
    if data.get("error_at") and now - data["error_at"] < ERROR_BACKOFF_SEC:
        return False
    if not data.get("brief"):
        return True
    if path.stat().st_size == data.get("source_size"):
        return False
    age = now - (data.get("generated_at") or 0)
    if age < MIN_AGE_SEC:
        return False
    marker = agents._marker_path(chat_id)
    turn_ended = marker.exists() and marker.stat().st_mtime > (data.get("generated_at") or 0)
    return turn_ended or age >= REFRESH_SEC


def refresh(chat_id: str, *, force: bool = False) -> bool:
    """Start a background regeneration if one is due (or `force`). True if one started."""
    path = agents.find_claude_transcript(chat_id)
    if path is None:
        return False
    with _lock:
        if chat_id in _inflight:
            return False
        if not force and not _due(chat_id, path):
            return False
        _inflight.add(chat_id)
    threading.Thread(target=_run, args=(chat_id, path), daemon=True, name=f"brief-{chat_id[:8]}").start()
    return True


def _run(chat_id: str, path: Path) -> None:
    try:
        size = path.stat().st_size  # what this brief covers (the transcript keeps growing)
        brief, meta = _summarize(timeline(path))
        data = _read(chat_id)
        _write(chat_id, {
            "brief": brief,
            "generated_at": time.time(),
            "source_size": size,
            "cost_usd": meta.get("cost_usd"),
            "total_cost_usd": round((data.get("total_cost_usd") or 0) + (meta.get("cost_usd") or 0), 6),
            "duration_ms": meta.get("duration_ms"),
            "model": meta.get("model"),
        })
    except Exception as e:  # noqa: BLE001 — keep the last good brief; surface the error
        data = _read(chat_id)
        data.update({"error": str(e)[:500], "error_at": time.time()})
        _write(chat_id, data)
    finally:
        with _lock:
            _inflight.discard(chat_id)


def _summarize(text: str) -> tuple[dict, dict]:
    """One headless Haiku call → (brief dict, {cost_usd, duration_ms, model})."""
    argv = [
        agents.binary("claude"), "-p",
        "--model", MODEL,
        "--no-session-persistence",  # leave no transcript for loom's chat index to adopt
        "--tools", "",
        "--setting-sources", "",  # no user/project hooks, CLAUDE.md, plugins
        "--strict-mcp-config",
        "--system-prompt", PROMPT,
        "--json-schema", json.dumps(SCHEMA),
        "--output-format", "json",
    ]
    # Same scrub as terminal sessions: an inherited Claude Code context would leak in.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        argv, input=text, capture_output=True, text=True, env=env, cwd=BRIEFS_DIR, timeout=TIMEOUT_SEC,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError((proc.stderr or proc.stdout or f"claude exited {proc.returncode}").strip()[:500]) from None
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result") or out.get("subtype") or "summarizer failed")[:500])
    brief = out.get("structured_output")
    if not isinstance(brief, dict):
        brief = json.loads(out.get("result") or "null")
    if not isinstance(brief, dict):
        raise RuntimeError("summarizer returned no brief")
    model = next(iter(out.get("modelUsage") or {}), None)
    return brief, {"cost_usd": out.get("total_cost_usd"), "duration_ms": out.get("duration_ms"), "model": model}
