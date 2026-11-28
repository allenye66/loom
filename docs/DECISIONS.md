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
**2026-11-01:** terminal launches now default to `xhigh` (claude `--effort xhigh`, grok
`--effort xhigh`, codex `model_reasoning_effort="xhigh"`) — Allen's preference over max.

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
*(The policy below was replaced by D34: "in use", parking instead of `stopped`, a 30 min idle window.)*
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
**Sub-agent rollouts are not chats**: codex multi-agent v2 gives every spawned sub-agent its
own rollout file in the same store (session_meta: `thread_source:"subagent"` +
`parent_thread_id`) — in the parent's cwd, i.e. the chat's worktree — and refuses to resume
one directly ("resume the parent first", -32600). The indexer flags them (`subagent`),
list_chats drops them, and the bind scan skips them (else a fresh chat's watcher could claim
its own session's sub-agent thread). Found live 2026-10-02: a resumed worktree session
spawned a review sub-agent whose rollout surfaced as a ghost bare-uuid chat.
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
`--effort xhigh`), `--dangerously-bypass-approvals-and-sandbox` (≈ `bypassPermissions`, D-adj
to taste: `-s workspace-write -a on-request`), `--search`, `--no-alt-screen` on the pty host.

## D18 — Status briefs: loom summarizes the transcript; the agent doesn't self-report (2026-10-11)
Problem: you give a worktree agent freedom, it runs for hours, and you lose the high-level
picture. Per-tool-call "now" lines don't help at hour three. Asking the agent to keep a
progress file was rejected: it costs tool calls, and it goes stale exactly when the agent is
deep in a long run. Instead loom reads the transcript (already the source of truth, D16) and
has **Haiku** write a brief: goal as it stands now, status, working vs idle time, phase
timeline, decisions attributed you/agent, unasked work, needs-you.
- **Input matters more than the model.** Prototype on real sessions found each of these made
  the brief wrong until fixed: mid-turn messages are `queued_command` attachments (not user
  records); AskUserQuestion answers are tool_results; `isMeta` records are harness noise
  (except cross-session handoffs); idle time must be **computed** from timestamps (a 2h48m
  outage after an API error was described as "testing" until gap markers were injected).
  Tool outputs are dropped, so the prompt forbids numbers not in the text.
- **Transport:** headless `claude -p --model haiku` on the user's own login (no API key;
  counts toward plan usage) with `--no-session-persistence` (no transcript for the chat index
  to adopt), `--setting-sources ""`, `--tools ""`, `--json-schema` for a structured brief.
- **Cost:** measured 1.5¢ list price for a 9 h session (~65k tokens in, ~25 s); 0.6¢ for 70
  min. One-shot over the whole compressed timeline (capped ~180k chars: tool lines dropped
  oldest-first) — no incremental folding, so no error accumulation.
- **Refresh only while watched:** the panel's polls pass `auto`; the server regenerates when
  the transcript grew and the turn ended (needs-you marker newer than the brief) or 10 min
  passed. Closed panels read the cache only.
Claude transcripts only for now; codex/grok need their own timeline builders.

## D19 — A chat's PRs are the ones it created, proven by GitHub's createdAt (2026-10-16)
Claude's `pr-link` records were the only PR source, and they're a "current PR" pointer, not
a ledger: one script that opened #2006 and #2007 got a pr-link for #2006 only. Scanning the
transcript for any PR URL would fix that but also register every old link the user pastes or
the agent looks up. The rule instead: **a PR belongs to a chat iff its URL appeared on the
agent's side (tool output, task notification, subagent transcript, pr-link — never user text)
AND GitHub's `createdAt` falls inside a window when one of the chat's tool calls was running**
(±2 min). Background calls stay open until their `<task-notification>` (it names the
tool-use-id). The parsers store sightings + merged windows in the index (`pr_seen`,
`tool_windows`; windows kept only when something was sighted); `github.created_prs` checks
them with one cached GraphQL call per repo (`~/.loom/pr_created.json` — createdAt is
immutable). Over all local chats: 565 sightings → 448 PRs. The 21 pr-link PRs this drops were
all created before the chat or by someone else while it was idle (a chat that picked up an
existing PR). For those, `+ pr` pins one (`prs_pinned`), and × hides a wrong one (`prs_hidden`).
Offline (gh can't answer), pr-link PRs are still trusted. Grok: no tool output seen yet to parse.

## D20 — loom is self-aware: agents drive loom through the `loom chats` CLI (2026-11-06)
Goal: tell the agent in any chat "close and archive these", "hand this off to a new loom
session", "break down every active chat in category X" — loom as a tool the agents use, not
only a UI around them. Decisions:
- **Interface = a CLI + a system-prompt note, not MCP.** `loom chats ls|show|brief|archive|
  unarchive|close|star|rename|categorize|spawn|send|open`, `loom categories ls|add|rm|rename`,
  `loom whoami` (`loom/harness.py`). It works the same in claude, grok and codex (all have a
  shell), from a Claude outside loom, and by hand. Every chat gets a short `<loom-harness>` note
  (`runtime.HARNESS_NOTE`, appended to the worktree note) plus `LOOM_CHAT_ID` (who `self` is),
  `LOOM_API_URL` (set by `loom serve`, so a chat reaches the server that launched it) and loom's
  venv bin on PATH. An MCP wrapper can come later if typed tools are wanted.
- **The CLI is a thin HTTP client of the running server**, never the JSON files: the server owns
  the live agent processes, and one writer avoids racing the dashboard. Output is plain text
  (chat titles contain `[brackets]` that rich would eat as markup), `--json` for scripting,
  `ls --ids | xargs …` for bulk. Chat refs: id, unique prefix, name, branch, task, or `self`.
- **Project-scoped by default.** The user runs several projects at once, and "archive all the
  idle chats" said inside a Payments-repo chat means that project's chats. So `ls`, `spawn` and
  `categories` default to the caller chat's project (else the repo containing the cwd; `spawn`
  with neither asks which). `--project NAME` picks another, `-A` means every project, and
  `loom projects` lists them. Chat refs resolve across projects (an id names one chat
  anywhere); categories stay per project, so filing a chat uses its own project's categories.
