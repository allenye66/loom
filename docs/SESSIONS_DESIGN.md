# Chat / Session Management — Design

Goal: a clean, fast, **keyboard-first** UI to organize Claude Code chats — star,
archive, hide, delete, tag (branch / PR / name / description), search — so
`/resume` and `/find-session` are never needed again.

## On-disk reality (verified on this machine, 2026-05-31)

- Chats live at `~/.claude/projects/<cwd-slug>/<session-id>.jsonl` — append-only
  transcripts, one file per session (a busy machine accumulates hundreds).
- Each transcript already contains, as record types:
  - `ai-title` → `aiTitle` (human-readable title)
  - `last-prompt` → `lastPrompt` (latest prompt; great 1-line preview)
  - `user` / `assistant` messages, each carrying `cwd`, `gitBranch`, `timestamp`
  - `pr-link` records (session ↔ PR association)
- Global prompt history: `~/.claude/history.jsonl` (`display, project, sessionId, timestamp`).
- Resume (from `claude --help`): `-r, --resume <id>` (interactive), `--fork-session`
  (branch a chat into a new id), `-n, --name <name>` (display name), `--from-pr`,
  `-c, --continue` (most recent).

## Principle

Claude's transcripts are a **read-only source of truth**. loom never edits them
(soft-delete only = move the file to a trash dir). loom owns a thin **overlay**
for the user-controlled bits Claude doesn't track (star/archive/hide/custom
name/tags/description). So loom is mostly a smart *index + organizer*, not a new store.

## Mapping chats → worktrees (no fragile slug math)

loom enumerates `~/.claude/projects/*/*.jsonl` and reads each session's recorded
`cwd` to map it to a repo/worktree — it does **not** try to derive the project-dir
slug from a path. Since loom already knows every worktree path (the task registry),
a chat with `cwd = ~/.loom/worktrees/<repo>/<branch>` links straight to that task,
and `cwd = <repo root>` links to the main checkout. A repo's chats = the
union across the main checkout + all its worktrees.

## Data

- **Index** (derived, cached — `~/.loom/index.sqlite`, FTS5): per session →
  `{id, cwd, repo, task, title=aiTitle, preview=lastPrompt, first_prompt, branch,
  prs[], created, last_active=mtime, n_user, n_assistant}`. Reparse a file only when
  its mtime changes → fast even at 164+ files. Reading is cheap: scan for the last
  `ai-title` / `last-prompt`, first user message, and a sampling of `gitBranch`.
- **Overlay** (user-owned — same sqlite or `~/.loom/chats.json`): per session →
  `{starred, archived, hidden, name, tags[], description, pr, deleted, deleted_at}`.
  This is the "save to local disk" piece — it's just a local file.

## UI (the "insanely clean" bar)

- A top-level **Chats** view, plus a "Chats (N)" strip on each task card.
- Tabs: **Active** · **Archived** · **Trash**. A pinned **★ Starred** group sits at
  the top of Active. Hidden chats are filtered from Active (toggle to reveal).
- Row = title (custom name or `aiTitle`) · branch chip · PR chip (→ GitHub) ·
  last-active · msg count · 1-line preview (`lastPrompt`). Hover/▸ reveals actions.
- **⌘K command palette / search** replaces `/find-session`: full-text over
  title + name + tags + branch + PR + description (+ optional message content).
  Filters: branch, PR, repo/worktree, starred-only, date.
- Keyboard-first: `j/k` move, `o` open, `s` star, `e` archive, `x` hide, `t` tag,
  `⌫` delete, `/` or `⌘K` search. No modal clutter; inline rename; optimistic updates.
- Live: poll the project dirs (~2s) or use a watcher; the running chat shows "● live".

## Actions → mechanism

| Action | How |
|---|---|
| Open / Resume | `claude --resume <id>` in the chat's `cwd` (the Terminal/tmux launcher loom already has) |
| Branch a chat | `claude --resume <id> --fork-session` |
| Star / Archive / Hide | overlay flag toggle (instant, local) |
| Tag branch / PR | auto-derived from the transcript; editable in the overlay |
| Rename / Describe / extra tags | overlay edit (optionally also pushed to Claude via `-n` on next launch) |
| Delete | soft: move `<id>.jsonl` → `~/.loom/trash/` + overlay flag; "empty trash" = hard delete; undo supported |

## API (additive)

```
GET    /api/chats?repo=&tab=&q=&branch=&pr=&starred=    # indexed + overlay-merged list
PATCH  /api/chats/{id}    {starred?, archived?, hidden?, name?, tags?, description?, pr?}
DELETE /api/chats/{id}                                  # -> trash
POST   /api/chats/{id}/restore
POST   /api/chats/reindex                               # force a rescan
```

## Decisions

- **Search depth:** metadata + prompts (light, private, fast) — default — vs index
  full message content (powerful, larger index).
- **Delete:** soft-trash with undo (default) vs hard delete.
- **Scope:** per-repo view vs an all-repos global inbox (loom is multi-repo).
- **Custom name:** overlay-only vs also set Claude's native `-n` name.

## Why this is independent of any target repo

This feature only reads `~/.claude/projects/**` and writes loom's own overlay — it
needs **zero** changes to any target repo. It's fully repo-agnostic, riding on the
worktree/test core.
