"""REST API the dashboard (and `loom` CLI) drive."""

from __future__ import annotations

import base64
import contextlib
import os
import shlex
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from loom import __version__
from loom.core import claude_session, doctor, github, manager, registry, repos, sessions, terminals
from loom.core.config import LOGS_DIR, LOOM_HOME, load_repo_config
from loom.core.tests import build_test_run, serialize_lock

router = APIRouter()

# Transient per-task test-run state (log file is the durable record).
_test_runs: dict[str, dict] = {}


# --- meta ---------------------------------------------------------------------
@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/doctor")
def get_doctor() -> dict:
    return {"checks": doctor.run_checks()}


# --- repos --------------------------------------------------------------------
class RepoIn(BaseModel):
    root: str


@router.get("/repos")
def list_repos() -> dict:
    return {"repos": repos.list_repos()}


@router.post("/repos")
def add_repo(body: RepoIn) -> dict:
    try:
        return repos.register(body.root)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


# --- tasks --------------------------------------------------------------------
def _view(task) -> dict:
    git: dict = {}
    with contextlib.suppress(Exception):
        git = manager.refresh_status(task)  # in-memory health refresh
    d = task.model_dump()
    d["git"] = git
    d["test"] = _test_runs.get(task.id)
    # The task chat's locked surface ("chat"/"terminal"/None) — drives the open picker.
    d["chat_mode"] = sessions.get_overlay(task.chat_id).get("mode") if task.chat_id else None
    return d


@router.get("/tasks")
def list_tasks(repo: str | None = None) -> dict:
    tasks = registry.list_tasks()
    if repo:
        tasks = [t for t in tasks if t.repo == repo]
    # _view → refresh_status shells out git_status (+ health probes) per task; with many worktrees
    # that's O(N) sequentially, so fan it out (cached results make most calls instant anyway).
    if tasks:
        with ThreadPoolExecutor(max_workers=min(12, len(tasks))) as ex:
            views = list(ex.map(_view, tasks))
    else:
        views = []
    # Annotate each task with whether its worktree's most-recent chat is archived,
    # so the UI can split tasks into active vs archived.
    try:
        idx = sessions.build_index()
        ov = sessions.overlay_all()
        latest: dict[str, tuple[float, bool]] = {}
        for s in idx:
            cwd = s.get("cwd")
            if not cwd:
                continue
            la = s.get("last_active") or 0
            if cwd not in latest or la > latest[cwd][0]:
                latest[cwd] = (la, bool(ov.get(s["id"], {}).get("archived")))
        for v in views:
            cid = v.get("chat_id")
            if cid:  # strict 1:1 link
                v["chat_archived"] = bool(ov.get(cid, {}).get("archived"))
            else:  # legacy task with no linked chat yet → fall back to the cwd's latest
                wt = v.get("worktree_path")
                v["chat_archived"] = bool(latest.get(wt, (0, False))[1]) if wt else False
    except Exception:  # noqa: BLE001 — never let chat-state lookup break the tasks list
        for v in views:
            v["chat_archived"] = False
    return {"tasks": views}


class TaskIn(BaseModel):
    repo_root: str
    branch: str
    base_branch: str | None = None
    note: str | None = None


@router.post("/tasks")
def create_task(body: TaskIn) -> dict:
    try:
        cfg = load_repo_config(body.repo_root)
        return _view(manager.create_task(cfg, body.branch, body.base_branch, body.note))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, force: bool = False) -> dict:
    manager.remove_task(task_id, force=force)
    _test_runs.pop(task_id, None)
    return {"ok": True}


def _cfg_for(task_id: str):
    task = registry.get_task(task_id)
    if not task:
        raise HTTPException(404, f"unknown task '{task_id}'")
    return task, load_repo_config(task.repo_root)


@router.post("/tasks/{task_id}/start")
def start_task(task_id: str, only: str | None = None) -> dict:
    """Start the task's dev services. `only` = comma-separated service names to (re)start just
    those (e.g. `?only=frontend` to bring a down port back up without touching the other)."""
    _, cfg = _cfg_for(task_id)
    svcs = {s for s in only.split(",") if s} if only else None
    return _view(manager.start_task(cfg, task_id, only=svcs))


@router.post("/tasks/{task_id}/stop")
def stop_task(task_id: str) -> dict:
    manager.stop_task(task_id)
    return _view(registry.get_task(task_id))


class TestIn(BaseModel):
    pytest_args: str = ""


