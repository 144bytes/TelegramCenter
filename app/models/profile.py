from __future__ import annotations

from dataclasses import dataclass, field

from ..util import gen_id
from .base import Model
from .enums import ProbeState


@dataclass
class ApiProfile(Model):
    id: str = field(default_factory=lambda: gen_id("api"))
    name: str = ""
    api_id: int | None = None
    api_hash: str = ""
    enabled: bool = True
    raw_state: str = ProbeState.UNKNOWN
    last_check: str | None = None
    last_error: str | None = None


@dataclass
class NetworkProfile(Model):
    id: str = field(default_factory=lambda: gen_id("net"))
    name: str = ""
    protocol: str = "SOCKS5"               # SOCKS5 | SOCKS4 | HTTP
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    enabled: bool = True
    raw_state: str = ProbeState.UNKNOWN
    last_check: str | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None

    def as_telethon_proxy(self):
        """The (proto, host, port, rdns, username, password) tuple Telethon
        wants, or None when the profile is incomplete."""
        if not self.host or not self.port:
            return None
        import socks  # provided by python-socks / PySocks
        proto = {
            "SOCKS5": socks.SOCKS5, "SOCKS4": socks.SOCKS4, "HTTP": socks.HTTP,
        }.get(self.protocol.upper(), socks.SOCKS5)
        if self.username:
            return (proto, self.host, int(self.port), True, self.username, self.password)
        return (proto, self.host, int(self.port))
