"""`loom chats …` / `loom categories …` / `loom whoami` — loom's agent-facing harness.

An agent running inside a loom chat (or you, in any shell) can see and drive loom's chats:
list them with their category and live status, read one, get its status brief, archive /
close / rename / categorize, spawn a new chat in a fresh worktree with a handoff prompt, and
type a message into another chat — or find the chat that wrote a PR and ask it a question
(`chats pr`, `chats ask`; D27). Every loom chat is told about this in its system prompt
(core/runtime.py HARNESS_NOTE) and carries `LOOM_CHAT_ID` (who "self" is) + `LOOM_API_URL`.

A thin HTTP client over the RUNNING loom server — never the JSON state files directly: the
server owns the live agent processes, and one writer avoids racing the dashboard. Output is
plain text (no rich markup — chat titles contain brackets) sized for an agent to read;
`--json` everywhere for scripting. See docs/DECISIONS.md D20.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import webbrowser

import httpx
import typer

from loom.core.config import LOOM_API_HOST, LOOM_API_PORT
from loom.core.mentions import slugify

chats_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="See and manage loom's chats: list, read, brief, archive/close, categorize, spawn, send.",
)
categories_app = typer.Typer(no_args_is_help=True, add_completion=False, help="loom's sidebar categories.")
index_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="The background session index behind `loom find`: status, pause/resume, summarize a chat now.",
)
board_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="The shared message board of a category: post findings/handoffs, read what you missed, wait for others.",
)

WORKING_WITHIN_S = 6  # output within this many seconds = "working" (the sidebar's pulse window)
_NONE_CATEGORY = {"none", "uncategorized", "-", ""}


# --- transport ------------------------------------------------------------------------------
def _base() -> str:
    return (os.environ.get("LOOM_API_URL") or f"http://{LOOM_API_HOST}:{LOOM_API_PORT}").rstrip("/")


def _self_id() -> str | None:
    return os.environ.get("LOOM_CHAT_ID") or None


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def _api(method: str, path: str, *, body: dict | None = None, params: dict | None = None, timeout: float = 60.0):
    headers = {"X-Loom-Chat": sid} if (sid := _self_id()) else {}
    try:
        r = httpx.request(method, f"{_base()}/api{path}", json=body, params=params, headers=headers, timeout=timeout)
    except httpx.ConnectError:
        _die(f"loom isn't running at {_base()} — start it with `loom serve` (or set LOOM_API_URL)")
    except httpx.TimeoutException:
        _die(f"loom at {_base()} timed out on {method} {path}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = r.text
        _die(f"{detail or r.reason_phrase} ({r.status_code})")
    return r.json()


def _out_json(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# --- world snapshot -------------------------------------------------------------------------
class _World:
    """One fetch of everything a command needs: chats (every tab), categories, live status."""

    def __init__(self) -> None:
        self.chats: list[dict] = _api("GET", "/chats", params={"scope": "all", "tab": "all"})["chats"]
        self.cats: dict = _api("GET", "/categories")
        self.terms = {t["chat_id"]: t for t in _api("GET", "/terminals", params={"detached": "true"})["terminals"]}
        self._by_id = {c["id"]: c for c in self.chats}

    def get(self, sid: str) -> dict | None:
        return self._by_id.get(sid)

    @property
    def loom_chats(self) -> list[dict]:
        """Chats loom manages — the ones its sidebar shows (linked to a worktree task), not the
        rest of your agent history."""
        return [c for c in self.chats if c.get("task")]

    def status(self, sid: str) -> str:
        t = self.terms.get(sid)
        if not t:
            return "stopped"
        if not t.get("attached"):
            return "running"  # alive, but loom hasn't re-attached since a restart → activity unknown
        if t.get("waiting_on"):
            return "blocked"  # sitting at a startup prompt (e.g. Claude's folder trust) — needs you
        if (t.get("idle_sec") or 0) < WORKING_WITHIN_S:
            return "working"
        return "needs-you" if t.get("needs") else "idle"

    def category_order(self, cat_id: str | None) -> int:
        if cat_id is None:
            return -1  # Uncategorized first, like the sidebar
        ids = [c["id"] for c in self.cats["categories"]]
        return ids.index(cat_id) if cat_id in ids else len(ids)

    def default_project(self) -> str | None:
        """The project a bare command means: the calling chat's, else the one whose repo or
        worktree contains the cwd, else None (= every project)."""
        me = self.get(_self_id() or "")
        if me and me.get("repo"):
            return me["repo"]
        cwd = os.getcwd()
        for r in _api("GET", "/repos", params={"include_hidden": "true"})["repos"]:  # a hidden project's chats still run
            root = r["root"].rstrip("/")
            if cwd == root or cwd.startswith(root + "/"):
                return r["name"]
        for c in self.loom_chats:
            wt = (c.get("cwd") or "").rstrip("/")
            if wt and (cwd == wt or cwd.startswith(wt + "/")) and c.get("repo"):
                return c["repo"]
        return None

    def resolve(self, ref: str) -> dict:
        """A chat by: `self`/`.`, full id, unique id prefix (4+ chars), name / title, branch, or
        task id. Ambiguity is an error that lists the candidates."""
        if ref in ("self", "."):
            sid = _self_id() or _die("`self` only works inside a loom chat (LOOM_CHAT_ID is unset)")
            return self.get(sid) or {"id": sid, "display_title": "(this chat)", "task": None}
        if ref in self._by_id:
            return self._by_id[ref]
        pool = self.loom_chats
        ref = ref.lstrip("@")  # an `@mention` token works as a ref
        hits = [c for c in pool if len(ref) >= 4 and c["id"].startswith(ref)]
        if not hits:
            low = ref.lower()
            hits = [
                c for c in pool
                if low in ((c.get("name") or "").lower(), (c.get("display_title") or "").lower())
                or ref in (c.get("branch"), c.get("task"))
            ]
        if not hits and slugify(ref):  # `@some-chat-name`: the slug the dashboard's @@ picker types
            slug = slugify(ref)
            hits = [c for c in pool if slug in (slugify(c.get("name") or ""), slugify(c.get("display_title") or ""))]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            _die(f"no loom chat matches '{ref}' (try `loom chats ls --tab all`)")
        lines = "\n".join(f"  {c['id'][:8]}  {_title(c)}" for c in hits[:12])
        _die(f"'{ref}' is ambiguous — {len(hits)} chats match:\n{lines}")


# --- formatting -----------------------------------------------------------------------------
def _title(c: dict) -> str:
    return c.get("name") or c.get("display_title") or c["id"][:8]


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    d = max(0, time.time() - ts)
    for unit, sec in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= sec:
            return f"{int(d // sec)}{unit} ago"
    return "just now"


def _clip(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _table(headers: list[str], rows: list[list[str]], *, last_wide: bool = True) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)] if rows else [len(h) for h in headers]
    fmt = lambda cells: "  ".join(
        c if (last_wide and i == len(cells) - 1) else c.ljust(widths[i]) for i, c in enumerate(cells)
    )
    print(fmt(headers))
    for r in rows:
        print(fmt(r))


def _row_json(w: _World, c: dict) -> dict:
    return {
        "id": c["id"],
        "title": _title(c),
        "status": w.status(c["id"]),
        "project": c.get("repo"),
        "category": c.get("category_name"),
        "category_id": c.get("category"),
        "branch": c.get("branch"),
        "agent": c.get("agent"),
        "task": c.get("task"),
        "worktree": c.get("cwd"),
        "archived": bool(c.get("archived")),
        "starred": bool(c.get("starred")),
        "last_active": c.get("last_active"),
        "last_active_ago": _ago(c.get("last_active")),
        "prs": c.get("prs") or [],
        "first_prompt": _clip(c.get("first_prompt"), 300) or None,
        "last_prompt": _clip(c.get("preview"), 300) or None,
        "spawned_by": c.get("spawned_by"),
        "url": f"{_base()}/?chat={c['id']}",
    }


def _select(w: _World, project: str | None, all_projects: bool, tab: str) -> tuple[list[dict], str | None]:
    proj = None if all_projects else (project or w.default_project())
    rows = w.loom_chats
    if proj:
        rows = [c for c in rows if c.get("repo") == proj]
    if tab == "active":
        rows = [c for c in rows if not c.get("archived") and not c.get("hidden")]
    elif tab == "archived":
        rows = [c for c in rows if c.get("archived")]
    elif tab != "all":
        _die("--tab is one of: active, archived, all")
    return rows, proj


def _cat_matches(c: dict, name: str) -> bool:
    cat = c.get("category")
    return bool(cat) and (cat == name or (c.get("category_name") or "").lower() == name.lower())


# --- chats ----------------------------------------------------------------------------------
@chats_app.command("ls")
def chats_ls(
    category: str = typer.Option(None, "--category", "-c", help="only this category (name or id)"),
    uncategorized: bool = typer.Option(False, "--uncategorized", "-u", help="only chats in no category"),
    tab: str = typer.Option("active", "--tab", "-t", help="active | archived | all"),
    status: str = typer.Option(None, "--status", "-s", help="working | needs-you | idle | blocked | running | stopped"),
    query: str = typer.Option(None, "--query", "-q", help="substring of title / prompts / branch"),
    project: str = typer.Option(None, "--project", "-p", help="project name (default: this chat's / the cwd's)"),
    all_projects: bool = typer.Option(False, "--all-projects", "-A", help="every project"),
    ids: bool = typer.Option(False, "--ids", help="print full chat ids only (pipe into archive/close)"),
    as_json: bool = typer.Option(False, "--json", help="machine-readable"),
) -> None:
    """List loom's chats with category, live status, branch and recency."""
    w = _World()
    rows, proj = _select(w, project, all_projects, tab)
    if category:
        rows = [c for c in rows if _cat_matches(c, category)]
    if uncategorized:
        rows = [c for c in rows if not c.get("category")]
    if status:
        rows = [c for c in rows if w.status(c["id"]) == status]
    if query:
        ql = query.lower()
        rows = [
            c for c in rows
            if ql in " ".join(str(c.get(k) or "") for k in ("display_title", "name", "first_prompt", "preview", "branch")).lower()
        ]
    rows.sort(key=lambda c: (w.category_order(c.get("category")), -(c.get("last_active") or 0)))
    if ids:
        print("\n".join(c["id"] for c in rows))
        return
    if as_json:
        _out_json([_row_json(w, c) for c in rows])
        return
    me = _self_id()
    print(f"{len(rows)} {tab} chat(s) in {proj or 'all projects'}" + (f" · category {category}" if category else ""))
    if rows:
        _table(
            ["ID", "STATUS", "CATEGORY", "BRANCH", "LAST", "TITLE"],
            [
                [
                    c["id"][:8] + ("*" if c["id"] == me else ""),
                    w.status(c["id"]) + (" (archived)" if tab == "all" and c.get("archived") else ""),
                    _clip(c.get("category_name") or "-", 24),
                    _clip(c.get("branch") or "-", 36),
                    _ago(c.get("last_active")),
                    _clip(_title(c), 80),
                ]
                for c in rows
            ],
        )
        if me and any(c["id"] == me for c in rows):
            print("(* = this chat)")


