"""`loom chats …` / `loom categories …` / `loom whoami` — loom's agent-facing harness.

An agent running inside a loom chat (or you, in any shell) can see and drive loom's chats:
list them with their category and live status, read one, get its status brief, archive /
close / rename / categorize, spawn a new chat in a fresh worktree with a handoff prompt, and
type a message into another chat. Every loom chat is told about this in its system prompt
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
import webbrowser

import httpx
import typer

from loom.core.config import LOOM_API_HOST, LOOM_API_PORT

chats_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="See and manage loom's chats: list, read, brief, archive/close, categorize, spawn, send.",
)
categories_app = typer.Typer(no_args_is_help=True, add_completion=False, help="loom's sidebar categories.")

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
        for r in _api("GET", "/repos")["repos"]:
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
        hits = [c for c in pool if len(ref) >= 4 and c["id"].startswith(ref)]
        if not hits:
            low = ref.lower()
            hits = [
                c for c in pool
                if low in ((c.get("name") or "").lower(), (c.get("display_title") or "").lower())
                or ref in (c.get("branch"), c.get("task"))
            ]
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
        _out_json({**_row_json(w, c), "transcript_tail": items})
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
    open_browser: bool = typer.Option(False, "--open", help="open the new chat in the browser"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Spin up a new chat: fresh worktree on --branch, agent already working on --prompt.

    It shows up in loom's sidebar immediately (recording which chat spawned it). Follow up with
    `loom chats send`. If Claude has never trusted the repo, the agent waits on its folder-trust
    prompt until you answer it in the browser — spawn says so."""
    text = _read_text(prompt, prompt_file, "prompt")
    w = _World()
    proj = project or w.default_project()
    if not proj:
        names = ", ".join(r["name"] for r in _api("GET", "/repos")["repos"]) or "none registered"
        _die(f"which project? pass --project (one of: {names})")
    body = {
        "repo": proj, "branch": branch, "base_branch": base, "prompt": text, "category": category, "name": name,
        "agent": agent or os.environ.get("LOOM_AGENT") or None,
    }
    r = _api("POST", "/spawn", body=body, timeout=180)
    url = f"{_base()}/?chat={r['chat_id']}"
    if as_json:
        _out_json({**r, "url": url})
    else:
        cat = (r.get("category") or {}).get("name")
        print(f"spawned {r['chat_id']}  ({r['agent']}, project {r['repo']}" + (f", category {cat}" if cat else "") + ")")
        print(f"  branch    {r['branch']}")
        print(f"  worktree  {r['worktree']}")
        print(f"  open      {url}")
        if r.get("waiting_on"):
            print(f"  ! the agent is waiting on its {r['waiting_on']} — the first message runs once someone")
            print("    answers it in the browser (`loom chats open <id>`); loom never answers it for you")
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


@chats_app.command("open")
def chats_open(ref: str = typer.Argument(...)) -> None:
    """Open a chat in loom's browser UI."""
    c = _World().resolve(ref)
    url = f"{_base()}/?chat={c['id']}"
    webbrowser.open(url)
    print(url)


def whoami(as_json: bool = typer.Option(False, "--json")) -> None:
    """Which loom chat this shell is (project, category, branch, worktree, status)."""
    sid = _self_id()
    if not sid:
        print(f"not inside a loom chat (LOOM_CHAT_ID unset) · loom at {_base()}")
        return
    w = _World()
    c = w.get(sid) or {"id": sid, "display_title": "(no transcript yet)"}
    if as_json:
        _out_json(_row_json(w, c))
        return
    _print_meta(w, c)


# --- categories -----------------------------------------------------------------------------
def _cat_scope(w: _World, project: str | None, all_projects: bool) -> tuple[list[dict], str | None]:
    proj = None if all_projects else (project or w.default_project())
    cats = [x for x in w.cats["categories"] if proj is None or x.get("repo") in (None, proj)]
    return cats, proj


@categories_app.command("ls")
def categories_ls(
    project: str = typer.Option(None, "--project", "-p"),
    all_projects: bool = typer.Option(False, "--all-projects", "-A"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Categories (in sidebar order) with their active / archived chat counts."""
    w = _World()
    cats, proj = _cat_scope(w, project, all_projects)
    chats = [c for c in w.loom_chats if proj is None or c.get("repo") == proj]

    def counts(cat_id: str | None) -> tuple[int, int]:
        mine = [c for c in chats if c.get("category") == cat_id]
        return sum(not c.get("archived") for c in mine), sum(bool(c.get("archived")) for c in mine)

    rows = [{"id": None, "name": "Uncategorized", "repo": proj}] + cats
    data = [{**x, "active": counts(x["id"])[0], "archived": counts(x["id"])[1]} for x in rows]
    if as_json:
        _out_json(data)
        return
    print(f"categories in {proj or 'all projects'}")
    _table(
        ["NAME", "ACTIVE", "ARCHIVED", "ID", "PROJECT"],
        [[d["name"], str(d["active"]), str(d["archived"]), d["id"] or "-", d.get("repo") or "-"] for d in data],
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


@categories_app.command("rm")
def categories_rm(name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p")) -> None:
    """Delete a category — its chats move back to Uncategorized."""
    w = _World()
    cat = _find_cat(w, name, project)
    _api("DELETE", f"/categories/{cat['id']}")
    print(f"deleted category {cat['name']} (its chats are Uncategorized now)")


@categories_app.command("rename")
def categories_rename(
    name: str = typer.Argument(...), new_name: str = typer.Argument(...), project: str = typer.Option(None, "--project", "-p"),
) -> None:
    """Rename a category."""
    w = _World()
    cat = _find_cat(w, name, project)
    _api("PATCH", f"/categories/{cat['id']}", body={"name": new_name})
    print(f"renamed category {cat['name']} → {new_name}")
