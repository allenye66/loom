"""Process-group spawn / liveness / teardown + health checks.

Each service runs in its own process group (start_new_session=True) so we can
kill the whole tree (autoreload forks, Vite workers) with one signal, and we
only ever clean up by *this task's* ports — never a blanket pkill.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import httpx


def spawn(command: str, cwd: str, env: dict[str, str], log_path: str) -> int:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    full_env = {**os.environ, **env}
    logf = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=full_env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group
    )
    return proc.pid


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group(pid: int, timeout: float = 5.0) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_port(port: int) -> None:
    """Port-scoped cleanup: kill whatever owns this port (and only this port)."""
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    for pid in out.stdout.split():
        try:
            kill_group(int(pid))
        except (ValueError, ProcessLookupError):
            pass


def health_check(url: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(url, timeout=timeout, follow_redirects=True).status_code < 500
    except Exception:
        return False
