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
  └── one session per chat, on the pty host:              │
        pty ── AF_UNIX ── pty_server daemon ── agent CLI (inline renderer)
        (legacy: a still-live classic tmux `loomx-<id>` session keeps tmux)
        │
loom/core (logic)                      ~/.loom/ (state, gitignored)
  ├── manager  → worktree, ports,        ├── registry.json     (tasks)
  │             process, tests           ├── chats.json        (chat overlay)
  ├── sessions → agent transcripts      ├── sessions_index.json(chat index cache)
  ├── terminals → pty/tmux bridge        ├── repos.json        (registered repos)
  └── registry/repos → JSON stores       ├── trash/ logs/ worktrees/ pty-sockets/
```

loom manages two things: **worktree tasks** (isolated dev/test stacks) and
**chats** (real `claude` / `grok` / `codex` CLI sessions, indexed from `~/.claude`,
`~/.grok`, and `~/.codex`).

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
| `core/doctor.py` | Preflight checks (git/uv/node + agent CLIs claude/grok/codex, one required; optional bun/tmux/gh/docker). |
| `core/repos.py` | `repos.json` registry of known repos (name→root). |
| `core/agents.py` | **Agent adapters** (claude \| grok \| codex): argv/env construction per CLI, transcript discovery, needs-you hooks. Codex extras: it mints its own session id (no `--session-id`), so loom binds the discovered native id into the overlay (`agent_session_id`) via the `notify` hook file / a rollout cwd-scan, and resumes with `codex resume <that id>`. |
| `core/sessions.py` | **Chat manager**: index `~/.claude/projects/**/*.jsonl` + `~/.grok/sessions/**` + `~/.codex/sessions/**` rollouts (mtime-cached), merge a local overlay, search, soft-trash, and `get_transcript()` (reconstruct a session into render items; claude + codex formats). Codex rows claimed by an `agent_session_id` binding are re-keyed to their loom chat id. Chat→task resolution: the `task.chat_id` link wins (homes adopted chats in their task's worktree), then worktree/repo cwd-prefix inference. |
| `core/runtime.py` | Per-worktree runtime context: if a session's cwd is inside a worktree, build its `LOOM_*` env (ports/log dir) + the `<loom-runtime>` system-prompt note. Project-agnostic. |
| `core/claude_session.py` | Native launcher (`open_session`/`resume_session` via tmux/Terminal.app). Powers the `loom claude` CLI and the `⧉ terminal` (plain shell in the worktree) button. |
| `core/terminals.py` | **The chat surface**: the *real* agent TUI (claude/grok/codex via `core/agents.py`) in the browser, bridged to WebSocket subscribers as raw bytes (xterm.js renders them). Host: `PtyTerminalSession` ("smooth scroll" — a detached `pty_server` daemon, inline renderer, xterm owns scrollback) is the only one loom starts; `TmuxTerminalSession` ("classic", legacy — fullscreen `claude` in `loomx-<chat_id>`) only drives a still-live old tmux session until it exits. Both survive loom restarts + browser disconnects. Uses `core/runtime.py` for the worktree env + system-prompt note. |
| `core/pty_server.py` | The pty backend's **persistence daemon**: runs one command under a PTY on an AF_UNIX socket, detached (`start_new_session`) so it outlives loom. Escape-protocol relay (`\x1c` framing: resize / snapshot / literal), 1 MB replay ring (alt-screen-filtered on replay), DA1/DA2 interception (answers device-attribute queries itself so xterm's auto-reply can't echo as garbage), and settle-aware snapshots (waits for the TUI to go quiescent before capturing). Stdlib-only; runnable standalone as `python -m loom.core.pty_server`. |
| `core/monitor.py` | Dev-stack **supervisor + reaper** (background loop): keeps an open chat's task services up (restarts any that probe down, unless explicitly stopped) and reaps stacks whose chat is done. |
| `core/logs.py` | Per-task service/test logs under `~/.loom/logs/`: end-of-file tail + byte-offset incremental follow (never loads whole files). |
| `core/github.py` | PR state via the `gh` CLI with a short TTL cache (`GET /api/chats/{id}/prs`); never raises (`unknown` when gh is missing/unauthed). |
| `core/perf.py` | Opt-in (`LOOM_PERF=1`) perf tracing to `~/.loom/perf.log`: slow requests + event-loop lag, for root-causing terminal lag. |

## Frontend code map (`dashboard/src/`)

| File | Responsibility |
|---|---|
| `main.tsx` | Entry: React root + the TanStack Query client (5 s default poll; the terminal has its own WS). |
| `App.tsx` | The whole app: the always-present `ChatSidebar` beside the content pane — the open chat's `TerminalView`, or an empty state (first-run repo add, open-branch-in-editor, notes, doctor badge). No separate home/Tasks/Chats pages. Wrapped in `ChatProvider`. |
| `api.ts` | REST types + TanStack Query hooks (`useTasks`, `useRepos`, `useDoctor`, `useChats`, `useTrash`, `useUsage`, `useTaskActions`, `useChatActions`). |
| `chat/ChatContext.tsx` · `chat/openChat.ts` | `ChatProvider`: the active chat (`open`/`close`), `?chat=<id>` sync (restored on load via `GET /api/chats/{id}`), and the global `NotesModal`. `openChat.ts` holds the contexts + `useOpenChat` / `useChatShell`. |
| `chat/ChatSidebar.tsx` | The chat rail: loom's task chats (task link or a task-worktree cwd — not your whole history), active/archived tabs, search, drag-to-reorder, categories (click a header to collapse; ✎ rename, ▲▼ reorder, ✕ delete; drag a chat onto a group to file it), per-row needs-you / working status (`GET /api/terminals`), archive, `+ new` task + chat. Also exports the in-chat `DevStackBar` (dev-stack start/stop) and `OpenInIde`. |
| `categories/categoriesStore.ts` | Sidebar categories + chat→category assignments in localStorage (`loom.chatCategories`); collapsed groups in `loom.sidebarCollapsedCats`. |
| `notes/*` | Per-chat notes in localStorage (`notesStore`): `NotesPanel` (drawer beside the terminal), `NotesModal` (all notes; reopen a chat from one), `NotesButton`. |
| `term/TerminalView.tsx` | The terminal pane (beside the sidebar): xterm.js bound to `/api/ws/term`. Branches on the server-reported backend — pty: smooth wheel scroll over xterm's own scrollback, snap-to-bottom on real input only (xterm's `scrollOnUserInput` — not on focus reports), `snapshot-start/end` bracketed repaints (reset + atomic rewrite, requested only after a real resize); tmux: wheel→SGR forwarding + tmux redraws. Plus image drop, transcript search, selectable copy-text panel, notes drawer, PR badges, usage chip, `⧉ terminal` (opens a plain shell in the worktree), open-in-editor, `↑ my msg ↓` / ⌘↑⌘↓ message jumps; hosts the chat's `DevStackBar` + `ServiceLogsPanel`. |
| `term/messageNav.ts` | Jump between the prompts you sent (pty + claude only): finds claude's inline-rendered prompts in xterm's scrollback (`❯` in column 0 on a grey row; skips the input box and still-queued prompts), steps older/newer from the last jump or the current view, highlights the landing row. |
| `term/readingPosition.ts` | Keeps a scrolled-up reader's place across a resize reflow or a snapshot rebuild (anchored on the text of the view's top logical lines; distance-from-bottom fallback), plus `scrollToLineExact` for xterm 6's post-reflow scroll lag. |
| `components/ServiceLogsPanel.tsx` | Live FE/BE/test log drawer in the terminal chat (SSE follow, filter, resize, clear). |
| `components/PrBadges.tsx` · `components/AgentIcon.tsx` | PR state badges (`GET /api/chats/{id}/prs`) · claude/grok/codex icons. |
| `usage/UsageChip.tsx` | Claude subscription usage (5 h / 7 d) in the chat header, from `GET /api/usage`. |
| `components/TasksView.tsx` · `TaskCard.tsx` · `ChatsView.tsx` | **Not mounted** — the old Tasks/Chats pages from before the sidebar-first shell; nothing imports them (`TaskCard` only via `TasksView`). |

## Data flows

### Worktree task
`loom new <branch>` / `POST /api/tasks` → `manager.create_task`: allocate ports
(`ports.allocate`), `git worktree add` off the repo's base branch, run `.loom.yaml`
`setup` (symlink node_modules), state→`ready`. `test` runs the suite in the
worktree via the serialize lock. Config always read from the **registered repo
root**, never from inside the worktree (so an untracked `.loom.yaml` is fine).

### Chat manager (read-only index + overlay)
`sessions.build_index()` scans `~/.claude/projects/*/*.jsonl`, `~/.grok/sessions/**`
and `~/.codex/sessions/**`, parsing only metadata (title/branch/PRs/prompts) with an
mtime cache. `list_chats()` merges each with the `chats.json` overlay
(star/archive/hide/name/tags/description/agent), links it to a task (the task's chat
link, else `cwd`), filters/searches, sorts. Delete = soft-trash: an overlay flag that
unlists the chat — the transcript is never moved or modified (files an older version
moved into `~/.loom/trash/` are still restorable). See `SESSIONS_DESIGN.md`.

### Terminal chat (`/api/ws/term` ↔ `core/terminals.py`)

The in-browser chat is the **actual** interactive agent CLI (claude | grok | codex,
locked per chat), so every slash command / permission prompt / feature works with
zero reimplementation. A claude chat runs

```
claude --effort max --permission-mode bypassPermissions --settings '{...theme,hooks[,tui]}'
       [--append-system-prompt <loom-runtime note>] (--resume|--session-id <chat_id>)
```

(grok: `--effort max --permission-mode acceptEdits [--no-alt-screen --minimal]`;
codex: `[resume <bound id>] -c model_reasoning_effort="xhigh"
--dangerously-bypass-approvals-and-sandbox --search -c notify=[<needs-hook>]
[--no-alt-screen]` — argv per agent built in `core/agents.py`)

under the pty host — the only one loom starts:

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
  Legacy: loom no longer starts these; it only keeps driving one that is still alive.

The host is the persistence layer (the loom server has no hot-reload → every
backend edit restarts it). A chat whose classic tmux session is still alive keeps
tmux until that session exits (never two claudes on one session id); a
`terminal_backend` left in the overlay by the old renderer picker is ignored.
The chat's `cwd` and worktree ports/logs come from `core/runtime.py`;
the chat id is the stable agent-native session id (`~/.claude` / `~/.grok`), so
terminal output and the indexed transcript are the same conversation. Codex is the
exception — it mints its own uuid at launch, so the loom chat id stays and the
native id is **bound** into the overlay (`agent_session_id`) post-launch
(`terminals._codex_bind_watch`: notify-hook file, else rollout cwd-scan; see D17);
indexed codex rows claimed by a binding are re-keyed to the loom chat id.

WS protocol (`/api/ws/term`):

| dir | frame | payload |
|---|---|---|
| browser → loom | first msg (JSON) | `{chat_id, cwd?, cols, rows}` — attach + initial size |
| browser → loom | JSON | `{type:"input", data}` · `{type:"resize", cols, rows}` · `{type:"repaint"}` · `{type:"ping"}` |
| loom → browser | JSON (on open) | `{type:"backend", backend:"pty"\|"tmux"}` — client picks its wheel/repaint path |
| loom → browser | **binary** | raw terminal output (xterm writes it) |
| loom → browser | JSON | `{type:"snapshot-start"}` / `{type:"snapshot-end"}` (pty repaint bracket — binary frames between them are one atomic settled snapshot) · `{type:"exit"}` (claude quit) · `{type:"error", message}` · `{type:"pong"}` |

Live status comes from hooks, not byte-scraping: each session's needs-you marker
(`~/.loom/needs/<chat>`) is set by the agent's Stop/Notification hooks (codex: its
`notify` hook) and cleared on prompt submit (codex: on typing); `GET /api/terminals`
reports it with seconds-since-output, which the sidebar shows as needs-you / working.
Claude subscription usage (5 h / 7 d) comes from the statusLine hook
(`~/.loom/statusline-usage.sh` → `~/.loom/usage.json` → `GET /api/usage`). Per-turn
cost/token readouts are not surfaced.

## REST + WS endpoints (`server/api.py`)
```
GET  /api/health, /api/doctor, /api/usage
GET/POST  /api/repos
GET/POST/DELETE  /api/tasks ;  POST /api/tasks/{id}/{start,stop,test} ;  GET /api/tasks/{id}/{test,logs,chat}
GET  /api/tasks/{id}/logs?kind=  (efficient tail: backend|frontend|test|…)
GET  /api/tasks/{id}/logs/kinds · /logs/since · SSE /logs/stream ;  DELETE /logs?kind=
GET  /api/chats ;  GET/PATCH/DELETE /api/chats/{id} ;  POST /api/chats/{id}/restore
GET  /api/chats-trash ;  POST /api/chats/reindex ;  GET /api/chats/{id}/{transcript,prs}
POST /api/ide ;  POST /api/shell ;  GET /api/terminals ;  POST /api/terminals/{chat_id}/upload
WS   /api/ws/term      (terminal mode — raw PTY bytes; core/terminals.py)
```

## State files (`~/.loom/`, gitignored)
`registry.json` (tasks) · `chats.json` (chat overlay) ·
`sessions_index.json` (index cache) · `repos.json` · `usage.json` (statusLine usage) ·
`needs/` (needs-you markers) · `trash/` (legacy: transcripts the old hard delete moved) ·
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
✅ **In-browser terminal** — the real claude/grok/codex TUI (`terminals.py`) on the pty
host (`pty_server.py` daemon, inline renderer: native xterm scrollback/select/copy),
surviving browser disconnects + loom restarts, with `?chat=<id>` deep links.
✅ **Sidebar-first shell** — the chat rail is the app: tabs, search, reorder,
collapsible categories, notes; dev-stack start/stop + logs live in each chat's
`DevStackBar` (supervised by `monitor.py`).
✅ **Live status** — needs-you / working per chat from agent hooks + output idle time;
Claude subscription usage from the statusLine hook.

**Open cleanup:** delete the legacy tmux host (`TmuxTerminalSession`) once no classic
sessions remain; remove the unmounted `TasksView` / `TaskCard` / `ChatsView`.