@router.post("/tasks/{task_id}/test")
def run_tests(task_id: str, body: TestIn) -> dict:
    task, cfg = _cfg_for(task_id)
    command, cwd, env = build_test_run(task, cfg, body.pytest_args)
    log_path = str(LOGS_DIR / f"{task_id}-test.log")
    rec = {"running": True, "pid": None, "exit_code": None, "log_path": log_path, "command": command}
    _test_runs[task_id] = rec
    serialize = cfg.test.isolation == "serialize"

    def go() -> None:
        ctx = serialize_lock() if serialize else contextlib.nullcontext()
        with ctx, open(log_path, "wb") as logf:
            p = subprocess.Popen(
                command, shell=True, cwd=cwd, env={**os.environ, **env},
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
            )
            rec["pid"] = p.pid
            rec["exit_code"] = p.wait()
            rec["running"] = False

    threading.Thread(target=go, daemon=True).start()
    return {"started": True, "log_path": log_path}


@router.get("/tasks/{task_id}/test")
def test_status(task_id: str) -> dict:
    return _test_runs.get(task_id) or {"running": False, "exit_code": None}


@router.get("/tasks/{task_id}/logs")
def get_logs(task_id: str, kind: str = "test", lines: int = 300) -> dict:
    path = Path(LOGS_DIR) / f"{task_id}-{kind}.log"
    if not path.exists():
        return {"log": ""}
    tail = path.read_text(errors="replace").splitlines()[-lines:]
    return {"log": "\n".join(tail)}


def _task_chat_id(task) -> str | None:
    """The task's single linked chat id (strict 1:1). New tasks get one at creation; for
    legacy tasks adopt the worktree's most-recent chat once (else mint a fresh id) and
    persist — so every task maps to exactly one chat from then on."""
    if not task or not task.worktree_path:
        return None
    if task.chat_id:
        return task.chat_id
    best = None
    for s in sessions.build_index():
        if s.get("cwd") == task.worktree_path and (best is None or (s.get("last_active") or 0) > best[1]):
            best = (s["id"], s.get("last_active") or 0)
    task.chat_id = best[0] if best else str(uuid.uuid4())
    registry.upsert(task)
    return task.chat_id


@router.get("/tasks/{task_id}/chat")
def task_chat(task_id: str) -> dict:
    """This task's single chat id (1:1) + its locked mode — powers 'open' from a task card.
    `mode` is null until the user first picks chat vs terminal (then it's fixed)."""
    cid = _task_chat_id(registry.get_task(task_id))
    mode = sessions.get_overlay(cid).get("mode") if cid else None
    return {"chat_id": cid, "mode": mode}


# --- chats / sessions ---------------------------------------------------------
class ChatPatch(BaseModel):
    starred: bool | None = None
    archived: bool | None = None
    hidden: bool | None = None
    name: str | None = None
    tags: list[str] | None = None
    description: str | None = None
    pr: int | None = None
    mode: str | None = None  # "chat" (SDK UI) | "terminal" (xterm); fixed once chosen


@router.get("/chats")
def list_chats(
    repo: str | None = None,
    scope: str = "repo",
    tab: str = "active",
    q: str | None = None,
    branch: str | None = None,
    starred: bool | None = None,
) -> dict:
    return {"chats": sessions.list_chats(repo=repo, scope=scope, tab=tab, q=q, branch=branch, starred=starred)}


@router.patch("/chats/{sid}")
def patch_chat(sid: str, body: ChatPatch) -> dict:
    return sessions.set_overlay(sid, body.model_dump(exclude_none=True))


@router.delete("/chats/{sid}")
def delete_chat(sid: str) -> dict:
    sessions.trash_chat(sid)
    return {"ok": True}


@router.post("/chats/{sid}/restore")
def restore_chat(sid: str) -> dict:
    sessions.restore_chat(sid)
    return {"ok": True}


@router.get("/chats-trash")
def chats_trash() -> dict:
    return {"trash": sessions.list_trash()}


@router.post("/chats/reindex")
def reindex_chats() -> dict:
    return {"count": len(sessions.build_index(force=True))}


@router.get("/chats/{sid}/transcript")
def chat_transcript(sid: str) -> dict:
    return {"items": sessions.get_transcript(sid)}


