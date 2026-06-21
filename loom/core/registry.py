"""Persistent JSON task registry with atomic writes.

Survives restarts; tempfile + os.replace makes every write atomic so a crash
mid-write can never corrupt the registry.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone

from loom.core.config import REGISTRY_PATH, ensure_dirs
from loom.models import Task

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, dict]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict[str, dict]) -> None:
    ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=str(REGISTRY_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, REGISTRY_PATH)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_tasks() -> list[Task]:
    return [Task(**v) for v in _read().values()]


def get_task(task_id: str) -> Task | None:
    raw = _read().get(task_id)
    return Task(**raw) if raw else None


def upsert(task: Task) -> Task:
    with _lock:
        data = _read()
        task.updated_at = now_iso()
        data[task.id] = task.model_dump()
        _write(data)
    return task


def delete(task_id: str) -> None:
    with _lock:
        data = _read()
        if task_id in data:
            del data[task_id]
            _write(data)