- **A spawned chat matches its spawner's model and effort** unless told otherwise (`--model`,
  `--effort`). "Match" means what the spawner is running *now*, read from its own session files
  (`agents.live_settings`: claude transcript `requestedModel` + `effort` on each assistant
  record, so a mid-chat `/model` counts; grok `summary.json`; codex `turn_context`, unverified),
  else its pin, else the CLI's default model and xhigh. The model carries over only to the same
  CLI (model names don't cross CLIs); the effort carries over when the new CLI has that level.
  The result is **pinned** in the new chat's overlay (`model`, `effort`) and passed on every
  launch, resume included, so it never silently drifts to a different global default. Chats
  without a pin launch exactly as before. Efforts are validated per CLI (400 before any worktree
  is made); models aren't (no list exists) — claude only rejects a bad one on its first request,
  so spawn watches for that error and reports it.
- **Categories moved server-side** (`~/.loom/categories.json`, `core/categories.py`). They lived
  in localStorage, invisible to any agent. The dashboard store keeps its API (optimistic update →
  adopt the server's response, 4 s poll so agent edits appear), imports a browser's legacy
  localStorage categories once (idempotent merge: missing ids appended, a chat's assignment only
  if the server has none; the old key is left as a backup), and shows the legacy ones read-only
  if the server is older than the dashboard (rebuilt, not yet restarted).
- **Archive stops the agent.** Archiving already stopped the dev stack and freed ports; the agent
  process kept running behind an archived chat. Now `PATCH archived:true` also closes it (the
  sidebar ⊘ too) — except when the caller is that chat itself (`X-Loom-Chat` header), which would
  kill the turn asking. `close` = stop the agent but stay active. Transcripts stay (D16); reopening
  or `send` resumes. Open tabs get `{type:"exit"}` ("exited — close and reopen").
- **Headless start.** A chat's agent used to start only when a browser attached. `start_chat`
  launches it with no subscriber; `spawn` = `create_task` + category + `start_chat`. The first
  message rides as the CLI's trailing positional prompt (claude `[prompt]`, grok `[PROMPT]`,
  codex `[PROMPT]`; brand-new sessions only) — no typing into a TUI that's still booting. Verified:
  a headless claude answers in ~5 s, and opening it later in the browser shows the full exchange.
- **`send` types through the normal input funnel** (bracketed paste so multi-line stays one
  message, 0.3 s, then Enter), so the /clear guard still applies and a busy agent queues it. A
  stopped chat is resumed first (wait until its output goes quiet). It refuses (409) to splice into
  someone's draft: the composer line is known non-empty, or unknown (cursor keys since the last
  submit) within 2 min of real keystrokes. Testing this found that **focus reports (`ESC[I`/`ESC[O`,
  sent on every click into the terminal) tainted the line tracker** — the /clear guard (D16)
  failed open from the first click. They're now transparent, like mouse reports.
- **Claude's folder-trust prompt is reported, never answered.** A repo Claude has never trusted (a
  worktree inherits its main repo's trust) opens on "Is this a project you trust?", defaulting to
  "No, exit"; a headless first message waits behind it. `waiting_on()` spots it in the output tail,
  `spawn` warns, `ls`/`show` say `blocked`, and `send` refuses (its Enter would pick "No, exit").
  Answering a security prompt for the user is not loom's call. (In the browser the prompt can paint
  blank until you click into the pane — the focus report makes claude redraw it.)
- Guard rails: an agent can't `close` or `send` to itself without `--force`; bulk verbs take
  `--dry-run`; the note says "only act on other chats when the user asks"; spawned chats record
  `spawned_by` (shown by `show`).

## D21 — Archive / delete answer at once; the slow part runs after the reply (2026-11-17)

Archiving a chat with a live agent took ~5 s (measured 5.2 s ×3): `PATCH /chats` waited for
`terminals.close_chat` — the pty daemon SIGTERMs its child and waits for it to exit, and on macOS
a `claude` can sit in the exit state for a long time — then the dev-stack stop and port release.
Deleting a 30k-file worktree took ~1.9 s (real ones with `node_modules` far more). Now:
- **Server.** `PATCH /chats` replies right after the overlay write (~2 ms); stop-agent / stop-dev-stack /
  release-ports (and, on unarchive, re-allocate-ports) run in `_archive_followup`, one chat's
  follow-ups strictly in request order, each acting on the chat's *current* archived flag — so
  archive→unarchive in quick succession ends unarchived with ports, however they interleave.
  Failures are logged (`[loom archive] …`), never raised. `terminals.close_chat` is now joinable
  (`_closing`) and `open_terminal` / `start_chat` wait out an in-flight close: the dying daemon's
  last act is unlinking its socket files, which would otherwise delete a re-opened chat's new ones.
  `DELETE /tasks/{id}?force=true` is split (`manager.detach_task` — registry, instant — then
  `finish_removal` — stop stack, rm caches + worktree — in the background; a force removal never
  failed anyway); re-creating the same branch meanwhile is refused until it finishes. A plain
  (non-force) delete stays synchronous, since it can be refused. A crash between the two halves
  leaves an orphan worktree directory, not a half-registered task.
- **Client.** Archive / unarchive / delete-worktree update the cached lists optimistically and roll
  back with an inline error if the request fails (`useChatActions().patch`, `useTaskActions().remove`).
- **Creating a chat** shows a placeholder sidebar row + a "creating…" pane from submit, then the
  terminal pane's own "starting <agent>…" until the first output. The real row only appears once an
  agent attaches (`opened_at`), so the placeholder stays until then.
- D20's semantics are unchanged: archiving still stops the agent (never the caller's own) and the
  dev stack — just not on the request path.

