# CLAUDE.md — loom

Orientation for any AI agent (or human) picking up work in this repo. Read this
first, then the deeper docs in `docs/`.

## What loom is

A **local-first orchestrator for working on many agent coding tasks at once**.
Two halves:
1. **Worktree tasks** — each task = a git worktree on its own branch, with
   deterministic ports + isolated test runs, so you can work/test several
   branches in parallel without checkout conflicts.
2. **In-browser agent TUI client** — the **real** interactive `claude` or
   `grok` CLI (chosen at task/session create, locked per chat), hosted
   server-side (default: a detached `pty_server` daemon, "smooth scroll";
   fallback: a **tmux** session, "classic") and bridged to xterm.js over a
   WebSocket (so every slash command / permission prompt / feature works with zero
   reimplementation), plus a chat manager that indexes `~/.claude` and
   `~/.grok/sessions` history. The goal is to **replace the agent terminal as a
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
  is a real agent CLI (`claude` or `grok`, from overlay `agent` via
  `core/agents.py`) hosted by one of two backends so it survives browser
  disconnects *and* loom restarts — **pty** (default): a detached
  `loom/core/pty_server.py` daemon on `~/.loom/pty-sockets/`, inline renderer,
  xterm owns scrollback (smooth scroll/select); **tmux** (fallback): fullscreen
  agent in `loomx-<chat_id>`. `/api/ws/term` attaches as a subscriber, fanning raw
  bytes to xterm.js; per-chat choice in the overlay (`terminal_backend`), switchable
  (kill + `--resume`). Per-worktree ports/logs are injected via `core/runtime.py`.

Full code map + data flow + the WS protocol: **`docs/ARCHITECTURE.md`**.

## Key conventions / gotchas (don't relearn these the hard way)

- **Terminal sessions have two hosts** — the default **pty** backend needs no tmux
  (a detached `pty_server` daemon keeps `claude` alive across loom restarts); the
  **classic tmux** backend still needs `tmux`. Both scrub `CLAUDECODE`/`CLAUDE_CODE_*`
  from the child so the nested `claude` doesn't inherit auto-approve (see
  `docs/CLAUDE_AGENT_SDK_NOTES.md`).
- Terminal sessions launch with `--effort max` (and agent-specific flags from
  `core/agents.py`). Agent is chosen when creating a task (`agent: claude|grok`)
  and stored sticky in the chat overlay — never switch mid-session.
- The chat manager treats agent transcripts as **read-only** truth
  (`~/.claude/projects/**/*.jsonl` and `~/.grok/sessions/**`) and keeps user
  state (star/archive/tags/name/agent) in `~/.loom/chats.json`.
- Match the existing code style; keep Python imports at top of file.

## Docs

- `docs/ARCHITECTURE.md` — modules, data flow, WS protocol, state files.
- `docs/DECISIONS.md` — design decisions + rationale (read before changing direction).
- `docs/CLAUDE_AGENT_SDK_NOTES.md` — **verified** SDK/CLI/transcript facts (with sources). Trust this over training data.
- `docs/SESSIONS_DESIGN.md` — chat manager / session indexing design.
- `README.md` — install + quickstart.

## Roadmap (current focus)

**Done:** the in-browser terminal — the real `claude` TUI over tmux (`terminals.py`),
surviving browser disconnects + loom restarts; a **sidebar** of per-worktree chats with
click-to-switch + `?chat=<id>` deep links; per-worktree dev-stack start/stop from the
task card.

**Next:** richer per-worktree status surfacing in the sidebar. See
`docs/ARCHITECTURE.md` § Roadmap.
