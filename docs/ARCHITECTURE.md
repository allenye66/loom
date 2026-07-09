# loom — Architecture

How the pieces fit, where the code lives, and how data flows. Pair with
`DECISIONS.md` (why) and `CLAUDE_AGENT_SDK_NOTES.md` (verified external facts).

## System overview

```
Browser (React dashboard)
  │  REST (TanStack Query)        WebSocket (/api/ws/term)
  ▼                               ▼
FastAPI app (loom/server)  ──────────────────────────────┐
  ├── REST API (tasks, repos, chats, transcript, doctor)  │
  └── one session per chat, two hosts:                    │
        pty (default) ── AF_UNIX ── pty_server daemon ── `claude` (inline)
        tmux (classic) ── PTY ──── tmux `loomx-<id>` ─── `claude` (fullscreen)
        │
loom/core (logic)                      ~/.loom/ (state, gitignored)
  ├── manager  → worktree, ports,        ├── registry.json     (tasks)
  │             process, tests           ├── chats.json        (chat overlay)
  ├── sessions → ~/.claude transcripts   ├── sessions_index.json(chat index cache)
  ├── terminals → pty/tmux bridge        ├── repos.json        (registered repos)
  └── registry/repos → JSON stores       ├── trash/ logs/ worktrees/ pty-sockets/
```

loom manages two things: **worktree tasks** (isolated dev/test stacks) and
**chats** (real `claude` CLI sessions, indexed from `~/.claude`).

## Backend code map (`loom/`)

