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

# mtime/size-keyed cache of the parsed registry. `list_tasks()`/`get_task()` are called on
# hot, per-request (and previously per-chat) paths; re-reading + re-parsing registry.json on
# every call showed up as GIL-bound CPU that starved the terminal's event loop. Reads reuse
# this until the file actually changes; writers invalidate it.
_cache: tuple[tuple[float, int], dict[str, dict]] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> dict[str, dict]:
    global _cache
    try:
        st = REGISTRY_PATH.stat()
    except OSError:
        return {}
    key = (st.st_mtime, st.st_size)
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    try:
        data = json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    _cache = (key, data)
    return data


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
    global _cache
    with _lock:
        data = dict(_read())  # shallow copy → never mutate the cached dict in place
        task.updated_at = now_iso()
        data[task.id] = task.model_dump()
        _write(data)
        _cache = None  # next _read() reloads from the file we just wrote
    return task


def delete(task_id: str) -> None:
    global _cache
    with _lock:
        data = dict(_read())
        if task_id in data:
            del data[task_id]
            _write(data)
            _cache = None
