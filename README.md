# loom

**Parallel git-worktree dev orchestrator for Claude Code.** Run many PRs/tasks at
once — each on its own worktree/branch, each independently testable — so a skill
like `/address-comments` on PR B never blocks because another chat has you checked
out on branch A.

> A personal tool, shared as-is — opinionated, no support guarantees. Adapt it to
> your stack with a per-repo `.loom.yaml` (see `projects/example.loom.yaml`).

## Why

A single working tree is a global singleton: one branch, one dirty state, one set
of dev servers on fixed ports. loom turns each **task** into an isolated worktree
with deterministic ports, a per-worktree env, and isolated test runs — and lets you
pop a Claude Code session straight into it.

It's a **hybrid**: adopt an existing tool (or native Claude Code) for the *session*
layer; loom owns the *runnable-stack-per-worktree* layer that nothing else does.

## Install

```bash
# prerequisites: git, uv (pip install uv), node
uv tool install --from . loom     # or: uv sync && uv run loom ...
loom doctor                       # preflight check
```

## Quickstart

```bash
# 1. register your repo — any git repo works. Optionally add a .loom.yaml first to give
#    loom its dev services / tests (see projects/example.loom.yaml — a Flask + JS app)
cp projects/example.loom.yaml /path/to/your-repo/.loom.yaml   # optional, then edit it
loom repo-add /path/to/your-repo                               # or "+ add project…" in the dashboard

# 2. launch the dashboard
loom serve                        # opens http://127.0.0.1:8787

# …or drive it from the CLI:
loom new my-feature -r /path/to/your-repo
loom test my-feature tests/test_thing.py
loom claude my-feature "/address-comments"
loom ls
loom rm my-feature
```

## How isolation works (Phase 1)

| Concern | Approach |
|---|---|
| Code / branch | one `git worktree` per task |
| Ports | deterministic `hash(branch)` offset, collision-checked (`<base>+o`) |
| Tests | each worktree runs its suite independently; `serialize` lock so concurrent runs don't clash on a shared test resource (e.g. one test DB) |
| node_modules | symlinked from the main checkout (seconds, not an `npm ci`) |

## Architecture

```
loom/
  cli.py            # Typer CLI (doctor/serve/repo-add/new/ls/test/start/stop/claude/rm)
  server/           # FastAPI: /api/* + /api/ws/term + serves the built dashboard
  core/
    registry.py     # atomic JSON task registry + state machine
    ports.py        # hash-offset, collision-checked per-worktree port allocation
    worktree.py     # git worktree create/remove/status
    process.py      # process-group spawn / health / port-scoped teardown
    manager.py      # task lifecycle (create/start/stop/remove)
    tests.py        # isolated test runs
    monitor.py      # dev-stack supervisor + reaper
    sessions.py     # chat index (~/.claude, ~/.grok, ~/.codex) + local overlay
    agents.py       # claude / grok / codex launch adapters
    terminals.py    # the in-browser agent terminal (pty host)
    pty_server.py   # detached PTY daemon that outlives loom restarts
    doctor.py       # preflight checks
dashboard/          # React + Vite + Tailwind (bun): chat sidebar + live terminal
projects/           # reference .loom.yaml configs
```

## Status

Working: worktree tasks with isolated ports + test runs; per-worktree dev stacks
(start/stop from each chat, kept up while the chat is open); and the in-browser agent
terminal (claude / grok / codex) with the chat sidebar. Not built: a live preview of a
worktree's app inside the dashboard. See `docs/ARCHITECTURE.md` § Roadmap.