def _render_item(it: dict, full: bool) -> list[str]:
    n = 100_000 if full else 1500
    k = it.get("kind")
    if k == "user":
        text = it.get("text") or ""
        return [f"you: {text if full else _clip(text, n)}"]
    if k == "error":
        return [f"! {it.get('text')}"]
    out: list[str] = []
    if (it.get("text") or "").strip():
        out.append(f"agent: {it['text'].strip() if full else _clip(it['text'], n)}")
    for t in it.get("tools") or []:
        inp = t.get("input") or {}
        arg = next((inp[k] for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt") if inp.get(k)), "")
        out.append(f"  ↳ {t.get('name')}: {_clip(str(arg), 140)}" + (" [error]" if t.get("isError") else ""))
    return out


def _settings(sid: str) -> dict:
    """{model, effort, pinned_model, pinned_effort} for a chat; {} if the server can't say."""
    try:
        r = httpx.get(f"{_base()}/api/chats/{sid}/settings", timeout=15)
        return r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}


def _model_line(s: dict) -> str:
    if not s:
        return "-"
    out = f"{s.get('model') or 'CLI default'} · effort {s.get('effort') or '-'}"
    pins = [x for x in (s.get("pinned_model"), s.get("pinned_effort") and f"effort {s['pinned_effort']}") if x]
    return out + (f"  (pinned: {', '.join(pins)})" if pins else "")


def _print_meta(w: _World, c: dict) -> None:
    sid = c["id"]
    t = w.terms.get(sid) or {}
    st = w.status(sid) + (f" (last output {t['idle_sec']:.0f}s ago)" if t.get("idle_sec") is not None else "")
    if t.get("waiting_on"):
        st += f" — waiting on its {t['waiting_on']}: open the chat to answer it"
    lines = [
        ("id", sid + ("  (this chat)" if sid == _self_id() else "")),
        ("title", _title(c)),
        ("status", st),
        ("project", c.get("repo") or "-"),
        ("category", c.get("category_name") or "Uncategorized"),
        ("branch", c.get("branch") or "-"),
        ("agent", c.get("agent") or "-"),
        ("model", _model_line(_settings(sid))),
        ("worktree", c.get("cwd") or "-"),
        ("task", c.get("task") or "-"),
        ("archived", "yes" if c.get("archived") else "no"),
        ("last", _ago(c.get("last_active"))),
        ("prs", " ".join(f"#{p}" for p in (c.get("prs") or [])) or "-"),
        ("spawned by", c.get("spawned_by") or "-"),
        ("url", f"{_base()}/?chat={sid}"),
    ]
    for k, v in lines:
        print(f"{k:<11} {v}")


@chats_app.command("show")
def chats_show(
    ref: str = typer.Argument(..., help="chat id / prefix / name / branch / self"),
    tail: int = typer.Option(12, "--tail", "-n", help="how many transcript items to print (0 = none)"),
    full: bool = typer.Option(False, "--full", help="don't truncate long messages"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """A chat's details + the tail of its conversation (you / agent / tool calls)."""
    w = _World()
    c = w.resolve(ref)
    items = _api("GET", f"/chats/{c['id']}/transcript")["items"] if tail else []
    items = items[-tail:] if tail else []
    if as_json:
        for it in items:
            it.pop("images", None)
            it.pop("thinking", None)
        s = _settings(c["id"])
        _out_json({**_row_json(w, c), "model": s.get("model"), "effort": s.get("effort"), "transcript_tail": items})
        return
    _print_meta(w, c)
    if tail:
        print(f"\n--- last {len(items)} transcript item(s) ---" if items else "\n(no transcript yet)")
        for it in items:
            for line in _render_item(it, full):
                print(line)


@chats_app.command("brief")
def chats_brief(
    ref: str = typer.Argument(..., help="chat id / prefix / name / branch / self"),
    refresh: bool = typer.Option(False, "--refresh", help="regenerate now (one Haiku call, ~1¢)"),
    cached: bool = typer.Option(False, "--cached", help="never generate; print what's cached"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """A chat's status brief: goal, status, phases, decisions, needs-you.

    loom's Haiku summary of the transcript — generated on first ask, cached after. Claude chats only."""
    w = _World()
    c = w.resolve(ref)
    sid = c["id"]
    b = _api("GET", f"/chats/{sid}/brief")
    if not b.get("supported"):
        _die(f"no brief for {sid[:8]}: briefs need a Claude transcript (agent: {c.get('agent')}, or nothing sent yet)")
    if refresh or (not b.get("brief") and not cached):
        _api("POST", f"/chats/{sid}/brief")
        deadline = time.time() + 240
        while time.time() < deadline:
            b = _api("GET", f"/chats/{sid}/brief")
            if not b.get("running"):
                break
            time.sleep(2)
    if as_json:
        _out_json({"id": sid, "title": _title(c), **b})
        return
    br = b.get("brief")
    if not br:
        _die(b.get("error") or "no brief yet (drop --cached to generate one)")
    print(f"{sid[:8]}  {_title(c)}")
    print(f"status   {br.get('status')} — {br.get('reason')}")
    print(f"goal     {br.get('goal')}")
    tm = br.get("time") or {}
    print(f"time     span {tm.get('span')} · active {tm.get('active')} · idle {tm.get('idle')}" + (f" · {tm['note']}" if tm.get("note") else ""))
    if br.get("needs_you"):
        print(f"needs    {br['needs_you']}")
    if br.get("phases"):
        print("phases")
        for ph in br["phases"]:
            print(f"  {ph.get('start')}–{ph.get('end')}{' (idle)' if ph.get('idle') else ''}  {ph.get('text')}")
    if br.get("decisions"):
        print("decisions")
        for d in br["decisions"]:
            print(f"  [{d.get('by')}] {d.get('text')}")
    if br.get("unasked"):
        print("unasked work")
        for u in br["unasked"]:
            print(f"  - {u}")
    if b.get("stale"):
        print("(stale: the transcript grew since — --refresh to regenerate)")


def _confirm_targets(w: _World, refs: list[str]) -> list[dict]:
    if not refs:
        _die("name at least one chat (ids from `loom chats ls`, or pipe `loom chats ls --ids | xargs …`)")
    seen, out = set(), []
    for r in refs:
        c = w.resolve(r)
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)
    return out


def _bulk_patch(refs: list[str], patch: dict, verb: str, dry_run: bool) -> None:
    w = _World()
    for c in _confirm_targets(w, refs):
        sid = c["id"]
        if dry_run:
            print(f"would {verb} {sid[:8]}  {_title(c)}")
            continue
        _api("PATCH", f"/chats/{sid}", body=patch)
        note = ""
        if patch.get("archived") and sid == _self_id():
            note = "  (this chat — it keeps running until you close it)"
        elif patch.get("archived") and w.status(sid) != "stopped":
            note = "  (agent stopped)"
        print(f"{verb}d {sid[:8]}  {_title(c)}{note}")


@chats_app.command("archive")
def chats_archive(
    refs: list[str] = typer.Argument(None, help="chats to archive"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="print what would change"),
) -> None:
    """Archive chats — they leave the active list and their agent + dev stack stop.

    Transcripts stay; unarchiving and reopening (or `send`) resumes them. Archiving the calling
    chat itself only archives it — it keeps running until closed."""
    _bulk_patch(refs, {"archived": True}, "archive", dry_run)


@chats_app.command("unarchive")
def chats_unarchive(
    refs: list[str] = typer.Argument(None, help="chats to bring back to the active list"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
) -> None:
    """Move archived chats back to the active list (they resume when opened or messaged)."""
    _bulk_patch(refs, {"archived": False}, "unarchive", dry_run)


@chats_app.command("star")
def chats_star(refs: list[str] = typer.Argument(None), off: bool = typer.Option(False, "--off", help="unstar")) -> None:
    """Star (or --off: unstar) chats."""
    _bulk_patch(refs, {"starred": not off}, "unstar" if off else "star", False)


@chats_app.command("close")
def chats_close(
    refs: list[str] = typer.Argument(None, help="chats whose agent to stop"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    force: bool = typer.Option(False, "--force", help="allow closing the calling chat itself"),
) -> None:
    """Stop chats' agent processes but keep them in the active list (reopening resumes)."""
    w = _World()
    for c in _confirm_targets(w, refs):
        sid = c["id"]
        if dry_run:
            print(f"would close {sid[:8]}  {_title(c)}  ({w.status(sid)})")
            continue
        r = _api("POST", f"/chats/{sid}/close", params={"force": "true"} if force else None)
        print(f"{'closed' if r.get('closed') else 'not running'}  {sid[:8]}  {_title(c)}")


@chats_app.command("rename")
def chats_rename(ref: str = typer.Argument(...), name: str = typer.Argument(..., help="new sidebar name")) -> None:
    """Rename a chat in loom's sidebar."""
    c = _World().resolve(ref)
    _api("PATCH", f"/chats/{c['id']}", body={"name": name})
    print(f"renamed {c['id'][:8]} → {name}")


def _set_muted(refs: list[str], muted: bool) -> None:
    w = _World()
    for ref in refs:
        c = w.resolve(ref)
        _api("PATCH", f"/chats/{c['id']}", body={"muted": muted})
        print(f"{'muted' if muted else 'unmuted'} {c['id'][:8]}  {_title(c)}")


@chats_app.command("mute")
def chats_mute(refs: list[str] = typer.Argument(..., help="chat id / prefix / name …")) -> None:
    """Silence a chat's desktop notifications and attention badge (it still shows `needs you` in the sidebar)."""
    _set_muted(refs, True)


@chats_app.command("unmute")
def chats_unmute(refs: list[str] = typer.Argument(..., help="chat id / prefix / name …")) -> None:
    """Turn a chat's notifications back on."""
    _set_muted(refs, False)


def _resolve_category(w: _World, name: str, project: str | None, create: bool) -> dict | None:
    if name.strip().lower() in _NONE_CATEGORY:
        return None
    cats = w.cats["categories"]
    hit = next((x for x in cats if x["id"] == name), None) or next(
        (x for x in cats if x["name"].lower() == name.lower() and x.get("repo") in (None, project)), None,
    )
    if hit:
        return hit
    if not create:
        avail = ", ".join(sorted({x["name"] for x in cats if x.get("repo") in (None, project)})) or "none yet"
        _die(f"no category '{name}' in {project or 'this project'} (have: {avail}) — add --create to make it")
    state = _api("POST", "/categories", body={"name": name, "repo": project})
    return state["categories"][0]


@chats_app.command("categorize")
def chats_categorize(
    refs: list[str] = typer.Argument(..., help="chats, then the category name last (`none` = Uncategorized)"),
    create: bool = typer.Option(False, "--create", help="make the category if it doesn't exist"),
) -> None:
    """File chats under a category: `loom chats categorize <id...> <category>`."""
    if len(refs) < 2:
        _die("usage: loom chats categorize <chat...> <category>")
    *chat_refs, cat_name = refs
    w = _World()
    targets = _confirm_targets(w, chat_refs)
    by_project: dict[str | None, list[dict]] = {}
    for c in targets:
        by_project.setdefault(c.get("repo"), []).append(c)
    for proj, group in by_project.items():
        cat = _resolve_category(w, cat_name, proj, create)
        w.cats = _api("PUT", "/categories-assign", body={"chat_ids": [c["id"] for c in group], "category_id": cat["id"] if cat else None})
        for c in group:
            print(f"{c['id'][:8]}  {_title(c)}  → {cat['name'] if cat else 'Uncategorized'}")


def _read_text(text: str | None, file: str | None, what: str) -> str | None:
    if file:
        try:
            return open(os.path.expanduser(file)).read()
        except OSError as e:
            _die(f"can't read {what} file: {e}")
    if text == "-":
        return sys.stdin.read()
    return text


@chats_app.command("spawn")
def chats_spawn(
    branch: str = typer.Option(..., "--branch", "-b", help="new git branch (and worktree) for the chat"),
    prompt: str = typer.Option(None, "--prompt", help="its first message — e.g. a handoff (`-` = stdin)"),
    prompt_file: str = typer.Option(None, "--prompt-file", "-f", help="read the first message from a file"),
    category: str = typer.Option(None, "--category", "-c", help="file it under this category (created if new)"),
    name: str = typer.Option(None, "--name", help="sidebar name"),
    project: str = typer.Option(None, "--project", "-p", help="project (default: this chat's / the cwd's)"),
    base: str = typer.Option(None, "--base", help="base branch (default: the project's)"),
    agent: str = typer.Option(None, "--agent", "-a", help="claude | grok | codex (default: this chat's, else claude)"),
    model: str = typer.Option(None, "--model", "-m", help="e.g. opus, sonnet, fable (default: this chat's, else the CLI's)"),
    effort: str = typer.Option(None, "--effort", "-e", help="low | medium | high | xhigh | max (default: this chat's, else xhigh)"),
    open_browser: bool = typer.Option(False, "--open", help="open the new chat in the browser"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Spin up a new chat: fresh worktree on --branch, agent already working on --prompt.

    It shows up in loom's sidebar immediately (recording which chat spawned it). Follow up with
    `loom chats send`. It runs the same agent, model and effort as the chat spawning it unless
    told otherwise (outside a chat: claude, the CLI's default model, xhigh); the choice is pinned,
    so the chat relaunches on the same settings. If Claude has never trusted the repo, the agent
    waits on its folder-trust prompt until you answer it in the browser — spawn says so."""
    text = _read_text(prompt, prompt_file, "prompt")
    w = _World()
    proj = project or w.default_project()
    if not proj:
        names = ", ".join(r["name"] for r in _api("GET", "/repos")["repos"]) or "none registered"
        _die(f"which project? pass --project (one of: {names})")
    body = {
        "repo": proj, "branch": branch, "base_branch": base, "prompt": text, "category": category, "name": name,
        "agent": agent or os.environ.get("LOOM_AGENT") or None, "model": model, "effort": effort,
    }
    r = _api("POST", "/spawn", body=body, timeout=180)
    url = f"{_base()}/?chat={r['chat_id']}"
    if as_json:
        _out_json({**r, "url": url})
    else:
        cat = (r.get("category") or {}).get("name")
        how = r["agent"] if "effort" not in r else f"{r['agent']} · {r.get('model') or 'default model'} · effort {r['effort']}"
        print(f"spawned {r['chat_id']}  ({how}, project {r['repo']}" + (f", category {cat}" if cat else "") + ")")
        print(f"  branch    {r['branch']}")
        print(f"  worktree  {r['worktree']}")
        print(f"  open      {url}")
        if r.get("waiting_on"):
            print(f"  ! the agent is waiting on its {r['waiting_on']} — the first message runs once someone")
            print("    answers it in the browser (`loom chats open <id>`); loom never answers it for you")
        elif r.get("error"):
            print(f"  ! {r['error']} — check the model name; switch it in the chat with /model")
        else:
            print("  first message sent" if text else "  idle — no first message (send one with `loom chats send`)")
    if open_browser:
        webbrowser.open(url)


@chats_app.command("send")
def chats_send(
    ref: str = typer.Argument(..., help="chat id / prefix / name / branch"),
    text: str = typer.Argument(None, help="the message (`-` = stdin)"),
    file: str = typer.Option(None, "--file", "-f", help="read the message from a file"),
    no_submit: bool = typer.Option(False, "--no-submit", help="type it but don't press Enter"),
    force: bool = typer.Option(False, "--force", help="type even over an unsent draft / into this chat itself"),
) -> None:
    """Type a message into another chat, as if you'd sent it there.

    Resumes the chat first if its agent isn't running; mid-turn, the agent queues it. Refuses if
    someone has an unsent draft in that chat's input box (unless --force)."""
    msg = _read_text(text, file, "message")
    if not msg or not msg.strip():
        _die("nothing to send (pass the text, `-` for stdin, or --file)")
    w = _World()
    c = w.resolve(ref)
    if c["id"] == _self_id() and not force:
        _die("that's this chat — sending to yourself just queues a message behind this turn (use --force)")
    r = _api("POST", f"/chats/{c['id']}/send", body={"text": msg, "submit": not no_submit, "force": force}, timeout=90)
    how = "started its agent and sent" if r.get("launched") else "sent"
    print(f"{how if not no_submit else 'typed (not submitted)'} → {c['id'][:8]}  {_title(c)}")


@chats_app.command("scan-prs")
def chats_scan_prs(
    ref: str = typer.Argument("self", help="chat id / prefix / name / branch (default: this chat)"),
    force: bool = typer.Option(False, "--force", help="ignore the 45 s throttle"),
) -> None:
    """Run the PR scout on a chat now: find PRs it opened that the transcript scraper missed, and link them.

    (The dashboard does this every minute while you have the chat open.) Tier 1 asks GitHub which PRs have
    the chat's branch as their head; tier 2 has Haiku read the recent conversation for PRs it mentions —
    only when there's something new to judge. Found PRs show as PR chips on the chat."""
    c = _World().resolve(ref)
    r = _api("POST", f"/chats/{c['id']}/pr-scout", params={"force": "true" if force else "false"}, timeout=180)
    if r.get("skipped"):
        print(f"{c['id'][:8]}  skipped — {r['skipped']}")
        return
    for f in r["found"]:
        print(f"{c['id'][:8]}  linked PR #{f['number']} ({f['key']}) — {f['title'] or ''}  [found by {'branch match' if f['source'] == 'branch' else 'reading the conversation'}]")
    if not r["found"]:
        print(f"{c['id'][:8]}  nothing new" + (" (Haiku read the conversation)" if r.get("haiku") else ""))


@chats_app.command("pr")
def chats_pr(
    ref: str = typer.Argument(..., help="a PR URL, owner/repo#N, or a number (in this chat's repo)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Which loom chat wrote this PR? Finds the agent behind a PR you need to understand.

    A chat is tied to a PR when its transcript shows it opening it (`created`), the user attached the
    PR to it (`pinned`), or the PR's head branch is its task branch (`branch`). Read what it knows
    first — `loom chats brief <id>` / `loom chats show <id> --tail 30` cost the other agent nothing —
    and use `loom chats ask` only for what the code, the PR and that transcript don't answer."""
    w = _World()
    data = _api("GET", "/prs/chats", params={"ref": ref, "cwd": os.getcwd()}, timeout=90)
    pr, found = data["pr"], data["chats"]
    if as_json:
        _out_json({"pr": pr, "chats": [{**c, "status": "archived" if c["archived"] else w.status(c["id"])} for c in found]})
        return
    state = pr.get("state", "unknown") + (f" · checks {pr['checks']}" if pr.get("checks") not in (None, "unknown", "none") else "")
    print(f"PR {pr['repo']}#{pr['number']}  {pr.get('title') or '(title unavailable — is `gh` signed in?)'}")
    print(f"   {state}" + (f" · branch {pr['branch']}" if pr.get("branch") else "") + (f" · {pr['url']}" if pr.get("url") else ""))
    if not found:
        print("\nno loom chat is tied to this PR — it may come from outside loom (or from a chat since deleted); "
              "the PR's commits and description are what's left.")
        return
    print()
    _table(
        ["ID", "EVIDENCE", "STATUS", "CHAT", "PROJECT", "BRANCH"],
        [[c["id"][:8], c["evidence"], "archived" if c["archived"] else w.status(c["id"]), _clip(c["name"], 40), c.get("project") or "-", c.get("branch") or "-"]
         for c in found],
    )
    top = found[0]["id"][:8]
    print(f"\nread first (free):   loom chats brief {top}   ·   loom chats show {top} --tail 30")
    print(f'only if still needed: loom chats ask {top} "<one self-contained question>"   (interrupts that agent)')


@chats_app.command("ask")
def chats_ask(
    ref: str = typer.Argument(..., help="chat id / prefix / name / branch (e.g. from `loom chats pr`)"),
    question: str = typer.Argument(None, help="the question (`-` = stdin)"),
    file: str = typer.Option(None, "--file", "-f", help="read the question from a file"),
    wait: int = typer.Option(90, "--wait", "-w", help="seconds to wait for the answer (max 100 per call)"),
    wake: bool = typer.Option(False, "--wake", help="allow restarting the agent of an ARCHIVED chat"),
) -> None:
    """Ask another chat's agent a question and print its reply.

    For when you need context only that agent has (typically the one that wrote a PR you must build on)
    and neither the code, the PR, nor `loom chats brief|show <id>` answers it. It interrupts that agent
    (mid-turn it queues the question) and may restart a stopped one — so ask sparingly, and make the
    question self-contained: that agent knows nothing of your task. The message reaches it tagged as
    coming from another agent, not the user. Blocks until the reply lands (or --wait runs out; read
    it later with `loom chats show <id> --tail 5`)."""
    msg = _read_text(question, file, "question")
    if not msg or not msg.strip():
        _die("nothing to ask (pass the question, `-` for stdin, or --file)")
    w = _World()
    c = w.resolve(ref)
    sid = c["id"]
    if sid == _self_id():
        _die("that's this chat — you can't ask yourself")
    if c.get("archived") and not wake:
        _die(f"{sid[:8]} ({_title(c)}) is archived — the user shelved it, and asking would restart its agent. "
             "Prefer `loom chats brief|show`; pass --wake only if you really need its answer.")
    nonce = uuid.uuid4().hex[:6]
    framed = (f"[ask {nonce}] {msg.strip()}\n\nAnswer concisely in your reply — the asking agent reads it directly. "
              "Don't change files or run anything unless the question requires it.")
    base = _api("GET", "/events")["latest"]
    r = _api("POST", f"/chats/{sid}/send", body={"text": framed, "submit": True, "force": False}, timeout=90)
    print(f"asked {sid[:8]}  {_title(c)}" + ("  (started its agent first)" if r.get("launched") else ""), flush=True)
    live_events = (c.get("agent") or "claude") == "claude"  # only claude's hooks feed the event ledger

    def answer() -> str | None:
        items = _api("GET", f"/chats/{sid}/transcript", timeout=60)["items"]
        at = max((i for i, it in enumerate(items) if it.get("kind") == "user" and nonce in (it.get("text") or "")), default=None)
        if at is None:
            return None  # our question hasn't been submitted yet (it's queued behind a running turn)
        texts = [(it.get("text") or "").strip() for it in items[at + 1:] if it.get("kind") not in ("user", "error")]
        texts = [t for t in texts if t]
        return "\n\n".join(texts) if texts else None

    deadline = time.time() + max(5, min(wait, 100))
    cursor, quiet_since = base, None
    while time.time() < deadline:
        finished = False
        if live_events:
            for e in _api("GET", "/events", params={"since": cursor})["events"]:
                cursor = e["id"]
                finished = finished or (e["chat_id"] == sid and e["event"] in ("Stop", "StopFailure", "SessionEnd"))
        else:  # no lifecycle events from grok/codex: "finished" = quiet for a few seconds
            t = next((x for x in _api("GET", "/terminals", params={"detached": "true"})["terminals"] if x["chat_id"] == sid), {})
            quiet_since = quiet_since or time.time() if (t.get("idle_sec") or 0) >= 8 else None
            finished = quiet_since is not None
        if finished:
            for _ in range(3):  # the transcript can trail the Stop by a moment
                ans = answer()
                if ans:
                    print(f"\n--- reply from {sid[:8]} ({_title(c)}) ---\n{_clip(ans, 6000)}")
                    return
                time.sleep(1.5)
        time.sleep(1.5)
    part = answer()
    print(f"\nno complete answer within {wait}s — {sid[:8]} is still working or hasn't reached the question. "
          f"Read it later: loom chats show {sid[:8]} --tail 5", file=sys.stderr)
    if part:
        print(f"(partial so far)\n{_clip(part, 2000)}")
    raise typer.Exit(3)


@chats_app.command("open")
def chats_open(ref: str = typer.Argument(...)) -> None:
    """Open a chat in loom's browser UI."""
    c = _World().resolve(ref)
    url = f"{_base()}/?chat={c['id']}"
    webbrowser.open(url)
    print(url)


def notify(
    text: str = typer.Argument(None, help="what you need from the user (`-` = stdin)"),
    file: str = typer.Option(None, "--file", "-f", help="read it from a file"),
) -> None:
    """Push a notification to the USER: you need their decision, approval or information to proceed.

    It pops a desktop notification (if they enabled them) and sits in the dashboard's notification panel
    until they answer — inline, or by opening this chat. Then END YOUR TURN with the question: their reply
    arrives as your next message, marked as the answer to this. Write it self-contained (they may have
    forgotten this chat's context): the decision needed, the options, and your recommendation.

    Use it only when you truly can't proceed without them. Not for progress, "done", or anything you can
    decide yourself; identical open notifications are merged and a chat may have at most 5 open."""
    msg = _read_text(text, file, "notification")
    if not msg or not msg.strip():
        _die("nothing to send (pass the text, `-` for stdin, or --file)")
    sid = _self_id() or _die("`loom notify` only works inside a loom chat (LOOM_CHAT_ID is unset) — it needs to know which chat is asking")
    r = _api("POST", f"/chats/{sid}/notify", body={"text": msg})
    print(("pushed to the user's inbox" if r.get("created") else "already pending in the user's inbox (same text)") + f" (#{r['id']}). "
          "Now end your turn with the question — their reply arrives as your next message.")


def find_pr(
    description: str = typer.Argument(..., help="what you remember about the PR — what it did, roughly when, anything"),
    days: int = typer.Option(None, "--days", "-d", help="look this far back (default: what the description implies, else 45)"),
    project: str = typer.Option(None, "--project", "-p", help="only this project"),
    refine: list[str] = typer.Option([], "--refine", "-r", help="an extra detail to narrow it down (repeatable)"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Find one of the user's sessions (chats, archived too) — and the PR it made — from a loose description.

    Searches loom's session index first (summaries, commits and files per session — built in the background; see `loom index
    status`), then the user's PRs on GitHub, ranks with a cheap Haiku pass, and quotes the session logs as evidence:
    "added a new export button on the billing page last week". Prints the likeliest matches with a confidence and why, and a
    question that would narrow it down (answer it with --refine). Takes ~10-20 s."""
    r = _api("POST", "/find-pr", body={"query": description, "history": refine, "days": days, "project": project})
    jid, deadline, last = r["job"], time.time() + 150, ""
    while time.time() < deadline:
        j = _api("GET", f"/find-pr/{jid}")
        if j["phase"] != last and not j["done"] and not as_json:
            last = j["phase"]
            print(f"  … {last}", file=sys.stderr, flush=True)
        if j["done"]:
            break
        time.sleep(1.5)
    else:
        _die("the search is still running — try again in a moment")
    if j.get("error"):
        _die(j["error"])
    res = j["result"]
    if as_json:
        _out_json(res)
        return
    s = res["searched"]
    print(f"searched {s['prs']} PRs and {s['chats']} chats ({', '.join(s['repos']) or 'no GitHub repo'}) since {s['since']}")
    for n in res.get("notes") or []:
        print(f"  note: {n}")
    if not res["suggestions"]:
        print("no match — try --days 90, or describe it differently")
        return
    for i, x in enumerate(res["suggestions"], 1):
        pr = x.get("pr")
        head = f'PR {pr.get("key") or "#" + str(pr["number"])} "{pr.get("title")}"' if pr else "(no PR linked)"
        print(f"\n{i}. [{x['confidence']}] {head}" + (f"  {pr.get('state')} {_clip(str(pr.get('url') or ''), 70)}" if pr else ""))
        for c in x.get("chats") or []:
            print(f"   session: \"{_clip(c['name'], 60)}\" ({'archived' if c['archived'] else 'active'}{', ' + c['outcome'] if c.get('outcome') else ''}) → loom chats show {c['id'][:8]}")
            if c.get("summary"):
                print(f"     did: {_clip(c['summary'], 220)}")
            for sn in (c.get("snippets") or [])[:1]:
                print(f"     log ({sn.get('role')}): {_clip(sn.get('text'), 160)}")
        print(f"   why: {x['why']}")
    if res.get("narrow_down"):
        print(f"\nto narrow it down: {res['narrow_down']}   (answer with --refine \"…\")")


@index_app.command("status")
def index_status_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """How far the background session index is, and today's Haiku usage."""
    s = _api("GET", "/index/status")
    if as_json:
        _out_json(s)
        return
    state = "paused" if s["paused"] else ("disabled (LOOM_INDEXER=0)" if not s["enabled"] else ("backing off after errors" if s["backoff"] else "running"))
    print(f"{s['indexed']}/{s['total']} sessions indexed (facts) · {s['summarized']} summarized · {state}")
    print(f"today: {s['today']}/{s['cap']} Haiku summaries · total spent ≈ ${s['cost_usd']}" + (f" · now: {s['running']}" if s.get("running") else ""))
    if s.get("last_error"):
        print(f"last error: {s['last_error']}")


@index_app.command("pause")
def index_pause_cmd() -> None:
    """Stop spending Haiku on summaries (facts are still collected — they're free)."""
    _api("POST", "/index/pause")
    print("paused — `loom index resume` to continue")


@index_app.command("resume")
def index_resume_cmd() -> None:
    _api("POST", "/index/resume")
    print("resumed")


@index_app.command("run")
def index_run_cmd(ref: str = typer.Argument(None, help="chat to index now (default: one pass of the worker)")) -> None:
    """Index a chat right now — its facts and a Haiku summary (~10 s) — without waiting for it to go quiet."""
    cid = _World().resolve(ref)["id"] if ref else None
    r = _api("POST", "/index/run", body={"chat_id": cid}, timeout=240)
    print(f"indexed {cid[:8]}: {r}" if cid else f"one pass: {r.get('did')}")


def whoami(as_json: bool = typer.Option(False, "--json")) -> None:
    """Which loom chat this shell is (project, category, branch, worktree, status)."""
    sid = _self_id()
    if not sid:
        print(f"not inside a loom chat (LOOM_CHAT_ID unset) · loom at {_base()}")
        return
    w = _World()
    c = w.get(sid) or {"id": sid, "display_title": "(no transcript yet)"}
    if as_json:
        s = _settings(sid)
        _out_json({**_row_json(w, c), "model": s.get("model"), "effort": s.get("effort")})
        return
    _print_meta(w, c)
    _print_workspace(sid)


def _print_workspace(sid: str) -> None:
    ws = _api("GET", f"/chats/{sid}/workspace").get("workspace")
    if not ws:
        print(f"{'board':<11} none — this chat isn't in a category (`loom chats categorize self <category>`)")
        return
    b = ws["board"]
    if not b.get("enabled"):
        print(f"{'board':<11} off — the user hasn't enabled a message board for this category (only they can; message chats with `loom chats send`)")
    else:
        print(f"{'board':<11} on — {b['posts']} post{'s' if b['posts'] != 1 else ''}, {b['unread']} unread — `loom board read`")
    others = ", ".join(f"{s['name']} ({s['id'][:8]})" for s in ws["siblings"][:12]) or "no one else yet"
    print(f"{'others':<11} {others}" + (f" (+{len(ws['siblings']) - 12} more)" if len(ws["siblings"]) > 12 else ""))


def projects(
    as_json: bool = typer.Option(False, "--json"),
    show_hidden: bool = typer.Option(False, "--hidden", help="include projects the user hid from the dashboard"),
) -> None:
    """The user's projects (registered repos) with their chat counts; * = this chat's / cwd's."""
    w = _World()
    here = w.default_project()
    data = []
    for r in _api("GET", "/repos", params={"include_hidden": "true" if show_hidden else "false"})["repos"]:
        mine = [c for c in w.loom_chats if c.get("repo") == r["name"]]
        active = [c for c in mine if not c.get("archived") and not c.get("hidden")]
        data.append({
            "name": r["name"],
            "root": r.get("root"),
            "hidden": bool(r.get("hidden")),
            "current": r["name"] == here,
            "active": len(active),
            "archived": sum(1 for c in mine if c.get("archived")),
            "working": sum(1 for c in active if w.status(c["id"]) == "working"),
            "needs_you": sum(1 for c in active if w.status(c["id"]) in ("needs-you", "blocked")),
        })
    if as_json:
        _out_json(data)
        return
    _table(
        ["", "PROJECT", "ACTIVE", "WORKING", "NEEDS-YOU", "ARCHIVED", "ROOT"],
        [["*" if d["current"] else "", d["name"] + (" (hidden)" if d["hidden"] else ""), str(d["active"]), str(d["working"]), str(d["needs_you"]),
          str(d["archived"]), d["root"] or "-"] for d in data],
    )


# --- categories -----------------------------------------------------------------------------
def _cat_scope(w: _World, project: str | None, all_projects: bool) -> tuple[list[dict], str | None]:
    proj = None if all_projects else (project or w.default_project())
    cats = [x for x in w.cats["categories"] if proj is None or x.get("repo") in (None, proj)]
    return cats, proj


@categories_app.command("ls")
def categories_ls(
    project: str = typer.Option(None, "--project", "-p"),
    all_projects: bool = typer.Option(False, "--all-projects", "-A"),
    archived: bool = typer.Option(False, "--archived", "-a", help="include archived categories"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Categories (in sidebar order) with their active / archived chat counts."""
    w = _World()
    cats, proj = _cat_scope(w, project, all_projects)
    cats = [x for x in cats if archived or not x.get("archived")]
    chats = [c for c in w.loom_chats if proj is None or c.get("repo") == proj]

    def counts(cat_id: str | None) -> tuple[int, int]:
        mine = [c for c in chats if c.get("category") == cat_id]
        return sum(not c.get("archived") for c in mine), sum(bool(c.get("archived")) for c in mine)

    rows = [{"id": None, "name": "Uncategorized", "repo": proj}] + cats
    data = [{**x, "active": counts(x["id"])[0], "archived": counts(x["id"])[1], "category_archived": bool(x.get("archived"))} for x in rows]
    if as_json:
        _out_json(data)
        return
    print(f"categories in {proj or 'all projects'}")
    _table(
        ["NAME", "ACTIVE", "ARCHIVED", "ID", "PROJECT"],
        [[d["name"] + (" (archived)" if d["category_archived"] else ""), str(d["active"]), str(d["archived"]), d["id"] or "-", d.get("repo") or "-"] for d in data],
    )


@categories_app.command("add")
def categories_add(name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p")) -> None:
    """Make a category (it lands at the top of the project's sidebar)."""
    w = _World()
    proj = project or w.default_project()
    state = _api("POST", "/categories", body={"name": name, "repo": proj})
    print(f"added category {state['categories'][0]['name']} ({state['categories'][0]['id']}) in {proj or 'no project'}")


def _find_cat(w: _World, ref: str, project: str | None) -> dict:
    cats, proj = _cat_scope(w, project, False)
    hit = next((x for x in cats if x["id"] == ref or x["name"].lower() == ref.lower()), None)
    return hit or _die(f"no category '{ref}' in {proj or 'any project'}")


@categories_app.command("archive")
def categories_archive(name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p")) -> None:
    """Archive a category: all its active chats are archived (agents stopped) and it leaves the sidebar.

    Reversible — its message board and the chats' assignments are kept. `loom categories restore` brings the
    whole category back; un-archiving any one of its chats does too."""
    w = _World()
    cat = _find_cat(w, name, project)
    r = _api("POST", f"/categories/{cat['id']}/archive", timeout=120)
    n = len(r.get("archived_chats") or [])
    print(f"archived category {cat['name']} — {n} chat{'s' if n != 1 else ''} archived; its board is kept (restore: loom categories restore \"{cat['name']}\")")


@categories_app.command("restore")
def categories_restore(name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p")) -> None:
    """Restore an archived category and the chats its archive archived (it doesn't start any agent)."""
    w = _World()
    cats = [x for x in w.cats["categories"] if x["id"] == name or x["name"].lower() == name.lower()]
    cat = next((x for x in cats if x.get("archived")), None) or _die(f"no archived category '{name}' (`loom categories ls --archived`)")
    r = _api("POST", f"/categories/{cat['id']}/restore", timeout=120)
    n = len(r.get("restored_chats") or [])
    print(f"restored category {cat['name']} — {n} chat{'s' if n != 1 else ''} un-archived")


@categories_app.command("rm")
def categories_rm(name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p")) -> None:
    """Delete a category FOR GOOD — its chats move back to Uncategorized and its message board (posts included)
    is deleted. Use `loom categories archive` if you might want it back."""
    w = _World()
    cat = _find_cat(w, name, project)
    r = _api("DELETE", f"/categories/{cat['id']}")
    posts = r.get("purged_posts") or 0
    print(f"deleted category {cat['name']} (its chats are Uncategorized now" + (f"; {posts} board post{'s' if posts != 1 else ''} deleted" if posts else "") + ")")


@categories_app.command("rename")
def categories_rename(
    name: str = typer.Argument(...), new_name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p"),
) -> None:
    """Rename a category."""
    w = _World()
    cat = _find_cat(w, name, project)
    _api("PATCH", f"/categories/{cat['id']}", body={"name": new_name})
    print(f"renamed category {cat['name']} → {new_name}")


# --- board ----------------------------------------------------------------------------------
def _board_key(category: str | None) -> tuple[str, str]:
    """(board key, category name): the named category's, else the calling chat's own."""
    cats = _api("GET", "/categories")
    if category:
        low = category.lower()
        hit = next((c for c in cats["categories"] if c["id"] == category or c["name"].lower() == low), None)
        return (hit["id"], hit["name"]) if hit else _die(f"no category '{category}' (`loom categories ls`)")
    sid = _self_id()
    if not sid:
        _die("not inside a loom chat — name a board with --category")
    cid = cats["assign"].get(sid)
    if not cid:
        _die("this chat isn't in a category, so it has no board — ask the user to file it in one "
             "(or `loom chats categorize self <category>` if they asked you to)")
    name = next((c["name"] for c in cats["categories"] if c["id"] == cid), cid)
    return cid, name


def _fmt_post(p: dict, me: str | None, names: dict[str, str]) -> str:
    to = p.get("to") or []
    direct = " → you" if me and me in to else (" → " + ", ".join(names.get(t, t[:8]) for t in to) if to else "")
    reply = f" (re #{p['reply_to']})" if p.get("reply_to") else ""
    body = (p.get("text") or "").rstrip().replace("\n", "\n      ")
    return f"#{p['seq']} {p.get('from_name') or p.get('from')}{direct}{reply} · {_ago(p['ts'])}\n      {body}"


def _print_posts(posts: list[dict], me: str | None) -> None:
    names: dict[str, str] = {}
    if any(p.get("to") for p in posts):
        names = {c["id"]: _title(c) for c in _World().loom_chats}
    for p in posts:
        print(_fmt_post(p, me, names))


@board_app.command("read")
def board_read(
    category: str = typer.Option(None, "--category", "-c", help="a category (default: this chat's)"),
    all_: bool = typer.Option(False, "--all", "-a", help="the recent history, not just what's new"),
    limit: int = typer.Option(30, "--limit", "-n", help="with --all: how many of the newest posts"),
    seq: int = typer.Option(None, "--seq", help="just this post, in full"),
    peek: bool = typer.Option(False, "--peek", help="don't mark what you read as read"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Read your category's board. By default: only the posts you haven't seen (and mark them seen)."""
    key, cname = _board_key(category)
    me = _self_id() or "user"
    params: dict = {"reader": me, "mark": not peek}
    if seq is not None:
        params["since"] = max(0, seq - 1)  # then keep only that post below
    elif all_:
        params["limit"] = limit
    else:
        params["unread_only"] = True
    r = _api("GET", f"/boards/{key}", params=params)
    if not r["board"].get("enabled"):
        _die(f'the message board for category "{cname}" isn\'t enabled — only the user can turn it on (dashboard → the ▤ on the category). '
             "Until then, message chats directly with `loom chats send`.")
    posts = [p for p in r["posts"] if seq is None or p["seq"] == seq]
    if as_json:
        _out_json({"category": cname, "posts": posts, "last_seq": r["last_seq"]})
        return
    if not posts:
        print(f'no new posts on "{cname}" ({r["last_seq"]} total — `loom board read --all` for the history)')
        return
    print(f'Board "{cname}" — {len(posts)} post{"s" if len(posts) != 1 else ""}:')
    _print_posts(posts, me)


@board_app.command("post")
def board_post(
    text: str = typer.Argument(None, help="the message (`-` = stdin)"),
    to: list[str] = typer.Option([], "--to", "-t", help="nudge these chats (id / name / @name) — repeatable; everyone in the category can still read it. Tagging `@chat-name` in the text does the same"),
    wake: bool = typer.Option(False, "--wake", help="also start the agent of a STOPPED tagged chat (the user closed it) so it gets the nudge now"),
    reply: int = typer.Option(None, "--reply", "-r", help="the post number you're answering"),
    category: str = typer.Option(None, "--category", "-c"),
    file: str = typer.Option(None, "--file", "-f", help="read the post from a file"),
) -> None:
    """Post to your category's board — findings, decisions, handoffs, questions.

    Tag a chat with `@chat-name` in the text (or pass --to) to notify it: if its agent is running it gets a
    one-line nudge now and the post attached to that message; otherwise it sees the post with its next message.
    Only chats in your category can be tagged."""
    msg = _read_text(text, file, "post")
    if not msg or not msg.strip():
        _die("nothing to post (pass the text, `-` for stdin, or --file)")
    key, cname = _board_key(category)
    ids = [_World().resolve(t)["id"] for t in to] if to else []
    r = _api("POST", f"/boards/{key}", body={"text": msg, "to": ids, "reply_to": reply, "wake": wake})
    print(f'posted #{r["post"]["seq"]} to "{cname}"')
    for tid, how in (r.get("nudged") or {}).items():
        print(f"  → {tid[:8]}: {how}")
    if r.get("unmatched_tags"):
        print("  note: " + ", ".join(f"@{t}" for t in r["unmatched_tags"]) + " didn't match a chat in this category — nobody was nudged for it "
              "(check `loom whoami` for the names; ignore this if it wasn't meant as a tag)")


@board_app.command("wait")
def board_wait(
    timeout: float = typer.Option(100.0, "--timeout", "-t", help="give up after this many seconds (keep under your shell's limit)"),
    count: int = typer.Option(1, "--count", "-n", help="wait until this many posts from others have arrived"),
    category: str = typer.Option(None, "--category", "-c"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Block until someone else posts (e.g. an orchestrator waiting for its workers' reports), then print it."""
    key, cname = _board_key(category)
    me = _self_id() or "user"
    deadline = time.monotonic() + max(1.0, timeout)
    got: list[dict] = []
    while len(got) < max(1, count):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        r = _api("GET", f"/boards/{key}/wait", params={"reader": me, "timeout": min(left, 60.0)}, timeout=min(left, 60.0) + 20)
        got += r["posts"]
    if as_json:
        _out_json({"category": cname, "posts": got, "timed_out": len(got) < max(1, count)})
        return
    if not got:
        print(f'no new posts on "{cname}" after {timeout:.0f}s')
        return
    note = "" if len(got) >= count else f" (timed out waiting for {count})"
    print(f'Board "{cname}" — {len(got)} new post{"s" if len(got) != 1 else ""}{note}:')
    _print_posts(got, me)


@board_app.command("ls")
def board_ls(as_json: bool = typer.Option(False, "--json")) -> None:
    """Every category's board: posts, unread for you, members."""
    me = _self_id() or "user"
    boards = _api("GET", "/boards", params={"reader": me})["boards"]
    if as_json:
        _out_json(boards)
        return
    rows = [
        [b["name"], "on" if b.get("enabled") else "off", str(b["members"]), str(b["count"]), str(b.get("unread", 0)), _ago(b.get("last_ts"))]
        for b in boards
    ]
    _table(["category", "board", "chats", "posts", "unread", "last post"], rows) if rows else print("no categories yet")


@board_app.command("enable")
def board_enable(category: str = typer.Option(None, "--category", "-c", help="the category")) -> None:
    """Turn a category's message board on (for you, in a plain shell). Chats can't — it's the user's call."""
    if _self_id():
        _die("a chat can't enable a message board — that's the user's call (the ▤ on the category in the dashboard, "
             "or `loom board enable --category NAME` in their own shell)")
    key, cname = _board_key(category)
    _api("POST", f"/boards/{key}/enable")
    print(f'enabled the message board for "{cname}" — its chats are told on their next message')
