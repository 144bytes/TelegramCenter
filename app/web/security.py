"""Access control for the local API.

Four independent layers, so that no single mistake exposes the backend:

  1. the socket is bound to 127.0.0.1 — nothing on the network can reach it;
  2. every /api/ request must carry the runtime token in `X-TC-Token`;
  3. a request whose `Origin` is set and is not ours is rejected outright;
  4. everything that changes state is POST-only, so a stray <img> or <form>
     on some other page cannot trigger it.

Requiring a custom header is what makes (3) effective: a cross-origin fetch
carrying `X-TC-Token` is not a "simple request", so the browser must preflight
it, and we refuse the preflight.
"""
from __future__ import annotations

import secrets

TOKEN_HEADER = "X-TC-Token"


class Guard:
    def __init__(self, host: str, port: int) -> None:
        self.token = secrets.token_urlsafe(32)
        self.origins = {f"http://{host}:{port}", f"http://localhost:{port}"}

    def check(self, headers, method: str, path: str) -> tuple[bool, str]:
        """Return (allowed, reason)."""
        origin = headers.get("Origin")
        if origin and origin not in self.origins:
            return False, "origin not allowed"

        if not path.startswith("/api/"):
            return True, ""

        supplied = headers.get(TOKEN_HEADER, "")
        if not supplied or not secrets.compare_digest(supplied, self.token):
            return False, "bad or missing token"

        if method not in ("GET", "POST"):
            return False, "method not allowed"
        return True, ""

    @staticmethod
    def is_mutation(path: str) -> bool:
        return path.startswith("/api/") and not path.startswith("/api/state") \
            and not path.startswith("/api/events")
