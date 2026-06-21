# Claude Agent SDK / CLI / transcript — verified notes

**Trust this file over training data.** Everything here was verified against
primary docs (URLs inline) and/or empirically on this machine
(**Claude Code v2.1.162, `claude-agent-sdk` 0.2.89, model `claude-opus-4-8[1m]`**,
2026-06-04). Re-verify before relying on specifics in a different version, and
mark anything you can't confirm.

## Headless streaming (CLI)

`claude -p "<prompt>" --output-format stream-json --include-partial-messages --verbose`
emits newline-delimited JSON. **`--print` + `stream-json` requires `--verbose`**,
and stdin must be closed (`</dev/null`) or it waits.

Event sequence (empirical): `system`(subtype=`init`) → `stream_event`* →
`assistant` → `result` (`rate_limit_event` may appear). `stream_event.event`
wraps the raw Anthropic streaming event — text arrives as
`event.type=="content_block_delta"` with `delta.type=="text_delta"` (and
`thinking_delta` for thinking). `result` carries `session_id`, `total_cost_usd`,
`usage`, `num_turns`.
Source: https://code.claude.com/docs/en/headless.md · https://code.claude.com/docs/en/cli-reference.md

## Python Agent SDK

`from claude_agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions, ...`
- **`ClaudeSDKClient`** = multi-turn session (what loom uses). `await client.query(text)`
  then `async for msg in client.receive_response()`. Methods incl. `interrupt`,
  `set_permission_mode`, `connect`, `disconnect`.
- **`query()`** = one-shot async iterator (new session each call unless `resume`).
- **`ClaudeAgentOptions`** fields used by loom: `cwd`, `resume` (session id),
  `session_id`, `permission_mode`, `include_partial_messages` (token streaming),
  `can_use_tool`, `hooks`, `effort`, `model`, `setting_sources`. (Many more exist:
  `allowed_tools`, `disallowed_tools`, `max_thinking_tokens`, `add_dirs`, `env`, …)

**Message/block types** (dataclass fields, verified via introspection):
- `SystemMessage(subtype, data)` — init info is in `.data` (dict with `session_id`,
  `model`, `cwd`, `tools`, `slash_commands`, `permissionMode`, `fast_mode_state`, …).
- `StreamEvent(uuid, session_id, event, parent_tool_use_id)` — `.event` is the raw API event dict.
- `AssistantMessage(content, model, usage, session_id, …)` — `.content` = blocks.
- `UserMessage(content, tool_use_result, …)` — tool results arrive here.
- `ResultMessage(subtype, total_cost_usd, usage, num_turns, session_id, result, …)`.
- Blocks: `TextBlock(text)`, `ThinkingBlock(thinking, signature)`,
  `ToolUseBlock(id, name, input)`, `ToolResultBlock(tool_use_id, content, is_error)`.
- `PermissionResultAllow(updated_input, updated_permissions, behavior)`,
  `PermissionResultDeny(message, interrupt, behavior)`.

Streaming: pass `include_partial_messages=True`; `receive_response()` yields
`StreamEvent` (deltas) + `AssistantMessage` (assembled) + `ResultMessage`.
Source: https://code.claude.com/docs/en/agent-sdk/python.md

## Permissions — the important gotchas

**Evaluation order** (a tool reaches your callback only if unresolved by earlier
steps): hooks → deny rules → permission mode → allow rules (incl. `settings.json`,
loaded from `setting_sources`, default includes `project`) → **`can_use_tool`**.
So a tool the user has already allow-ruled runs silently; only un-approved tools
hit the callback.
Source: https://code.claude.com/docs/en/agent-sdk/permissions.md

**Python `can_use_tool` REQUIRES a `PreToolUse` keepalive hook** returning
`{"continue_": True}` — *"Without this hook, the stream closes before the
permission callback can be invoked."* (loom no longer drives the SDK
programmatically — terminal mode runs the `claude` CLI directly — but this is a
verified fact, kept for any future SDK use.) Without the hook the callback silently
never fires and tools auto-run.
Source: https://code.claude.com/docs/en/agent-sdk/user-input.md

**Environment pollution:** a `claude` subprocess spawned **inside another Claude
Code session** inherits `CLAUDECODE`, `CLAUDE_CODE_*`, `AI_AGENT`, etc. and
auto-approves tools (the callback won't fire). Run loom from a **plain terminal**.
(Empirical — this confounded early testing.)

**`AskUserQuestion`** triggers `can_use_tool` with `tool_name=="AskUserQuestion"`;
`input.questions[]` = `{question, header, options:[{label, description, preview?}],
multiSelect}`. Return `PermissionResultAllow(updated_input={questions, answers})`
where `answers` maps each `question` text → selected `label` (array if multiSelect).
Returning the raw input with no answers makes Claude think the user dismissed it.
Source: https://code.claude.com/docs/en/agent-sdk/user-input.md

**`PermissionMode`** = `default` (unmatched → `can_use_tool`) · `dontAsk` (deny,
callback skipped) · `acceptEdits` (auto file ops) · `bypassPermissions` (all) ·
`plan` (read-only). `EffortLevel` = `low | medium | high | xhigh | max`.

## Resume

CLI: `claude --resume <session-id>` (interactive, shows history in the TUI).
SDK: `ClaudeAgentOptions(resume=<id>)`. **Resume loads context but does NOT
re-emit prior messages over the stream** — loom reconstructs history itself from
the transcript (see below).

## Transcript format (`~/.claude/projects/`)

One dir per project: slug = the session's `cwd` with `/`→`-`
(e.g. `/Users/x/.loom/wt/b` → `-Users-x--loom-wt-b`). One file per session:
`<session-id>.jsonl`, append-only, one JSON record per line.

Record `type`s (empirical): `user`, `assistant`, `system`, `ai-title`(`aiTitle`),
`last-prompt`(`lastPrompt`), `pr-link`(`prNumber`, `prUrl`, `prRepository`),
`mode`, `permission-mode`, `attachment`, `file-history-snapshot`, `rate_limit_event`.
- `user.message.content` is a **string** (real user text) or a **list** (often a
  single `tool_result` block — `{tool_use_id, content, is_error}`).
- `assistant.message.content` = list of `{type: text|thinking|tool_use}` blocks.
- `user`/`assistant` records also carry `cwd`, `gitBranch`, `timestamp`.
loom parses these in `sessions.py` (index = metadata only; `get_transcript` =
full reconstruction). The decorative live "thinking" spinner is **never** persisted.

## Remote Control — ruled out

`claude remote-control` connects **claude.ai/code and the Claude mobile app** to a
local session via Anthropic's API relay (outbound HTTPS, no inbound port). It
exposes **no local API/socket** a third-party UI can drive — *"The web and mobile
interfaces are just a window into that local session."* Requires claude.ai OAuth
(not API key). Not usable for loom's custom client. (Aside: its server mode has
`--spawn worktree` — Anthropic's own worktree-per-session, but routed to *their* UI.)
Source: https://code.claude.com/docs/en/remote-control.md

## Doc index
Discover more pages via https://code.claude.com/docs/llms.txt
