# loom — Design Decisions

Why loom is the way it is. Read before reversing a direction. Newest context at
the bottom of each entry. (Dates are when decided.)

## D1 — Build strategy: hybrid, then a full client (2026-05-31)
Started as "adopt an existing session tool (Conductor/Claude Squad/native CC
worktrees) + build only the env-orchestrator." Evolved: the user wanted
loom to **fully replace the Claude Code terminal UI**, so loom now also includes
its own in-browser Claude client (live chat). The worktree/test orchestration and
the chat client are the two pillars.
**Why:** the session layer is commodity, but a clean web chat that's unified with
worktrees + history is the differentiator the user actually wanted.

## D2 — Database: shared dev RDS only (2026-05-31)
Worktree tasks all point at the **one shared dev RDS**; no per-worktree DBs.
**Why:** the user primarily wants to see real data; cloning/seeding per-branch DBs
isn't worth the infra. **Accepted risk:** concurrent destructive testing shares
data; a human-run migration is global. Tests are unaffected (they use Docker PG
`:7432`, not the RDS), so per-worktree **test** isolation is still in scope.

## D3 — Interface: web dashboard first (2026-05-31)
A FastAPI + React dashboard, not a CLI/TUI. The Typer CLI exists underneath.

## D4 — First milestone: test isolation (2026-05-31)
Phase-1 = worktrees + isolated test runs (so `/address-comments` etc. run across
PRs). Isolation via a **serialize file-lock** by default (works today, no target-repo
change); `db-suffix`/`db-port` modes need the test suite to read the injected env var.

## D5 — Chat manager = read-only index + local overlay (2026-05-31)
loom never edits Claude's transcripts. It indexes `~/.claude/projects/**` and
keeps user state (star/archive/hide/name/tags/description) in `~/.loom/chats.json`.
Delete = soft-trash. Search depth = metadata + prompts. Replaces `/resume` +
`/find-session`. See `SESSIONS_DESIGN.md`.

## D6 — Live chat via the Python Claude Agent SDK (2026-06-04)
Considered three routes for the in-browser client:
- **Embedded PTY terminal** (xterm.js) — fast, full-fidelity, but "a terminal in a
  tab," not clean. Rejected as the primary.
- **Remote Control** (`claude remote-control`) — **rejected**: it's a closed relay
  to Anthropic's *own* clients (claude.ai/code, mobile), exposes no local API.
- **Python Agent SDK** (`claude-agent-sdk`) — **chosen**: in-process streaming
  (`include_partial_messages`), `cwd`=worktree, `resume`, and a `can_use_tool`
  callback for in-UI approvals. Sessions persist to `~/.claude` → unified with the
  chat manager. Facts in `CLAUDE_AGENT_SDK_NOTES.md`.

## D7 — Tool permissions: mirror the terminal; special-case AskUserQuestion (2026-06-04)
Read-only tools (`Read/Grep/Glob/LS/NotebookRead`) auto-allow; everything else
routes through `can_use_tool` → an in-UI Allow/Deny. `AskUserQuestion` is always
allowed and rendered as a real question card (returns `{questions, answers}`).
**Gotcha discovered:** the Python `can_use_tool` callback only fires with a
`PreToolUse` keepalive hook; and a `claude` spawned *inside another Claude Code
session* inherits env that auto-approves. So run loom from a plain terminal.
**Open:** a `plan`/read-only permission-mode toggle is still TODO.

## D8 — Sessions run at effort="max" (2026-06-04)
`ClaudeAgentOptions.effort` defaults to `None` (SDK default, not max), so loom sets
`effort="max"` explicitly (overridable per session via the `start` message).

## D9 — Assistant messages render as markdown (2026-06-04)
react-markdown + remark-gfm + rehype-highlight. Raw text read as broken.

## D10 — Resume renders transcript history (2026-06-04)
SDK `resume` loads context but does **not** re-emit prior messages. So loom
reconstructs the transcript (`sessions.get_transcript`) and seeds it before the
live stream continues — otherwise a resumed chat looked empty / "lost."

## D11 — Sessions move server-side (decided 2026-06-04, in progress)
The WS currently owns the SDK session, so refresh/close kills the agent and only
one chat is viewable. Decision: make sessions **server-side persistent** (a
registry that keeps `ClaudeSDKClient`s alive, buffers events, tracks status), with
the WS as an attach/detach viewer. Unlocks refresh-safety, multi-chat sidebar +
"needs you" status, and URL deep-links. See `ARCHITECTURE.md` § Roadmap.

## D12 — In-browser client = the real terminal, not the SDK chat (2026-06-20)
The structured **Agent-SDK chat** (D6–D11: `core/chat_sessions.py` + a React
`ChatView`/`useChat` that reimplemented Claude Code's UI from SDK events) was
**removed** in favor of terminal mode alone — the *actual* `claude` TUI hosted in a
server-side tmux session and bridged to xterm.js (`core/terminals.py`).
**Why:** the real TUI gets every slash command / permission prompt / feature for free
(zero reimplementation) and survives loom restarts via tmux, whereas the SDK chat was
a large surface that always lagged the CLI. The chat **manager** (`sessions.py`,
read-only index + overlay) and the worktree/test core are unchanged. Supersedes D6–D11.

## Non-goals
Rebuilding the agent/session-orchestration commodity layer; running a target repo's
DB migrations (human-only); remote/multi-machine (local-only); per-worktree dev DBs (D2).