## D22 — Category message boards, per-message context injection, `@@` mentions (2026-11-17)
**Update (11-18): boards are opt-in.** A category's board is **off by default**; the user turns it on
(the ▤ on the category header → "enable message board"; or `loom board enable --category X` in their
own shell). Only the user can: a request carrying an agent's `X-Loom-Chat` gets 403, and `loom board
enable` refuses inside a chat. There is no off switch for now. While off, `post`/`read`/`wait` answer
with what to do ("only the user can turn it on… message chats with `loom chats send`"), a categorised
chat still gets its roster ("this category has no message board — the user can enable one"), and
`loom whoami` says `board: off`. Switching it on re-sends the roster block ("the user just ENABLED
this category's board") on each chat's next message, so every chat learns the state without being
nudged; `whoami` / `board ls` always show it. The flag lives in `loom.db` (`board_settings`).
Goal: let the chats in one category work *together* — try three designs then reconvene; an
orchestrator spawns QA chats and collects their reports — without the user pasting chat URLs
between them. Decisions:
- **A board per category, in SQLite.** `loom board post|read|wait|ls` + a dashboard modal. Posts are
  an append-only log with per-reader read cursors (`core/board.py`, `loom.db`, WAL). SQLite rather
  than another JSON file because `seq` assignment and cursor moves are single transactions (verified
  with 8 threads / 6 processes posting at once: no lost or duplicate seq), "posts since N" is an
  index lookup, and it is the natural home for shared state that agents and the dashboard both
  write. Existing JSON state (`chats.json`, `categories.json`, `registry.json`) is *not* migrated:
  it has one writer (the server) and works. Chats outside any category have no board — every
  existing workflow is unchanged. The user's notes/chat order still live in the browser's
  localStorage; moving them server-side (so agents can read them) is a separate job.
- **Context arrives per message, not in the system prompt.** A `UserPromptSubmit` hook (a tiny
  `curl`, not the Python CLI — startup would add ~250 ms to every message) POSTs claude's hook
  JSON to `/api/chats/{id}/prompt-context`; the answer is attached to that turn, or nothing.
  Returned only when there is something to say: the category roster (first message of a Claude
  session, again only if the roster changes), unread board posts (then marked read), and resolved
  `@mentions`. A chat with nothing new — and every uncategorised chat — adds **zero tokens**.
  Baking the category and siblings into the system prompt was rejected: it goes stale the moment
  the user drags a chat to another category. Only claude has this hook; grok/codex get the harness
  note and resolve `@name` through the CLI (`loom chats show @name`).
- **Update (11-19): `#` is the primary mention trigger; `@@` still works.** Probed against a real
  claude 2.1.328 TUI: `@` makes claude draw its big file picker (MCP resources included), and `@@`'s
  take-back Backspace then tears it down — a visible flicker, which is why `@@` felt unreliable. Claude
  does nothing with `#` (no memory mode in this version), so `#` at the start of a word is *swallowed*
  and opens loom's picker with nothing to undo. Real uses of `#` stay typeable: Space on an empty query
  types `# ` (heading), `##` types `##`, and `#123` / `#foo` with no match commit literally on
  Space/Enter/punctuation; mid-word `#` (`C#`) never triggers. Verified in the real TUI via the browser.
- **`@@` triggers the mention picker** (claude's own `@` is its file picker). The first `@` has
  reached the TUI before the second arrives, so the picker sends a Backspace to take it back out,
  swallows keystrokes while open, and pastes `@chat-slug ` (bracketed, so claude's picker doesn't
  open on it). The visible text stays a plain `@name`; the hook resolves it server-side — slug of
  the chat's name, then id prefix, then a unique slug prefix, same-category first, and only chats
  that exist (so `@README.md` is left alone). Escape hatches: Esc cancels, Backspace on an empty
  query leaves one `@`, Space on an empty query types a literal `@@ `. Mid-word `@@` never triggers.
- **Nudges push, the hook delivers.** `loom board post --to <chat>` types a one-line heads-up into
  a *running* target (never launches a stopped chat; refuses over an unsent draft; ≤5 per
  author→target per minute so two agents can't ping-pong forever). The content isn't repeated in
  the nudge: the target's hook attaches the unread posts to that very message.
  **Update (11-22): tagging `@chat-name` in the post text nudges exactly like `--to`** (server-side, any author —
  agents and the dashboard alike): the tagged chat is nudged and the post is addressed to it ("→ you" in its digest).
  Only the category's own members can be tagged (they're the ones who can read the board), never yourself; an `@token`
  that matches nobody (a typo, an `@decorator` in a snippet) is reported back to the poster instead of failing silently.
  A *stopped* chat still isn't launched unless the poster passes `--wake` (the user closed it). Verified with real
  agents: a lead tagged `@worker-b` with no `--to`, the nudge reached worker-b's input, it answered on the board.
- **Measured** (real agents in an isolated loom; 3 workers + a coordinator implementing `slugify`
  three ways; board vs `loom chats send`; n=2 per arm, so *indicative, not significant*): both
  mechanisms delivered 3/3 reports with verbatim-correct tables and picked the true winner every
  time; wall clock ≈ equal (96 s vs 91 s); the coordinator made 4.0 vs 7.5 API calls and re-read about
  half the cached tokens (178k vs 364k, means) in the board arm (it blocks on `wait -n 3` instead of
  taking a new turn per message). The overhead the board adds: ~150 tokens of static harness note per chat, one
  ~110–180-token roster block per chat session, ~13 ms (`curl`) + 5–35 ms (server) per message.
  The board's real advantages here are structural (a durable, human-readable record; broadcast;
  late joiners; no input-box collisions), not raw speed on a one-shot task.
- **Trust boundary:** a board post is text from a peer agent, injected into another agent's
  context — the same boundary `loom chats send` already crosses. Posts are labelled by author and
  nothing in the digest is phrased as an instruction; treat the board like any other tool output.

