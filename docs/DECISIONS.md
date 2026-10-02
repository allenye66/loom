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

## D13 — Terminal host: pty daemon by default; tmux demoted to fallback (2026-07-07)
The tmux-hosted terminal garbled text on scroll / wrap / resize: tmux enters the
**alternate screen** on attach, so xterm never owns scrollback, and three emulators
(Ink, tmux, xterm) each track width and desync. New default host is a small detached
**`pty_server` daemon** (`core/pty_server.py`, stdlib-only, AF_UNIX socket) running
claude's **inline** renderer — xterm owns a real scrollback (smooth wheel scroll,
drag-select/copy), and the daemon provides the same restart-persistence tmux did
(that was tmux's only real job). Non-obvious load-bearing pieces: alt-screen
filtering of the replay ring, DA1/DA2 query interception, settle-aware snapshots
(bracketed to the browser as `snapshot-start/end`; the client resets + applies
atomically), and partial-frame reassembly on the socket. tmux stays as a per-chat
fallback (`terminal_backend` overlay field; switch = kill + `--resume`) and for
native `tmux attach`; pre-existing live tmux chats stay tmux until switched, so one
session id never runs two claudes. Remove tmux once pty is proven.

## D14 — Dev stacks are reaped when their chat is done (2026-07-08)
Archiving a chat now stops its task's dev stack (`PATCH /chats` → `monitor.stop_for_archived_chat`),
and a **reaper** pass in `core/monitor.py` (startup + every 10 min) stops any task holding services
that has no live terminal session and whose chat is archived — or whose worktree has had no chat
activity for 12h (`LOOM_REAP=0` to disable, `LOOM_REAP_IDLE_HOURS` to tune). **Why:** nothing ever
stopped a stack except the explicit stop button and worktree deletion, so archived/abandoned tasks
kept Django+Vite running for weeks — ~37 leaked stacks (on top of a health-probe restart loop)
exhausted 48 GB RAM + swap into a machine-wide OOM (2026-07-07). A reaped task lands on state
`stopped` like a user stop, so the supervisor won't resurrect it; reopening the chat needs a manual
start from the task card. Teardown safety: `stop_task` now group-kills only pids spawned by THIS
loom process (`process.spawned_this_run`) — a pid recorded by an earlier run may have been *reused*
by an unrelated process — and otherwise tears down by current port listeners, the same
current-state-not-remembered-pid policy `start_task` already used.

## D15 — Supervisor probes tolerate busy servers; vite caches are per-worktree (2026-07-08)
Two amplifiers behind the 121 GB vite-cache / OOM incident. (1) The supervisor treated one missed
1s health probe as "down" and killed the service — but a busy-but-alive vite (mid dep-optimization,
or a loaded machine) routinely misses 1s, so healthy servers were killed in a loop (4k+ restarts
per task), each kill stranding a `deps_temp_*` staging dir in the cache. Now: 5s probe timeout +
`_DOWN_STREAK` consecutive failed sweeps (~45s) before a restart; a crashed port still refuses
instantly and comes back within ~1 min. (2) Kalendir worktrees symlink `node_modules` to the main
checkout, so every worktree's vite shared ONE `node_modules/.vite` — concurrent servers clobbered
each other's dep cache (stale-chunk Lazy/Suspense errors), and Kalendir's config also set
`optimizeDeps.force` for all of development (full ~1 GB esbuild re-scan on every start). Fix lives
mostly repo-side (that's the right layer — loom stays generic): `vite.config.ts` honors
`VITE_CACHE_DIR`, `.loom.yaml` points it at `~/.loom/cache/{slug}/vite` and its start command
sweeps stale `deps_temp_*` (age-gated) from the shared cache for branches that predate the hook.
loom's part: `remove_task` wipes `~/.loom/cache/<task_id>/`, so per-task caches die with the task.

## D16 — Transcripts are sacred: overlay-only trash, and loom never forwards /clear (2026-07-09)
Five /clears silently executed against a live session via a stale-rendering tab whose INPUT
path was still alive (the nastiest freeze variant), each spawning a junk "<local-command-
caveat…" chat: /clear hops to a NEW session id whose transcript starts with the local-command
marker, and the indexer leaked that into first_prompt. Three rules now hold: (1) `trash_chat`
sets an overlay flag ONLY — loom never moves/deletes/modifies anything under
~/.claude/projects (the old behavior moved the jsonl to ~/.loom/trash; restore keeps a legacy
path for files already there). (2) The terminal input funnel (`_SessionBase.write` →
`_guard_input`) swallows the Enter that would submit `/clear` — exact-line match tracked from
the keystroke stream incl. backspace edits and bracketed paste; unknown composer states
(cursor keys / history recall) fail OPEN so real work is never false-blocked; a BEL beeps the
terminal instead. Native `tmux attach` / `⧉ terminal` bypass loom and are out of scope.
(3) The indexer skips local-command noise when deriving first_prompt, so command-headed
sessions can never render as junk chats again.

## D17 — Codex support: bind the native session id post-launch, never pre-mint it (2026-10-02)
Codex CLI (verified on 0.160.0) has no `--session-id`: it mints its own uuid at launch and
only `codex resume <uuid>` reattaches it. So the claude/grok invariant "loom chat id == agent
session id" can't hold. Rather than re-keying live chats (which would break the pty/tmux
session keyed on chat id, WS attaches, and `?chat=` deep links mid-flight), the loom chat id
stays authoritative and the native id is **bound** into the chat overlay as
`agent_session_id`, discovered two ways (first one wins, claim-once):
1. **notify hook** (exact): loom passes `-c notify=["~/.loom/codex-notify.sh"]`; codex invokes
   it on agent-turn-complete / approval-requested / async-question with a JSON arg carrying
   `thread-id`. The env-guarded script (`LOOM_CODEX_NOTIFY_FILE`) drops that JSON per chat;
   it also touches `LOOM_NEEDS_MARK` — codex's analogue of claude's Stop/Notification hooks
   for the sidebar "needs you" dot (cleared from the input stream on real typing, since codex
   has no UserPromptSubmit equivalent).
2. **rollout cwd-scan** (early/fallback): `~/.codex/sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl`
   first line is `session_meta` with `cwd` — and worktree↔chat is strict 1:1, so the newest
   unclaimed rollout in the chat's worktree is this chat's. A fresh launch's watcher
   (`_codex_bind_watch`, 15 min) only scans rollouts written since open; the pre-launch
   recovery path (lost binding) takes the worktree's newest.
The indexer re-keys claimed codex rows to their loom chat id (and titles them from codex's
own `session_index.jsonl` thread names); unclaimed rollouts surface under their native uuid,
so externally-created codex sessions list/resume like claude ones. The binding doubles as an
**adoption** mechanism: writing `agent` + `agent_session_id` into any task's chat overlay
pulls a natively-started session into that task — the chat resolver treats the `task.chat_id`
link as authoritative over transcript-cwd inference and homes the chat in the task's worktree
(sidebar, terminal cwd, dev stack). First used 2026-10-02 to adopt two codex chats started in
`~` into kalendir worktree tasks. Codex writes no rollout
until the first prompt (verified) — same pending-row UX as claude. Zero-content rollouts that
no loom chat opened are hidden as junk (codex writes env-wrapper records even for untyped
sessions). Launch flags mirror the claude posture: `model_reasoning_effort="xhigh"` (≈
`--effort max`), `--dangerously-bypass-approvals-and-sandbox` (≈ `bypassPermissions`, D-adj
to taste: `-s workspace-write -a on-request`), `--search`, `--no-alt-screen` on the pty host.

## Non-goals
Rebuilding the agent/session-orchestration commodity layer; running a target repo's
DB migrations (human-only); remote/multi-machine (local-only); per-worktree dev DBs (D2).
