"""Pydantic models shared across the CLI, server, and registry."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TaskState(str, Enum):
    created = "created"      # registry entry exists, worktree not ready yet
    ready = "ready"          # worktree created + setup run; no servers running
    running = "running"      # at least one service process is up
    stopped = "stopped"      # servers torn down, worktree kept
    archived = "archived"    # worktree removed
    error = "error"          # setup/start failed (see .note)


class Ports(BaseModel):
    """Deterministic, collision-checked per-worktree port allocation."""

    offset: int
    backend: int
    frontend: int


class ServiceProc(BaseModel):
    name: str
    pid: int | None = None
    port: int | None = None
    healthy: bool = False
    health_url: str | None = None


class Task(BaseModel):
    """One worktree/branch and its isolated stack. `id` is the branch slug."""

    id: str
    repo: str
    repo_root: str
    branch: str
    base_branch: str
    worktree_path: str
    state: TaskState = TaskState.created
    ports: Ports | None = None
    services: list[ServiceProc] = Field(default_factory=list)
    created_at: str
    updated_at: str
    pr: int | None = None
    note: str | None = None
    chat_id: str | None = None  # the task's single linked chat (session id) — strict 1:1
