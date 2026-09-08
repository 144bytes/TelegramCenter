"""API and proxy profiles: CRUD plus the probes that set their raw state.

A probe writes only `raw_state` / `last_error`. Everything downstream — which
accounts turn red, which campaigns stop — is derived by StateManager from that
single value, so a probe never has to know who depends on it.
"""
from __future__ import annotations

import asyncio
import socket
import time

from ..logging import LOG
from ..models import ApiProfile, NetworkProfile
from ..models.enums import ProbeState
from ..util import now_iso

MOD = "profiles"


class ProfileService:
    def __init__(self, storage, service, bus):
        self.storage = storage
        self.service = service
        self.bus = bus

    def _changed(self, kind: str, id_: str) -> None:
        self.bus.publish("entity.changed", entity=kind, id=id_)

    # ── API profiles ────────────────────────────────────────────────────
    def create_api(self, **fields) -> ApiProfile:
        prof = ApiProfile(**fields)
        self.storage.api_profiles.add(prof)
        self._changed("api_profile", prof.id)
        return prof

    def ensure_api(self, api_id, api_hash: str, name: str = "",
                   verified: bool = False) -> ApiProfile:
        """The profile for this api_id/api_hash pair, created if it is new.

        Typing the same keys twice must not leave two identical profiles
        behind, so an existing match is reused.
        """
        api_id = int(api_id)
        api_hash = str(api_hash).strip()
        existing = self.storage.api_profiles.find(
            lambda p: p.api_id == api_id and p.api_hash == api_hash)
        if existing is not None:
            return existing
        return self.create_api(
            name=name.strip() or f"API {api_id}",
            api_id=api_id, api_hash=api_hash,
            # "verified" means Telegram just accepted these keys during a
            # login, which is better evidence than a probe would give
            raw_state=ProbeState.ONLINE if verified else ProbeState.UNKNOWN,
            last_check=now_iso() if verified else None)

    def update_api(self, prof: ApiProfile, **fields) -> ApiProfile:
        for k, v in fields.items():
            if hasattr(prof, k):
                setattr(prof, k, v)
        self.storage.api_profiles.upsert(prof)
        self._changed("api_profile", prof.id)
        return prof

    def delete_api(self, id_: str) -> bool:
        ok = self.storage.api_profiles.delete(id_)
        if ok:
            # dependants are not rewritten: StateManager will report them as
            # broken on the next read, which is what makes the error visible
            # instead of silently repairing it.
            self._changed("api_profile", id_)
        return ok

    async def probe_api(self, prof: ApiProfile) -> ApiProfile:
        prof.raw_state = ProbeState.CHECKING
        self.storage.api_profiles.upsert(prof)
        self._changed("api_profile", prof.id)

        from telethon import TelegramClient
        from telethon.sessions import MemorySession
        client = None
        try:
            client = TelegramClient(MemorySession(), int(prof.api_id), prof.api_hash)
            await asyncio.wait_for(client.connect(), timeout=20)
            if not client.is_connected():
                raise RuntimeError("не удалось подключиться к Telegram")
            prof.raw_state, prof.last_error = ProbeState.ONLINE, None
        except Exception as exc:  # noqa: BLE001
            prof.raw_state = ProbeState.ERROR
            prof.last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning(f"api probe {prof.name}: {prof.last_error}", module=MOD)
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        prof.last_check = now_iso()
        self.storage.api_profiles.upsert(prof)
        self._changed("api_profile", prof.id)
        return prof

    # ── proxy profiles ──────────────────────────────────────────────────
    def create_proxy(self, **fields) -> NetworkProfile:
        prof = NetworkProfile(**fields)
        self.storage.network_profiles.add(prof)
        self._changed("network_profile", prof.id)
        return prof

    def update_proxy(self, prof: NetworkProfile, **fields) -> NetworkProfile:
        for k, v in fields.items():
            if hasattr(prof, k):
                setattr(prof, k, v)
        self.storage.network_profiles.upsert(prof)
        self._changed("network_profile", prof.id)
        return prof

    def delete_proxy(self, id_: str) -> bool:
        ok = self.storage.network_profiles.delete(id_)
        if ok:
            self._changed("network_profile", id_)
        return ok

    async def probe_proxy(self, prof: NetworkProfile) -> NetworkProfile:
        prof.raw_state = ProbeState.CHECKING
        self.storage.network_profiles.upsert(prof)
        self._changed("network_profile", prof.id)

        def _connect() -> int:
            started = time.perf_counter()
            with socket.create_connection((prof.host, int(prof.port)), timeout=8):
                pass
            return int((time.perf_counter() - started) * 1000)

        try:
            latency = await asyncio.wait_for(asyncio.to_thread(_connect), timeout=12)
            prof.raw_state, prof.last_error = ProbeState.ONLINE, None
            prof.last_latency_ms = latency
        except Exception as exc:  # noqa: BLE001
            prof.raw_state = ProbeState.ERROR
            prof.last_error = f"{type(exc).__name__}: {exc}"
            prof.last_latency_ms = None
            LOG.warning(f"proxy probe {prof.name}: {prof.last_error}", module=MOD)
        prof.last_check = now_iso()
        self.storage.network_profiles.upsert(prof)
        self._changed("network_profile", prof.id)
        return prof

    async def probe_all(self) -> None:
        for prof in self.storage.api_profiles.all():
            if prof.enabled and prof.api_id and prof.api_hash:
                await self.probe_api(prof)
        for prof in self.storage.network_profiles.all():
            if prof.enabled and prof.host and prof.port:
                await self.probe_proxy(prof)
