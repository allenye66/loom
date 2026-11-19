"""Only answer requests that really come from this machine's own pages and tools.

loom's API can type into agents that run with bypassed permissions (`/chats/{id}/send`, the terminal
WebSocket, `/shell`), so "any web page open in your browser can call it" is a real exposure. The
server binds to loopback, which keeps other machines out, but a browser on this machine will happily
send requests to it from *any* site, and CORS `*` let those pages read the answers. Two checks close
the common routes:

- **Host** must be a loopback name. A DNS-rebinding attack points an attacker's domain at 127.0.0.1;
  the browser then talks to loom believing it is the attacker's origin, but the Host header still says
  the attacker's name — so it's refused.
- **Origin**, when the browser sends one (every cross-origin fetch, every WebSocket handshake), must be
  a loopback origin on any port — the dashboard itself, `vite` dev servers (5173, per-worktree 3xxx),
  your own localhost apps. Foreign sites and `Origin: null` (sandboxed frames, `file://`) are refused,
  and CORS headers are only ever returned to loopback origins.

Requests with no Origin (the `loom` CLI, `curl`, agents' hooks) pass — they aren't browsers.

Not covered: a page served from *another localhost port* (one of your own dev servers with an XSS hole
or a malicious dependency) is a loopback origin and still allowed; closing that needs a secret the
dashboard and CLI share (a per-session token). If you deliberately bind loom to a non-loopback address
(`LOOM_API_HOST=0.0.0.0`, for remote use) these checks switch off — you've opted into the network.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

LOCAL_BINDS = {"localhost", "127.0.0.1", "::1"}  # listening here = loopback-only → the guard is on
# Names a page on this machine can be reached by. `0.0.0.0` is included because browsers on macOS open it
# (it can't be produced by a rebinding attacker: Host mirrors the attacker's own domain).
LOOPBACK = LOCAL_BINDS | {"0.0.0.0"}
CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?$"


def extra_hosts() -> set[str]:
    """Names the user added on purpose (`LOOM_ALLOWED_HOSTS=my-mac.tailnet.ts.net,…`)."""
    return {h.strip().lower() for h in os.environ.get("LOOM_ALLOWED_HOSTS", "").split(",") if h.strip()}


def strict(bind_host: str) -> bool:
    """Enforce only when loom listens on loopback (see the module note for the opt-out)."""
    return bind_host.strip().lower() in LOCAL_BINDS


def _host_name(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):  # [::1]:8787
        return h[1:].split("]", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _origin_host(origin: str) -> str | None:
    try:
        return (urlsplit(origin).hostname or "").lower() or None
    except ValueError:
        return None


class LoopbackOnlyMiddleware:
    """Pure-ASGI so it covers HTTP *and* WebSocket handshakes, and sits outside CORS (a refused
    request must not get CORS headers)."""

    def __init__(self, app, bind_host: str = "127.0.0.1") -> None:
        self.app = app
        self.on = strict(bind_host)
        self.allowed = LOOPBACK | extra_hosts()

    async def __call__(self, scope, receive, send):
        if self.on and scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            why = None
            if _host_name(headers.get("host", "")) not in self.allowed:
                why = f"host {headers.get('host', '')!r} isn't this machine"
            elif "origin" in headers and _origin_host(headers["origin"]) not in self.allowed:
                why = f"origin {headers['origin']!r} isn't a local page"
            if why:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    body = json.dumps({"detail": f"refused: {why} (loom only answers pages and tools on this machine)"}).encode()
                    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                    await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