@router.get("/chats/{sid}")
def get_one_chat(sid: str) -> dict:
    """The chat's index entry + overlay (name/starred/archived), or null if it has no
    transcript yet. Powers the in-overlay rename/star/archive actions. `mode` is the
    locked surface (chat/terminal), surfaced top-level too so a ?chat= deep link can
    restore the right one even before any transcript exists."""
    mode = sessions.get_overlay(sid).get("mode")
    chat = sessions.get_chat(sid)
    if chat is not None:
        chat = {**chat, "mode": mode}
    return {"chat": chat, "mode": mode}


@router.get("/chats/{sid}/prs")
def chat_prs(sid: str) -> dict:
    """The chat's branch + its PRs enriched with live GitHub status (via `gh`, cached)."""
    chat = sessions.get_chat(sid) or {}
    repo = chat.get("pr_repo")
    nums: list = list(chat.get("prs") or [])
    if chat.get("pr_manual") and chat["pr_manual"] not in nums:
        nums.append(chat["pr_manual"])
    out, seen = [], set()
    for n in nums:
        if str(n) in seen:
            continue
        seen.add(str(n))
        out.append(github.pr_status(repo, n))
    return {"branch": chat.get("branch"), "prs": out}


class OpenIdeIn(BaseModel):
    cwd: str


def _editor_command(cwd: str) -> str:
    """The editor-open command template for this cwd. Resolution order: `$LOOM_EDITOR`
    (per-machine override) → the cwd's repo `.loom.yaml` `editor:` → the cursor default.
    A full command template; `{worktree}` is substituted before running (e.g.
    "cursor --new-window {worktree}", "code --new-window {worktree}", "subl {worktree}")."""
    env = os.environ.get("LOOM_EDITOR")
    if env:
        return env
    target = Path(cwd).expanduser().resolve()
    for t in registry.list_tasks():
        if not t.worktree_path:
            continue
        wt = Path(t.worktree_path).expanduser().resolve()
        if target == wt or wt in target.parents:
            with contextlib.suppress(Exception):
                return load_repo_config(t.repo_root).editor
            break
    return "cursor --new-window {worktree}"


# macOS app-bundle fallbacks for editors whose CLI often isn't on PATH (you have the .app but
# never ran "install '<cli>' command in PATH"). cli -> .app name; the bin lives at
# <app>/Contents/Resources/app/bin/<cli> (the VS Code-derived family: Cursor, VS Code, …).
_EDITOR_APPS = {"cursor": "Cursor", "code": "Visual Studio Code", "codium": "VSCodium", "windsurf": "Windsurf"}


def _resolve_cli(cli: str) -> str | None:
    """Absolute path to an editor CLI: prefer PATH, else look inside the known .app bundle."""
    found = shutil.which(cli)
    if found:
        return found
    app = _EDITOR_APPS.get(cli)
    if app:
        for base in ("/Applications", str(Path.home() / "Applications")):
            p = Path(base) / f"{app}.app/Contents/Resources/app/bin/{cli}"
            if p.exists():
                return str(p)
    return None


@router.post("/ide")
def open_ide(body: OpenIdeIn) -> dict:
    """Open a worktree folder in the configured editor (the chat header's edit button).
    The command comes from `$LOOM_EDITOR` or the repo's `.loom.yaml` `editor:` field, so
    Cursor / VS Code / Sublime / etc. all work without hardcoding any one of them."""
    p = Path(body.cwd).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"no such directory: {body.cwd}")
    template = _editor_command(body.cwd)
    parts = shlex.split(template)
    if not parts:
        raise HTTPException(400, "empty editor command — set `editor:` in .loom.yaml or $LOOM_EDITOR")
    # Resolve the CLI (PATH or .app bundle) so it works even when the shell command isn't installed.
    cli = _resolve_cli(parts[0])
    if not cli:
        raise HTTPException(
            400,
            f"editor '{parts[0]}' not found — install its CLI on PATH (e.g. Cursor → "
            "“Shell Command: Install 'cursor' command in PATH”), or set `editor:` / $LOOM_EDITOR",
        )
    # Substitute the worktree path into the args (not the CLI); split-then-substitute keeps a path
    # with spaces as one argv entry. A bare command (no {worktree}) gets the path appended.
    argv = [cli] + [os.path.expandvars(a.replace("{worktree}", str(p))) for a in parts[1:]]
    if "{worktree}" not in template:
        argv.append(str(p))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not run editor command ({template!r}): {e}") from e
    if r.returncode != 0:
        raise HTTPException(400, (r.stderr or r.stdout or f"editor command failed: {template!r}").strip())
    return {"opened": True}


