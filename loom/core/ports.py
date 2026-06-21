"""Deterministic, collision-checked per-worktree port allocation.

`hash(branch) -> stable offset` so the same branch keeps the same ports across
restarts, with linear probing to dodge offsets already taken or ports in use.
A repo that needs other per-worktree indices (e.g. a DB number) can derive them
from `{offset}` in its `.loom.yaml`.
"""

from __future__ import annotations

import hashlib
import socket

from loom.models import Ports

PORT_RANGE = 90  # usable offsets: 1..90


def _hash_offset(slug: str) -> int:
    h = int(hashlib.sha1(slug.encode()).hexdigest(), 16)
    return (h % PORT_RANGE) + 1


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def allocate(
    slug: str,
    backend_base: int = 8000,
    frontend_base: int = 3000,
    taken_offsets: set[int] | None = None,
) -> Ports:
    taken = taken_offsets or set()
    start = _hash_offset(slug)
    for i in range(PORT_RANGE):
        offset = ((start - 1 + i) % PORT_RANGE) + 1
        if offset in taken:
            continue
        backend, frontend = backend_base + offset, frontend_base + offset
        if is_free(backend) and is_free(frontend):
            return Ports(offset=offset, backend=backend, frontend=frontend)
    raise RuntimeError("No free port offset available (too many active worktrees?)")
