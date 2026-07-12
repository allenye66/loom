"""Per-task service / test log files under ~/.loom/logs/.

Each service started by loom writes stdout+stderr to
`{task_id}-{service}.log` (see process.spawn). Test runs use `{task_id}-test.log`.

Helpers here never load whole multi‑MB files into memory — they seek from the end
for tails and track a byte offset for live follow.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from loom.core.config import LOGS_DIR

# Cap how much we ship on an initial tail (bytes read from end of file).
_DEFAULT_MAX_BYTES = 256 * 1024  # 256 KiB
_DEFAULT_MAX_LINES = 2000

# kind must be a simple slug — no path traversal via ".." / slashes.
_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def log_path(task_id: str, kind: str) -> Path:
    if not _KIND_RE.match(kind):
        raise ValueError(f"invalid log kind: {kind!r}")
    if not _KIND_RE.match(task_id):
        raise ValueError(f"invalid task id: {task_id!r}")
    return LOGS_DIR / f"{task_id}-{kind}.log"


def _stat(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "size": 0, "mtime": None, "path": str(path)}
    st = path.stat()
    return {
        "exists": True,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "path": str(path),
    }


def list_kinds(task_id: str, service_names: list[str] | None = None) -> list[dict]:
    """Known log streams for a task: configured services + any extra files on disk + test.

    Prefer the configured service order (backend, frontend, …), then leftover files,
    then `test` last.
    """
    seen: set[str] = set()
    kinds: list[dict] = []

    def add(kind: str, *, source: str) -> None:
        if kind in seen:
            return
        seen.add(kind)
        meta = _stat(log_path(task_id, kind))
        kinds.append({"kind": kind, "source": source, **meta})

    for name in service_names or []:
        if _KIND_RE.match(name):
            add(name, source="service")

    # Files already on disk that aren't in the configured service list (legacy / ad-hoc).
    prefix = f"{task_id}-"
    if LOGS_DIR.exists():
        for p in sorted(LOGS_DIR.glob(f"{task_id}-*.log")):
            kind = p.name[len(prefix) : -len(".log")]
            if kind and kind != "test" and _KIND_RE.match(kind):
                add(kind, source="file")

    add("test", source="test")
    return kinds


def tail(
    task_id: str,
    kind: str,
    *,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_lines: int = _DEFAULT_MAX_LINES,
) -> dict:
    """Efficient end-of-file tail. Returns text + byte offset (file size after read)."""
    path = log_path(task_id, kind)
    meta = _stat(path)
    if not meta["exists"] or meta["size"] == 0:
        return {"log": "", "offset": 0, "truncated": False, **meta}

    size = int(meta["size"])
    max_bytes = max(1024, min(int(max_bytes), 2 * 1024 * 1024))  # 1 KiB .. 2 MiB
    max_lines = max(50, min(int(max_lines), 10_000))

    with open(path, "rb") as f:
        if size <= max_bytes:
            data = f.read()
            truncated = False
        else:
            f.seek(size - max_bytes)
            data = f.read()
            # Drop the partial first line so we never start mid-line.
            nl = data.find(b"\n")
            if nl >= 0:
                data = data[nl + 1 :]
            truncated = True

    text = data.decode("utf-8", errors="replace")
    # Normalize newlines; keep trailing content without forcing a final newline.
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return {
        "log": "\n".join(lines),
        "offset": size,  # next read starts here for follow
        "truncated": truncated,
        **meta,
    }


def read_since(task_id: str, kind: str, offset: int, *, max_bytes: int = 512 * 1024) -> dict:
    """Read new bytes since `offset`. Handles truncation (file shrank) by re-tailing."""
    path = log_path(task_id, kind)
    meta = _stat(path)
    if not meta["exists"]:
        return {"log": "", "offset": 0, "reset": True, **meta}

    size = int(meta["size"])
    offset = max(0, int(offset))

    # Cleared / rotated / truncated under us — restart from a fresh tail.
    if offset > size:
        t = tail(task_id, kind)
        return {
            "log": t["log"],
            "offset": t["offset"],
            "reset": True,
            "truncated": t.get("truncated", False),
            **meta,
        }

    if offset == size:
        return {"log": "", "offset": size, "reset": False, **meta}

    max_bytes = max(1024, min(int(max_bytes), 2 * 1024 * 1024))
    to_read = min(size - offset, max_bytes)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(to_read)

    text = data.decode("utf-8", errors="replace")
    return {
        "log": text,
        "offset": offset + len(data),
        "reset": False,
        **meta,
    }


def clear(task_id: str, kind: str) -> dict:
    """Truncate the log file (create empty if missing). Services keep writing to the same fd."""
    path = log_path(task_id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open truncating — if a service still has the old fd open, its next write may
    # reappear at the old offset on some FS; on macOS/Linux with O_APPEND (we use "ab")
    # the write still goes to EOF of the open file description. Truncating via open("wb")
    # zeroes the inode size; append-mode writers will continue at the new EOF after
    # the next write on Linux; on macOS with shared inode it works for append mode.
    # Safest portable approach used by many tools: open with O_TRUNC.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.close(fd)
    return _stat(path)