| File | Responsibility |
|---|---|
| `models.py` | Pydantic models: `Task`, `TaskState`, `Ports`, `ServiceProc`. |
| `cli.py` | Typer CLI: `doctor, serve, repo-add, new, ls, rm, test, start, stop, claude`. `serve` runs uvicorn. |
| `server/app.py` | `create_app()`: FastAPI, permissive CORS (localhost tool), mounts the router under `/api`, serves `dashboard/dist` at `/`. |
| `server/api.py` | All REST endpoints + the `/api/ws/term` WebSocket route. |
| `core/config.py` | `~/.loom` paths, `LOOM_API_PORT` (8787), and the per-repo **`.loom.yaml`** loader (`RepoConfig`). |
| `core/registry.py` | Atomic JSON task registry (tempfile + `os.replace`). |
| `core/ports.py` | Deterministic `hash(branch)`→offset port allocation, collision-checked. |
| `core/worktree.py` | `git worktree` add/remove/status; `slugify`. |
| `core/process.py` | Process-group spawn (`start_new_session`), liveness, `kill_group`, port-scoped `kill_port`, `health_check`. |
| `core/manager.py` | Task lifecycle: `create_task` (worktree+alloc+setup), `start_task`/`stop_task` (Phase-2 dev servers), `remove_task`, `refresh_status`. |
| `core/tests.py` | Isolated test runs: `build_test_run` (render cmd/env from `.loom.yaml`), `serialize_lock` (file lock so concurrent runs don't clash on one shared test resource). |
| `core/doctor.py` | Preflight checks (git/uv/node/claude; optional bun/tmux/gh/docker). |
| `core/repos.py` | `repos.json` registry of known repos (name→root). |
| `core/sessions.py` | **Chat manager**: index `~/.claude/projects/**/*.jsonl` (mtime-cached), merge a local overlay, search, soft-trash, and `get_transcript()` (reconstruct a session into render items). |
| `core/runtime.py` | Per-worktree runtime context: if a session's cwd is inside a worktree, build its `LOOM_*` env (ports/log dir) + the `<loom-runtime>` system-prompt note. Project-agnostic. |
| `core/claude_session.py` | Native launcher (`open_session`/`resume_session` via tmux/Terminal.app). Powers the `loom claude` CLI and the `⧉ terminal` (native attach) button. |
| `core/terminals.py` | **The chat surface**: the *real* `claude` TUI in the browser, bridged to WebSocket subscribers as raw bytes (xterm.js renders them). Two hosts behind one interface: `PtyTerminalSession` (default, "smooth scroll" — a detached `pty_server` daemon, inline renderer, xterm owns scrollback) and `TmuxTerminalSession` ("classic" — fullscreen `claude` in `loomx-<chat_id>`). Both survive loom restarts + browser disconnects; per-chat choice in the overlay (`terminal_backend`), switchable via kill + `--resume`. Uses `core/runtime.py` for the worktree env + system-prompt note. |
| `core/pty_server.py` | The pty backend's **persistence daemon**: runs one command under a PTY on an AF_UNIX socket, detached (`start_new_session`) so it outlives loom. Escape-protocol relay (`\x1c` framing: resize / snapshot / literal), 1 MB replay ring (alt-screen-filtered on replay), DA1/DA2 interception (answers device-attribute queries itself so xterm's auto-reply can't echo as garbage), and settle-aware snapshots (waits for the TUI to go quiescent before capturing). Stdlib-only; runnable standalone as `python -m loom.core.pty_server`. |

## Frontend code map (`dashboard/src/`)

| File | Responsibility |
|---|---|
| `App.tsx` | Shell: header (Tasks/Chats nav, doctor badge), repo picker, view switch. Wrapped in `ChatProvider`. |
| `api.ts` | REST types + TanStack Query hooks (`useTasks`, `useRepos`, `useDoctor`, `useChats`, `useTaskActions`, `useChatActions`, …). |
| `components/TasksView.tsx` | New-task input + grid of `TaskCard`. |
| `components/TaskCard.tsx` | Per-worktree card: state, ports, git status, test runner+logs, `open` (→ terminal chat). |
| `components/ChatsView.tsx` | Chat manager UI: Active/Archived/Trash tabs, ★ starred, search, inline rename/tag, keyboard nav; `open` (→ terminal chat, resume). |
| `chat/ChatContext.tsx` | `ChatProvider` + `useOpenChat()` — opens a full-screen `TerminalView` overlay; restores `?chat=<id>` on load. |
| `chat/ChatSidebar.tsx` | The per-worktree chat rail (`ChatSidebar`) + the in-chat `DevStackBar` and `OpenInIde` button. Shared by the terminal overlay. |
| `term/TerminalView.tsx` | The terminal overlay: xterm.js bound to `/api/ws/term`. Branches on the server-reported backend — pty: smooth wheel scroll over xterm's own scrollback, snap-to-bottom on input, `snapshot-start/end` bracketed repaints (reset + atomic rewrite); tmux: wheel→SGR forwarding + tmux redraws. Plus renderer switcher, image drop, selectable copy-text panel, `⧉ terminal` native attach (tmux only). |

## Data flows

### Worktree task
`loom new <branch>` / `POST /api/tasks` → `manager.create_task`: allocate ports
(`ports.allocate`), `git worktree add` off the repo's base branch, run `.loom.yaml`
`setup` (symlink node_modules), state→`ready`. `test` runs the suite in the
worktree via the serialize lock. Config always read from the **registered repo
root**, never from inside the worktree (so an untracked `.loom.yaml` is fine).

### Chat manager (read-only index + overlay)
`sessions.build_index()` scans `~/.claude/projects/*/*.jsonl`, parsing only
metadata (title/branch/PRs/prompts) with an mtime cache. `list_chats()` merges
each with the `chats.json` overlay (star/archive/hide/name/tags/description),
links it to a task by `cwd`, filters/searches, sorts. Delete = move the `.jsonl`
to `~/.loom/trash/`. See `SESSIONS_DESIGN.md`.

### Terminal chat (`/api/ws/term` ↔ `core/terminals.py`)

The in-browser chat is the **actual** interactive `claude` CLI, so every slash
command / permission prompt / feature works with zero reimplementation. Each chat
runs

```
claude --effort max --permission-mode acceptEdits --settings '{...theme,hooks[,tui]}'
       [--append-system-prompt <loom-runtime note>] (--resume|--session-id <chat_id>)
```

under one of two hosts (per-chat `terminal_backend` overlay field; default `pty`):

- **pty** ("smooth scroll"): a detached `core/pty_server.py` daemon owns the PTY on
  `~/.loom/pty-sockets/loomx-<id>.sock` and outlives loom restarts. claude uses its
  default **inline** renderer (no `tui:fullscreen`, no alt-screen), so xterm.js owns a
  real scrollback — native smooth scroll and drag-select/copy, and no Ink↔tmux↔xterm
  width desync to garble text. Repaint/reconnect use the daemon's **settled snapshot**,
  which loom brackets to the browser as `snapshot-start`/`snapshot-end` (the client
  resets and applies it atomically).
- **tmux** ("classic"): fullscreen `claude` inside `loomx-<chat_id>`, loom attached via
  one PTY. tmux owns the screen (xterm only sees the alt buffer), so the browser
  forwards the wheel as SGR mouse events and repaints are `refresh-client` redraws.
  Kept as a fallback during the pty migration; also what native `tmux attach` shares.

Both hosts are the persistence layer (the loom server has no hot-reload → every
backend edit restarts it). Switching an existing chat = kill the host + relaunch with
`--resume` (`POST /terminals/{chat_id}/backend`): the conversation lives in the
transcript, so only the live process restarts. Chats that already had a live tmux
session before the pty default keep tmux until switched (never two claudes on one
session id). The chat's `cwd` and worktree ports/logs come from `core/runtime.py`;
the chat id is the stable `~/.claude` session id, so terminal output and the indexed
transcript are the same conversation.

WS protocol (`/api/ws/term`):

| dir | frame | payload |
|---|---|---|
| browser → loom | first msg (JSON) | `{chat_id, cwd?, cols, rows}` — attach + initial size |
| browser → loom | JSON | `{type:"input", data}` · `{type:"resize", cols, rows}` · `{type:"repaint"}` · `{type:"ping"}` |
| loom → browser | JSON (on open) | `{type:"backend", backend:"pty"\|"tmux"}` — client picks its wheel/repaint path |
| loom → browser | **binary** | raw terminal output (xterm writes it) |
| loom → browser | JSON | `{type:"snapshot-start"}` / `{type:"snapshot-end"}` (pty repaint bracket — binary frames between them are one atomic settled snapshot) · `{type:"exit"}` (claude quit) · `{type:"error", message}` · `{type:"pong"}` |

Status/cost/token readouts are **not** surfaced (they'd require scraping the byte
stream); a future option is a Claude Code `Notification` hook + transcript-tailing.

## REST + WS endpoints (`server/api.py`)
```
GET  /api/health, /api/doctor
GET/POST  /api/repos
GET/POST/DELETE  /api/tasks ;  POST /api/tasks/{id}/{start,stop,test} ;  GET /api/tasks/{id}/{test,logs,chat}
GET  /api/chats ;  PATCH /api/chats/{id} ;  POST /api/chats/{id}/restore ;  DELETE /api/chats/{id}
GET  /api/chats-trash ;  POST /api/chats/reindex ;  GET /api/chats/{id}/{transcript,prs}
POST /api/ide ;  POST /api/terminals/{chat_id}/{open-native,upload,backend}
WS   /api/ws/term      (terminal mode — raw PTY bytes; core/terminals.py)
```

## State files (`~/.loom/`, gitignored)
`registry.json` (tasks) · `chats.json` (chat overlay, incl. `terminal_backend`) ·
`sessions_index.json` (index cache) · `repos.json` · `trash/` (deleted transcripts) ·
`logs/` (`<task>-<service>.log`, `<task>-test.log`, `loomx-<chat>-pty.log`) ·
`worktrees/` (default base) · `pty-sockets/` (pty daemon sockets + PID sidecars).

## Ports / isolation model
Per task: backend `<base>+offset`, frontend `<base>+offset` (`ports.py`; bases come
from the repo's `.loom.yaml`). `{offset}` is also exposed as a template variable so a
repo can derive any other per-worktree index (e.g. a DB number) in its own config.
loom's own API: `8787`. Test isolation is the repo's choice (`serialize` lock by
default — see `DECISIONS.md`).

## Roadmap
✅ **Worktree tasks** — isolated worktree/ports/test runs per branch.
✅ **In-browser terminal** — the real `claude` TUI (`terminals.py`), surviving browser
disconnects + loom restarts; a per-worktree chat **sidebar** with click-to-switch
and `?chat=<id>` deep links; per-worktree dev-stack start/stop from the task card.
✅ **Smooth-scroll (pty) renderer** — tmux-free default host (`pty_server.py` daemon,
inline claude): native xterm scrollback/select/copy, per-chat switchable back to
classic tmux. Retire tmux once pty is proven.

**Next:** richer per-worktree status in the sidebar (e.g. a Claude Code `Notification`
hook + transcript-tailing to recover idle/needs-you state without scraping bytes).
