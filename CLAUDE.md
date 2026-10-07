# CLAUDE.md — loom

Orientation for any AI agent (or human) picking up work in this repo. Read this
first, then the deeper docs in `docs/`.

## What loom is

A **local-first orchestrator for working on many agent coding tasks at once**.
Two halves:
1. **Worktree tasks** — each task = a git worktree on its own branch, with
   deterministic ports + isolated test runs, so you can work/test several
   branches in parallel without checkout conflicts.
2. **In-browser agent TUI client** — the **real** interactive `claude`, `grok`,
   or `codex` CLI (chosen at task/session create, locked per chat), hosted
   server-side (a detached `pty_server` daemon, "smooth scroll") and bridged to
   xterm.js over a WebSocket (so every slash command / permission prompt / feature works with zero
   reimplementation), plus a chat manager that indexes `~/.claude`,
   `~/.grok/sessions`, and `~/.codex/sessions` history. The goal is to **replace the agent terminal as a
   UI** and let you run/▸switch between multiple chats.

A personal tool, shared as-is. Repo: `github.com/allenye66/loom`.

## Run / dev

- **Python** via `uv`, **JS** via `bun`. Prereqs checked by `loom doctor`.
- Run the app: `uv run loom serve` (from the repo root) → http://127.0.0.1:8787
  (serves the built dashboard + the API/WS).
- **Two critical gotchas when iterating:**
  - **Backend (Python) changes need a server restart** — there is no hot-reload
    (`Ctrl+C`, re-run `uv run loom serve`).
  - **Frontend (dashboard) changes need `bun run --cwd dashboard build`** and a
    **browser hard-refresh** (`Cmd+Shift+R`) — the server serves `dashboard/dist`,
    and the browser aggressively caches the hashed bundle.
- Typecheck/build the dashboard: `bun run --cwd dashboard typecheck && bun run --cwd dashboard build`
- **Run loom from a plain terminal, not from inside a Claude Code session** —
  a nested `claude` inherits `CLAUDECODE`/`CLAUDE_CODE_*` env and auto-approves
  tools (see `docs/CLAUDE_AGENT_SDK_NOTES.md`).

## Architecture at a glance

- **Backend** (`loom/`): FastAPI + Typer. `cli.py` (commands), `server/`
  (HTTP + `/api/ws/term` WebSocket), `core/` (the logic). State lives in
  `~/.loom/` (JSON registries + logs), never committed.
- **Frontend** (`dashboard/`): React + Vite + Tailwind v4 (bun). TanStack Query
  for REST, a raw WebSocket for the live terminal.
- **Live terminal** (`loom/core/terminals.py` ↔ `dashboard/src/term/`): each chat
  is a real agent CLI (`claude` | `grok` | `codex`, from overlay `agent` via
  `core/agents.py`) hosted so it survives browser disconnects *and* loom restarts —
  **pty**: a detached `loom/core/pty_server.py` daemon on `~/.loom/pty-sockets/`,
  inline renderer, xterm owns scrollback (smooth scroll/select). It's the only host
  loom starts; the legacy **tmux** host (fullscreen agent in `loomx-<chat_id>`) is
  used only for a classic session that is still alive, until it exits.
  `/api/ws/term` attaches as a subscriber, fanning raw bytes to xterm.js.
  Per-worktree ports/logs are injected via `core/runtime.py`.

Full code map + data flow + the WS protocol: **`docs/ARCHITECTURE.md`**.

## Key conventions / gotchas (don't relearn these the hard way)

- **Terminal sessions run on the pty host** — it needs no tmux (a detached
  `pty_server` daemon keeps `claude` alive across loom restarts); the legacy **classic
  tmux** host only drives a still-live old session. Both scrub `CLAUDECODE`/`CLAUDE_CODE_*`
  from the child so the nested `claude` doesn't inherit auto-approve (see
  `docs/CLAUDE_AGENT_SDK_NOTES.md`).
- Terminal sessions launch with max effort (and agent-specific flags from
  `core/agents.py`). Agent is chosen when creating a task (`agent:
  claude|grok|codex`) and stored sticky in the chat overlay — never switch
  mid-session.
- The chat manager treats agent transcripts as **read-only** truth
  (`~/.claude/projects/**/*.jsonl`, `~/.grok/sessions/**`,
  `~/.codex/sessions/**`) and keeps user state (star/archive/tags/name/agent)
  in `~/.loom/chats.json`.
- **Codex has no `--session-id`** — it mints its own uuid, so the chat overlay
  binds the discovered native id as `agent_session_id` post-launch (notify hook
  → rollout cwd-scan) and resume goes through `codex resume <that id>`. See
  `docs/DECISIONS.md` D17 before touching that flow.
- Match the existing code style; keep Python imports at top of file.

## Docs

- `docs/ARCHITECTURE.md` — modules, data flow, WS protocol, state files.
- `docs/DECISIONS.md` — design decisions + rationale (read before changing direction).
- `docs/CLAUDE_AGENT_SDK_NOTES.md` — **verified** SDK/CLI/transcript facts (with sources). Trust this over training data.
- `docs/SESSIONS_DESIGN.md` — chat manager / session indexing design.
- `README.md` — install + quickstart.

## Status

**Working today:**
- **Worktree tasks** — worktree + deterministic ports + isolated test runs per branch
  (from the CLI, or `+ new` in the sidebar).
- **In-browser terminal** — the real `claude` / `grok` / `codex` TUI on the pty host,
  surviving browser disconnects + loom restarts; `?chat=<id>` deep links.
- **Projects** — the sidebar shows one registered repo at a time; switch or add one from
  its header (any git repo — a `.loom.yaml` is optional; without one: no dev services, base
  branch = git's default). Categories are per project.
- **Sidebar-first shell** — the chat rail *is* the app (no separate Tasks/Chats pages):
  loom's task chats with active/archived tabs, search, drag-to-reorder, collapsible
  categories, per-chat needs-you / working status, and notes.
- **In-chat tools** — dev-stack start/stop + FE/BE/test logs, transcript search,
  copy-text panel, ⌘↑/⌘↓ message jumps, image drop, PR badges, Claude usage chip,
  `⧉ terminal` (plain shell in the worktree), open-in-editor.

**Open cleanup:** delete the legacy tmux host once no classic sessions remain, and the
unmounted `TasksView` / `TaskCard` / `ChatsView` components. See
`docs/ARCHITECTURE.md` § Roadmap.