## D23 — A chat follows its conversation when claude gives it a different session id (2026-11-18)
Problem: loom keys everything it owns by the chat id it launches claude with (`--session-id`): the
terminal socket, the hooks, `$LOOM_CHAT_ID`, categories, the board. Claude 2.1.x's background daemon can
hand a chat a pre-warmed session instead (`claimed-spare` in `~/.claude/daemon.log`), so the transcript
is named by *another* id. Loom then saw "no transcript" for its own id, showed a blank duplicate row,
launched an empty claude on an old link, lost the chat's category/board, and `--resume <real id>`
was refused while the daemon still hosted the session. Fix = the pattern codex already used (D17):
- **Bind, then re-key.** `agent_session_id` (overlay) names the real session; `build_index` presents
  that transcript's row under the loom chat id, so the sidebar, categories, board, terminal, deep links
  and `self` agree with no alias handling. On the first bind, the session row's own overlay (name, star,
  archive, tags) and category are adopted by the loom id. Resume uses `--resume <agent_session_id>`.
- **Detection (both exact, neither a guess).** (1) The `UserPromptSubmit` hook receives claude's real
  `session_id` with every message; if it isn't the chat's id, `sessions.bind_session` binds it. (2) For
  chats that diverged before this (or while the hook was absent), `repair_binding` finds the transcript
  in the chat's worktree that contains loom's own hook command for it (`needs/<chat id>` — loom bakes
  it into every session it launches; a session someone ran by hand never has it). Runs at server start,
  about once a minute, when a diverged chat is opened by id, and just before launching a claude chat
  that isn't running — so it resumes instead of starting blank. Negative scans are memoised.
- **Never rebound** when the chat has a transcript under its own id (a `/clear` continuation stays its
  own row) or the session already belongs to another chat. Old links/notes keyed by the session id
  still resolve (`canonical_id`; `GET /api/chats/{id}` returns the loom id the dashboard adopts).
- **Background-hosted sessions.** Before launching, if claude's daemon roster hosts the session,
  `agents.ensure_not_in_background` runs `claude agents --json`: idle + done → `claude stop` (the
  conversation is kept) and resume; busy → `SessionBusy` with how to attach/stop, never interrupting work.
  Cost when the daemon isn't involved: one small file read.
- Verified with real claude: a conversation run under a different session id, then asked by its loom
  id — resumed (`--resume <real id>`, the agent remembered a secret word) via both detection paths, and
  a real `claude --bg` session was stopped and resumed. Your actual affected chat is found in 4 ms.

## D24 — The attention layer: lifecycle events, notifications with per-chat mute, conflict radar (2026-11-19)
Goal: cut the human's attention cost across dozens of chats (the research round's top asks). Decisions:
- **Exact agent state from the agent's own hooks.** Claude `Stop` / `Notification` / `SessionStart` /
  `SessionEnd` (plus `UserPromptSubmit`, already posted for the context hook) POST to
  `/api/chats/{id}/event` → `chat_events` in `loom.db` (`core/events.py`, 300 newest per chat). `/api/terminals`
  gains `turn` (running | waiting | idle | ended) + `turn_since` — exact, survives loom restarts, and unlike
  `idle_sec` is not inferred from output silence. The `needs/<id>` marker files stay (they also serve
  grok/codex, which have no such hooks). The event hook discards curl's output (a SessionStart hook's stdout
  is injected into the conversation); verified against real claude that the extra hook events are accepted.
- **Desktop notifications, per-chat mute.** *(Superseded by D28: notifications are now explicit, pushed by the
  agent — not inferred from "the turn ended". The mute flag and the `(N) loom` tab badge remain.)* The first version fired on every
  `Stop` / `Notification` hook event, which cannot tell "done" from "I need your decision".
- **Conflict radar, scoped to a category.** *(Reworked by D28: PR-based, and invisible to the user.)* The first version
  showed a ⚠ badge/modal for any two chats changing the same files — noise for independent chats.
- **`⌘Z` / `Ctrl+Z` undo the input box.** Claude's input has its own undo on Ctrl+_ (word-granular; it
  restores Backspace-deleted text; no redo; its first step is sometimes a no-op) and a kill-ring (Ctrl+Y
  restores text removed with Ctrl+U/W). The terminal maps ⌘Z to Ctrl+_, retrying once if the screen didn't
  change, instead of reimplementing an undo model. Ctrl+Z is mapped too, since forwarding it would SUSPEND
  the agent. Verified in the real TUI.