class ShellIn(BaseModel):
    cwd: str


@router.post("/shell")
def open_shell(body: ShellIn) -> dict:
    """Open a plain native shell (Terminal.app) at a directory — e.g. a worktree — WITHOUT
    claude. The launcher drops the one-shot skip-autoclaude marker so a guarded ~/.zshrc won't
    auto-start claude in the new window. For quick git/file pokes alongside the running agent."""
    p = Path(body.cwd).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"no such directory: {body.cwd}")
    opened = claude_session._launch(f"cd {shlex.quote(str(p))}", label=p.name, prefer="terminal")
    return {"opened": opened}


@router.get("/terminals")
def list_terminals() -> dict:
    """Live terminal sessions + how long each has been idle — drives the sidebar's
    working-pulse / 'needs you' indicators."""
    return {"terminals": terminals.list_active()}


@router.post("/terminals/{chat_id}/open-native")
def open_native_terminal(chat_id: str) -> dict:
    """Open the SAME live tmux session in a native Terminal.app (`tmux attach`). The
    in-browser xterm and the real terminal then share one live session — terminal mode's
    counterpart to chat mode's `claude --resume` handoff (here no handoff is needed; tmux
    supports multiple clients on one session)."""
    session = terminals.session_name(chat_id)
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode != 0:
        raise HTTPException(404, "no live terminal session yet — open this terminal chat first")
    opened = claude_session._launch(f"tmux attach -t {shlex.quote(session)}", label=chat_id, prefer="terminal")
    return {"opened": opened}


class UploadIn(BaseModel):
    data: str  # base64-encoded file bytes
    name: str | None = None


@router.post("/terminals/{chat_id}/upload")
async def terminal_upload(chat_id: str, body: UploadIn) -> dict:
    """Drop a file (image, PDF, CSV, …) into a terminal chat: save it server-side and type its
    path into claude's input — the same mechanism as dragging a file into a native terminal
    (claude reads it from the path). Runs in the event loop (async) so the PTY write is on the
    loop thread."""
    ts = terminals.get(chat_id)
    if ts is None:
        raise HTTPException(404, "no live terminal session for this chat")
    # Preserve the original extension (sanitized) so claude reads it as the right type; drop it
    # if the name has no clean short alphanumeric extension.
    ext = os.path.splitext(body.name or "")[1].lower()
    if not (1 < len(ext) <= 8 and ext.startswith(".") and ext[1:].isalnum()):
        ext = ""
    updir = LOOM_HOME / "uploads"
    updir.mkdir(parents=True, exist_ok=True)
    path = updir / f"{uuid.uuid4().hex}{ext}"
    try:
        path.write_bytes(base64.b64decode(body.data))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"bad file data: {e}") from e
    ts.write((str(path) + " ").encode())  # insert the path at claude's cursor, like a native drag
    return {"path": str(path)}


@router.websocket("/ws/term")
async def term_ws(websocket: WebSocket) -> None:
    """Terminal mode: bridge a tmux-hosted `claude` PTY to xterm.js as raw bytes.

    Browser → server: JSON `{type:"input"|"resize"|"ping", ...}`.
    Server → browser: binary frames = terminal output; JSON frames = control (exit/pong).
    """
    await websocket.accept()
    try:
        start = await websocket.receive_json()
    except Exception:  # noqa: BLE001
        return
    chat_id = start.get("chat_id") or start.get("resume")
    if not chat_id:
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    cwd = start.get("cwd")
    cols = int(start.get("cols") or 120)
    rows = int(start.get("rows") or 32)
    # Lock this chat to terminal mode (persisted; the UI won't offer a switch afterward).
    with contextlib.suppress(Exception):
        sessions.set_overlay(chat_id, {"mode": "terminal"})
    try:
        ts = await terminals.open_terminal(chat_id, cwd, cols, rows)
    except Exception as e:  # noqa: BLE001 — surface a tmux/claude launch failure to the client
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(e)})
            await websocket.close()
        return
    await ts.subscribe(websocket)
    try:
        while True:
            m = await websocket.receive_json()
            t = m.get("type")
            if t == "input":
                ts.write((m.get("data") or "").encode())
            elif t == "resize":
                ts.resize(int(m.get("cols") or cols), int(m.get("rows") or rows))
            elif t == "repaint":
                await ts.repaint()  # force a clean tmux redraw (clears scroll/resize tearing)
            elif t == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        ts.unsubscribe(websocket)
