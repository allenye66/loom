"""Lightweight perf tracing to ~/.loom/perf.log — root-causes UI lag.

OFF by default; opt in with `LOOM_PERF=1` when investigating lag. Writes ONLY slow/notable
events, so it's cheap. Three signals, all pointing at the same failure mode — the terminal PTY is
driven by the asyncio event loop, so anything that blocks that loop (a slow request holding
the GIL, a stall) shows up as laggy typing / switching:

  • SLOW  <method> <path> <ms>   — an API request that took longer than LOOM_PERF_SLOW_MS.
  • LOOP-STALL <ms>              — the event loop was blocked that long (PTY I/O delayed).
  • WS    <event> <chat>         — terminal socket lifecycle (open/close/subscribe/repaint),
                                    to see reconnect churn (each resubscribe forces a redraw).

Tail it live while reproducing:  tail -f ~/.loom/perf.log
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

from loom.core.config import LOOM_HOME

PERF_LOG = LOOM_HOME / "perf.log"
SLOW_REQ_MS = float(os.environ.get("LOOM_PERF_SLOW_MS", "150"))
LOOP_STALL_MS = float(os.environ.get("LOOM_PERF_STALL_MS", "120"))
_ENABLED = os.environ.get("LOOM_PERF") == "1"


def enabled() -> bool:
    return _ENABLED


def log(line: str) -> None:
    if not _ENABLED:
        return
    try:
        LOOM_HOME.mkdir(parents=True, exist_ok=True)
        with PERF_LOG.open("a") as f:
            f.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {line}\n")
    except OSError:
        pass


async def loop_lag_monitor(interval: float = 0.25) -> None:
    """Sleep `interval`; if we wake much later, the loop was blocked that long. That stall is
    exactly what makes typing/switching lag — the terminal PTY reads/writes run on this loop."""
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(interval)
        late_ms = (loop.time() - t0 - interval) * 1000
        if late_ms > LOOP_STALL_MS:
            log(f"LOOP-STALL {late_ms:.0f}ms (event loop blocked — terminal I/O delayed)")