## D25 — The API only answers this machine's own pages and tools (2026-11-19)
Problem: CORS was `*` and nothing checked `Host`/`Origin` on REST or the terminal WebSocket, while the API
can type into agents that run with bypassed permissions (`/chats/{id}/send`, `/api/ws/term`, `/shell`).
Verified against the live server: a page on another origin read 3 projects / 260 chat records. Loopback
binding only keeps *other machines* out — the user's own browser will call it for any site it has open.
- `server/guard.py`, a pure-ASGI middleware outside CORS (so it covers WebSocket handshakes and a refused
  request gets no CORS headers): **Host** must be a loopback name (kills DNS rebinding — the browser's
  Host mirrors the attacker's domain) and, if the browser sent an **Origin**, it must be a loopback origin
  on any port (the dashboard, `vite` dev servers, per-worktree frontends). `Origin: null` and foreign
  sites get 403 / WS close 1008. CORS now echoes only loopback origins (`allow_origin_regex`), never `*`.
- Requests with no Origin (the `loom` CLI, `curl`, the agents' hooks) are unaffected — they aren't browsers.
- Off when loom is bound to a non-loopback address (`loom serve --host 0.0.0.0` — you chose the network);
  `LOOM_ALLOWED_HOSTS=a.ts.net,…` adds names deliberately (e.g. reaching loom over Tailscale on loopback).
- **Known gap:** a page served from *another localhost port* is a loopback origin and still allowed — one
  of your own dev servers with an XSS hole or a malicious dependency can still read/drive loom. Closing it
  needs a secret shared by the dashboard and the CLI/hooks (a per-install token in `~/.loom`, sent as a
  header, injected into agents' env); deliberately not done yet — it touches every client.
- Verified in a real browser: a page on a foreign origin can neither read the API nor open the WebSocket
  (403 in the server log, nothing reached an agent); the dashboard works on `localhost` and `127.0.0.1`.

## D26 — Projects can be hidden (reversible), not deleted (2026-11-19)
Want: take a repo out of the way (finished project, accidental add) without losing anything.
- **A `hidden` flag on the project's entry in `repos.json`** (`repos.set_hidden`, `PATCH /api/repos/{name}`
  `{hidden}`; `GET /repos` omits hidden ones unless `include_hidden=true`). Nothing is deleted or stopped:
  worktrees, tasks, chats and running agents are untouched, and internal callers (`sessions` resolving a
  chat's repo from its cwd, spawning into a project, the harness's cwd→project lookup) still see every
  project — so a hidden project's agents keep working and `loom chats …` still resolves from inside it.
- **Out of sight and quiet.** Hidden projects' chats are left out of the sidebar, ⌘K, the `(N) loom` tab
  badge and desktop notifications (a notification for a chat you can't see would be a dead end).
- **UI:** the project menu's hover eye-slash hides instantly with an **undo** toast (no confirm dialog —
  it's reversible); a "hidden (N)" list in the same menu has "show". Hiding the active project falls back
  to the next one; with all hidden the sidebar shows "no project" and the menu keeps the restore list.
  Re-adding a hidden project (`+ add project…`, `loom repo-add`) also un-hides it. CLI: `loom repo-hide /
  repo-unhide <name>`, `loom projects [--hidden]`.
- Not built: truly forgetting a project (deleting its entry / tasks) — hide covers "get it out of my way"
  and can't lose work.

## D27 — An agent can find the chat that wrote a PR, and ask it (2026-11-20)
Want: an agent that must build on / review / debug someone else's PR sometimes needs the *why* the diff
and PR text don't carry — the agent that wrote it has it. Chat → PRs already existed (`/chats/{id}/prs`,
D19); nothing went the other way and nothing let an agent use it.
- **`core/prlink.py` (PR → chat), `GET /prs/chats?ref=`**: a chat is tied to a PR by `created` (its transcript
  shows it opening it, confirmed against GitHub's createdAt — D19), `pinned` (the user attached it) or
  `branch` (the PR's head branch is its task branch — catches PRs opened by hand / by a chat whose
  transcript lost it). Strongest first; a chip the user removed stays removed; only loom task chats.
  `github.pr_status` now carries the head `branch`; `github.parse_pr_ref` takes a URL, `owner/repo#N`, or a
  bare number (resolved against the caller's repo).
- **Harness:** `loom chats pr <ref>` (who wrote it + their status + next steps) and `loom chats ask <id>
  "<question>"` (sends the question, blocks until that agent's turn ends, prints its reply; `--wait` ≤ 100 s
  a call, exit 3 + "read it later" on timeout; refuses an *archived* chat without `--wake`, refuses
  yourself). Completion = Claude's `Stop` event (D24) *and* the question's `[ask <nonce>]` found in the
  transcript, so the old turn's Stop isn't mistaken for the answer when the target was mid-turn; grok/codex
  (no events) fall back to "quiet for a few seconds".
- **Read before you ask.** The HARNESS_NOTE tells agents: look the author up, read `brief` / `show` (free),
  and ask only if that doesn't answer it — asking interrupts that agent (and costs a turn). Verified with
  real Haiku agents: a reviewer whose answer was in the author's transcript read it and did not ask; one
  who needed the author's own knowledge asked and got the reply.
- **Provenance:** `POST /chats/{id}/send` called by another chat (X-Loom-Chat) prepends `[message from
  another agent in loom — chat "name" (id8), not the user]`, and the note tells agents a peer can't authorize
  destructive / out-of-scope actions. Before this, an agent's message was indistinguishable from the user's.
- Limits: needs `gh` for the head-branch / createdAt checks (without it, only transcript evidence);
  chats launched before this keep the old note until relaunched (it's in the system prompt).

## D28 — Explicit agent-pushed notifications; PR-conflict awareness for agents only (2026-11-20)
Feedback on D24: the "needs you" signal was a guess (a turn ending is true for "done" and for "I have a question"),
and the conflict radar was something the user never wanted to look at.
- **Notifications are pushed by the agent.** `loom notify "<question>"` (any agent — claude/codex/grok; no hooks needed)
  writes an item to the user's **inbox** (`core/inbox.py`, SQLite `inbox`), pops a desktop notification (bell on, tab in
  the background, chat not muted; bursts coalesced) and appears in the dashboard's **notification panel** (the bell in
  the sidebar header, with a count badge): each card shows the chat, project/category, age and the question, with an
  inline reply box, "open" (jump to the chat) and dismiss. The HARNESS_NOTE tells agents when to use it (they need the
  user's decision/approval/information and can't decide; never for progress or "done") and to end their turn with the
  question. Guards: identical open text is merged, ≤ 5 open per chat. The tab title `(N) loom` counts unmuted open items.
  The old Stop/Notification-hook desktop pushes are gone; the lifecycle ledger stays (it drives `turn` state and `ask`).
- **The agent knows what the user's message answers.** A panel reply is typed into the chat prefixed
  `[reply to your notification — "<question>"]` (the item is marked answered *before* typing so the hook doesn't
  double-note). If the user instead types in the chat itself, the UserPromptSubmit hook (all chats, categorised or not)
  attaches "The user is replying to what you asked them with `loom notify`: …" and closes the items; a message another
  agent sent (tagged, D27) doesn't count. grok/codex have no hook, so a direct reply there isn't auto-detected — use the
  panel or dismiss.
- **PR-conflict awareness, for agents.** `core/radar.py` now looks at the **open PRs** created by chats in the same category
  (evidence: transcript / PR scout / pinned — `prlink.chat_pr_keys`), tests each pair's heads with `git merge-tree
  --write-tree` (PR head SHA from GitHub; worktrees share one object store; lockfiles ignored), and notes a chat's own
  open PR that GitHub already reports as CONFLICTING with its base. **No dashboard UI.** Each affected agent is told
  once per change through its per-message context ("your PR #1 and PR #2 … would conflict — coordinate with
  `loom chats send`, or plan your rebase"). `GET /api/categories/{id}/pr-conflicts` remains for diagnostics.
  `github.pr_status` carries `head_sha`/`mergeable`/`base_branch`, and caches merged PRs for 6 h (they can't change).

## D29 — The PR scout: catching PRs the transcript scraper missed (2026-11-20)
Linking PRs to chats from the transcript (D19) misses PRs whose URL never appeared in tool output. `core/prscout.py`,
run by the dashboard every minute **while you're on a chat** (`chat/usePrScout.ts`; `POST /chats/{id}/pr-scout`, throttled
45 s server-side; `loom chats scan-prs [chat]` by hand), has two tiers, cheapest first:
1. **branch** — free: `gh pr list --head <branch>` for the chat's task / current / transcript branch. Counted if open, or
   created after the chat's task began (an old merged PR on a reused branch name isn't this chat's).
2. **read** — one cheap Haiku call, only when there's something to read: PRs the recent conversation mentions
   (`owner/repo#N`, URLs, `#N`, "PR N") that nothing links yet. GitHub filters candidates first (must exist and be created
   after the task began), Haiku is asked only "did THIS agent open it, or just mention it?", and its yes is re-checked.
   Gates: new candidates only (verdicts remembered), ≥ 3 min between reads, 40 a day per chat. Claude transcripts only.
Found PRs are stored as `prs_scouted` and show as ordinary chips ("found by loom's PR scout"); × removes one for good.
They also count as evidence for `loom chats pr` (D27) and the conflict awareness above.

## D30 — Archive a category (reversible), delete purges its board; PR-number search; create a chat in a category (2026-11-24)
- **Deleting a category used to strand its board.** `categories.remove` only dropped the record and the assignments;
  the posts / cursors / enabled flag in `loom.db` (keyed by the category id) stayed as unreachable orphans, and
  re-creating a category of the same name gets a new id, so they never came back. Now `DELETE /categories/{id}` purges
  the board (`board.purge`) and reports `purged_posts`; the dashboard's confirm names the post count and points to archive.
- **Archive a category** (`POST /categories/{id}/archive`, `loom categories archive`, the ⊘ on its header): every active
  chat in it is archived the normal way (agents stopped, ports freed, in the background) and the category is flagged
  `archived` with the list of chats *that archive* archived (`archived_with`). The category record keeps its id, so the
  **board (posts, enabled flag, unread cursors) and every chat's assignment survive untouched**. The confirm says how many
  chats / running agents it affects and that the board is kept; a toast offers **undo**.
  Archived categories show in the Archived tab as groups ("archived", with ↺). An archived category's board is read-only
  (409 on post/enable) and it's skipped by the PR-conflict check.
- **Restore:** ↺ on the group (`loom categories restore`) un-archives exactly the chats its archive archived — not ones
  archived on purpose earlier. Un-archiving **any one chat** of an archived category restores the category too (hook in
  `PATCH /chats/{id}`): it reappears with just that chat active, its siblings stay archived inside it, the board is writable
  again, and the roster/unread posts reach the agent through the usual per-message context. Un-archiving never starts an agent.
- **Archived tab order (11-26 fix).** The server already sorted it by archive date (`archived_at`, most recent first; starred on top; chats archived before the stamp existed fall back to last activity), but the sidebar then re-sorted every tab by the saved manual slot order, so a chat kept its old position and the Archived tab wasn't by date. The Archived tab now uses the server's order (drag-reorder only applies to the active list). Within a category group the same order applies; groups themselves stay in category order.
- **Search by PR number.** Two bugs: the haystack only held Claude's own `pr-link` numbers (not PRs found by the transcript
  scraper, pinned, or scouted), and the query was matched as one literal string, so `#2140` / `PR 2140` / a PR URL never matched
  the bare number. Now each row's `prs` = pr-link ∪ created (as far as the on-disk createdAt cache knows — never a network call)
  ∪ scouted ∪ pinned − removed, plus `pr_mentions` (PRs the agent only saw); search is term-wise AND, and a PR term (`#N`,
  `owner/repo#N`, `prN`, a PR URL, "PR N") must be exactly that PR. ⌘K applies the same rules (tied outranks mentioned).
- **Create a chat in a category.** `POST /tasks` takes an optional `category` (id or name in the task's project; unknown /
  archived / another project's → 400 before anything is created), and the dashboard's `+ new` form has a category select
  (the ＋ on a category's header preselects it). Filed at creation, so it never flashes in Uncategorized.
- **Categories remain server-side** (`~/.loom/categories.json`, written by the server; the browser's old localStorage copy
  is only a one-time import). Moving them into SQLite was considered and not done: no behaviour change, some migration risk
  for the data that organises every chat. The one real argument for it is atomicity with boards (delete/archive touch both stores).

## D31 — Find a PR by describing it (2026-11-25)
Want: with hundreds of PRs a week and chats archived afterwards, reference "the PR where I added that button last week"
without scrolling GitHub or searching chats. `core/findpr.py`, `POST /api/find-pr` (a background job; poll `GET
/api/find-pr/{job}` for phase + result), the sidebar's "⌕ find a PR" modal, `loom find-pr "<description>"`.
- **Stages** (deterministic wherever possible; ≈3 small Haiku calls, ~10–20 s, ~a cent): (1) *understand* — the description →
  keywords, synonyms and a date range ("last week" → dates); (2) *retrieve* (no model) — the user's PRs from GitHub in that
  window (`gh pr list --author @me`, one per project repo; when the window overflows a page, keyword searches top it up; cached
  5 min so refinements are instant) and loom's chats **including archived** active in the window; PRs and chats are linked by the
  evidence loom already has (PR numbers in the chat, its branch), and each side is pre-scored on the keywords locally; (3) *rank* —
  Haiku over ≤60 PRs + ≤30 chats, one compact line each; (4) *verify* — for the top few, the PR's changed files + fuller
  description (and, for a chat with no PR linked, the PRs on its branch) → final suggestions with a confidence, a one-line why that
  cites evidence, and one question that would narrow it down.
- **Privacy/cost:** only titles, first prompts, cached brief goals, branch names, PR text and changed paths go to the model — never
  transcripts. Empty window → widened once (×3) and says so. If Haiku fails, it falls back to the local keyword ranking and says so.
- **UI:** describe it, optional look-back (auto / 7 / 30 / 90 days / 1 year) and scope (all projects / this one); results show the
  PR, state, date, size, confidence, why, the chat that wrote it (active/archived; "open chat" resumes an archived one), "open PR",
  and "copy reference" (a one-line PR + link + chat to paste into another ask). Answer the narrowing question — or add any detail — to
  refine (the whole conversation is re-sent). Verified with real Haiku on a corpus with distractors (a print button on the same
  page, an older export button, a crash fix): the right PR first each time.
- Limits: PRs are the user's own (`--author @me`) in projects whose repo `gh` can see; a PR older than the window is only found by a
  wider look-back; a chat with no PR linked is suggested on its own text (and its branch's PR, if one exists).

## D32 — Unsent-draft marker in the sidebar (2026-11-25)
Want: if you start typing in a chat and switch away, see that there's an unsent message waiting. loom already forwards every byte into every
agent's terminal (browser keystrokes, pastes, its own `loom chats send`), so `core/draft.py` (`DraftTracker`, pure, unit-tested) keeps a running
picture of the input box without scraping the screen: printable characters grow it, Backspace / Ctrl+W / Alt+Backspace shrink it, Enter outside a
paste, Ctrl+C and Ctrl+U clear it, and escape sequences (arrow keys, mouse and focus reports, paste markers, Alt+key) are skipped whole — so editing
with the arrow keys doesn't lose the draft and a multi-line paste counts. It counts characters (UTF-8 aware) rather than reconstructing the line,
which is why it's separate from the `/clear` guard's line tracker (that one gives up on cursor keys).
Each terminal session owns a tracker; `/api/terminals` gains `draft`, `draft_since`, `draft_preview`; a sidebar row shows a pencil icon (tooltip: the
draft's end and when it started) for a chat that has one — not the chat you're in. Blind spots: a loom restart forgets a draft still in the box; history
recall (↑) isn't counted; text-killing shortcuts it doesn't model (Ctrl+K) can leave a stale marker until the next Enter.

## D33 — Find a *session*: a background-built session index (SQLite) is the first pass (2026-11-26)
D31 searched GitHub PRs and chat titles; the thing you actually remember is the *work* ("the session where finance wanted to download
invoices"), which is in the conversations — most of your 420 chats are archived with meaningless titles. `core/sessindex.py` keeps, per session,
in `loom.db` (`session_index` + an FTS5 table `session_fts`, porter-stemmed, bm25-ranked):
- **facts** (no model, instant): files edited, commit messages (`-m` and heredoc forms), `gh pr create --title`s, your first and last messages — parsed from
  the transcript's tool calls with cheap pre-filters (most of a 100 MB transcript is tool output we never parse). Alone this finds "the session that touched
  ExportButton.tsx" (camelCase file names are indexed as words too).
- **summary** (one cheap Haiku call per session): goal, 2-4 sentences of what was done, keywords *including synonyms*, "done" bullets, outcome — from a
  compressed timeline (head + tail, tool output dropped; same input as the status briefs), through your own `claude` login. Measured: 0.04–0.11¢ per session
  (6 synthetic sessions: $0.0044), so backfilling ~420 chats is well under a dollar.
- **The worker** (`sessindex.start()` in the server lifespan; `LOOM_INDEXER=0` turns it off): facts for 6 sessions per pass, moving on every 3 s while there's a
  backlog; then at most ONE Haiku summary per pass, newest first, 15 s apart, only for a session quiet ≥ 10 min with ≥ 2 user turns; ≤ `LOOM_INDEX_DAILY` (200) a day;
  re-summarized only after the transcript grew ≥ 25% and ≥ 20 KB and 6 h passed; failures retry after 30 min and five in a row back off for an hour; pausable
  (`loom index pause|resume`, or the modal's "pause summaries"). Sessions not indexed yet are still found through their title / first prompt, and the modal
  says how far along the index is. Test servers run with the indexer off so they never summarize your real sessions.
- **Find** (`core/findpr.py`) now: understand → **index first pass** (FTS over all sessions, all time unless you gave a window) + GitHub PRs → rank (candidate lines
  carry the stored goal / summary / keywords / commits / files) → verify, where the finalists' **raw logs are grepped for quotable excerpts** (your messages and
  the agent's prose, never tool output) and shown as evidence. Results are sessions first — the PR it made is attached when there is one; a session with no PR is
  a result on its own. UI: "⌕ find a session"; `loom find "<description>"` (alias `find-pr`); `loom index status|pause|resume|run <chat>`; `/api/index/*`.
- Privacy note: the first backfill sends compressed conversation timelines of your *old* sessions to Haiku (as the status briefs already do for the chats you open);
  pause it or set `LOOM_INDEXER=0` if you don't want that.
- Verified: unit suite (facts, FTS, stemming, worker policies, caps, backoff, regrowth, log excerpts, index-first find), real Haiku on six sessions with meaningless
  titles found from vague descriptions (4/4), and a real-browser run (PR + session, session-only, status line, paused/off states).

## D34 — loom stops what it starts (2026-11-28)
The 11-27 night audit (Kalendir memory `mac-memory-audit`): ~138 GB of demand on a 48 GB Mac, swap full, load 135, and
roughly 40 GB of it was things loom started and never stopped. That included 36 archived chats still running (11 GB), 22
backends of chats idle for a day or more, a Vite for each of 30 spawned perf chats (12.7 GB), and 10 chats running two or
three times at once. Six causes in the code, and the fix for each:
- **One chat, several agents.** `pty_server` forked the agent, then unlinked whatever socket sat at its path and bound its own.
  When two opens of one chat raced (a browser reconnect during a headless `start_chat`, two tabs after a restart), the second
  daemon took the path and the first kept running, unreachable. Archive and close only killed the daemon the pidfile named
  (the newest one), and when a displaced copy exited, its cleanup unlinked the live daemon's socket, so the next open started a
  third. Now each daemon holds an exclusive `flock` on `<socket>.lock` for its whole life, taken **before** it forks the
  agent. A daemon that can't get it exits `EXIT_ALREADY_RUNNING` (3) without starting anything. Cleanup unlinks only the socket
  it bound (by inode) and the pidfile it wrote. `open_terminal` / `start_chat` hold a per-chat asyncio lock, and the launcher
  waits for a daemon that's still coming up instead of replacing it. `kill_session` stops every daemon for the socket, found
  by argv (`pgrep`, then `ps -p`), not only the pidfile's. The reaper stops leftover copies from before the lock: copies older
  than 60 s that the pidfile doesn't name, and unreachable ones (socket file gone) only once the chat isn't mid-turn.
  The lock file is never unlinked: deleting a lock file while someone may have it open is how two processes both end up
  "holding" it. A liveness probe takes `LOCK_SH` for microseconds, so a starting daemon retries `LOCK_EX` for up to 2 s.
- **macOS pid reuse.** With no `/proc`, `is_session_alive` treated any live pid as the daemon. A pidfile left by an August
  daemon now named `ExtensibleSSOSubscriber`, so that archived chat read as running, and closing it would have SIGTERMed that
  system process's group. Now a daemon is alive if it holds the lock, or, for a daemon that predates the lock, if the pidfile's
  pid has this socket's daemon command line. The command line is matched on argv (`<python> -m loom.core.pty_server <socket>`),
  never as a substring: an agent's shell that greps `ps` must never be taken for a daemon. The verdict is cached for 5 min,
  because `ps` takes 25–100 ms per call on a loaded Mac and 1–3 s for all processes. Once every pre-lock daemon has been
  relaunched, liveness is a lock probe alone.
- **Archive didn't stick.** Besides the duplicates above, three things went wrong. An agent archiving itself (rightly) isn't
  killed mid-turn, and nothing came back for it later. Anything that relaunched an archived chat (`loom chats send|ask`, a
  notification reply, board `--wake`, opening it from the archived tab) left it running for good. And
  `stop_for_archived_chat` and the reaper both skipped any worktree with a live session. Now the reaper enforces
  "archived ⇒ stopped". Once an archived chat is quiet (no tab shows it, it isn't mid-turn, and nothing has happened for
  2 min), the reaper stops its agent, parks its stack and releases its port offset. That catches all of these cases.
- **Stacks of idle chats.** The supervisor counted every attached session as active, and since agents are never stopped,
  that meant forever. It also restarted those stacks, and the reaper skipped any worktree with a live session. Now a
  chat is **in use** while a tab shows it, while it's mid-turn (claude's hooks; recent output for grok/codex, which have none;
  claude's own output proves nothing since its status line redraws itself), or for `LOOM_IDLE_STOP_MIN` (default 30) after its
  last sign of life: a turn event, a keystroke, a tab leaving it, a transcript write, a launch. The supervisor keeps
  *wanted* services up only while the chat is in use, and starts missing ones at once (the down-streak is only for
  restarts). The reaper (now every 60 s, was 10 min) **parks** an idle stack (`manager.park_task`): stopped, state `ready`,
  so the supervisor restarts it by itself the next time the chat is used. A deliberate stop (button, `loom stop`,
  `loom dev down`) still lands on `stopped` and stays down. A parked stack shows "○ idle" in the dev-stack strip. An idle
  **agent** is never stopped; only an archived one is. Some idle-looking chats do real work, such as session crons.
- **A Vite per spawned chat.** Chats an agent spawned (`spawned_by`) get every service except `frontend`, unless someone
  asks for it. Asking means the dev-stack start button, `loom dev up frontend` or `loom start`, recorded in
  `task.requested` and cleared when the stack is parked or stopped. A spawned chat's frontend that nobody asked for is
  stopped, but only once it isn't mid-turn or open in a tab. New: `loom dev status|up|down` (`/api/chats/{id}/dev`,
  `…/dev/up`, `…/dev/down`). `up` waits until the services answer, and leaves one that's still booting alone. Agents
  learn this from `<loom-runtime>`.
- **One spawn wave, 30 chats at once.** A spawner now has at most `LOOM_SPAWN_MAX_WORKING` (default 4) children working:
  mid-turn, or launched less than 3 min ago with no turn event yet. A spawn past that still creates the worktree and the
  chat, but its handoff waits in the overlay (`queued_at` / `queued_prompt`, `core/spawnq.py`). The sidebar shows it as
  "queued", `spawn` prints its place, and the monitor loop launches queued chats oldest-first as slots free up. Opening a
  queued chat, or `loom chats send` to it, starts it right away, on its handoff (consumed in `open_terminal`).

Verified: real-process tests of the lock, liveness and kill paths (a second daemon bows out without starting its command,
a stale pidfile on a live unrelated pid is neither alive nor killed, a displaced copy is found and stopped by argv, a decoy
shell is ignored, cleanup leaves foreign files alone). Decision tests on a constructed world (reaper plan, supervisor wants
and starts, spawn-queue slots). Two end-to-end runs on an isolated instance: the cap and queue, backend-only for spawned
chats, `dev up frontend`, three concurrent starts giving one daemon, idle park then auto-restart, outside and self-archive,
and a **pre-lock daemon** displacing a chat's daemon, then reaped. A read-only dry run against the live state would park 32
idle stacks, stop 29 spawned chats' Vites and release 97 archived tasks' port offsets.
(Supersedes D14's 12 h / live-session policy.)

## Non-goals
Rebuilding the agent/session-orchestration commodity layer; running a target repo's
DB migrations (human-only); remote/multi-machine (local-only); per-worktree dev DBs (D2).
