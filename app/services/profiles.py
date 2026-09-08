"""API and proxy profiles: CRUD, and the proxy probe.

An API profile has no probe of its own: connecting proves nothing about
the keys, and would come from the home address. Its status is what the
accounts using it found (see StateManager.api_profile_view).
"""
from __future__ import annotations

import asyncio
import socket
import time

from ..logging import LOG
from ..messages import msg, raw
from ..models import ApiProfile, NetworkProfile
from ..models.enums import ProbeState
from ..util import now_iso

MOD = "profiles"

# What the account's card says after the profile it used was deleted.
API_DELETED = msg("note.api_deleted")
PROXY_DELETED = msg("note.proxy_deleted")
PROXY_DELETED_OP = msg("note.proxy_deleted_operator")

# What a pooled client is built from, per kind of profile: a change to any
# of these reconnects the accounts behind it (AccountService.creds_for).
CONNECTION = {
    "api_profile": ("api_id", "api_hash", "enabled"),
    "network_profile": ("protocol", "host", "port", "username", "password", "enabled"),
}


class ProfileService:
    def __init__(self, storage, service, bus, campaigns):
        self.storage = storage
        self.service = service
        self.bus = bus
        # Deleting an API profile switches its accounts off, and a switched
        # off account cannot go on being scheduled to send.
        self.campaigns = campaigns

    def _changed(self, kind: str, id_: str) -> None:
        self.bus.publish("entity.changed", entity=kind, id=id_)

    def _invalidate_users(self, profile_id: str) -> None:
        """Drop the pooled client of everything connecting through this profile.

        Editing a proxy changes the address an account reaches Telegram from,
        and one auth key on two addresses is what kills a session.
        """
        for repo in (self.storage.accounts, self.storage.operators):
            for owner in repo.all():
                if owner.key and profile_id in (owner.api_profile_id,
                                                owner.network_profile_id):
                    self.service.invalidate(owner.key)

    # ── API profiles ────────────────────────────────────────────────────
    def create_api(self, **fields) -> ApiProfile:
        prof = ApiProfile(**fields)
        self.storage.api_profiles.add(prof)
        self._changed("api_profile", prof.id)
        return prof

    def ensure_api(self, api_id, api_hash: str, name: str = "") -> ApiProfile:
        """The profile for this api_id/api_hash pair, created if it is new.

        Typing the same keys twice must not leave two identical profiles.
        """
        api_id = int(api_id)
        api_hash = str(api_hash).strip()
        existing = self.storage.api_profiles.find(
            lambda p: p.api_id == api_id and p.api_hash == api_hash)
        if existing is not None:
            return existing
        return self.create_api(name=name.strip() or f"API {api_id}",
                               api_id=api_id, api_hash=api_hash)

    def update_api(self, prof: ApiProfile, **fields) -> ApiProfile:
        return self._save(self.storage.api_profiles, "api_profile", prof, fields)

    def _save(self, repo, kind: str, prof, fields: dict):
        """Write an edited profile, and reconnect everything that used it
        when what a connection is built from changed.

        Both kinds save the same way. Renaming one is not a reason to
        reconnect every account behind it.
        """
        reconnects = any(k in fields and getattr(prof, k) != fields[k]
                         for k in CONNECTION[kind])
        for k, v in fields.items():
            if hasattr(prof, k):
                setattr(prof, k, v)
        repo.upsert(prof)
        if reconnects:
            self._invalidate_users(prof.id)
        self._changed(kind, prof.id)
        return prof

    async def _probe(self, repo, kind: str, prof, run) -> object:
        """Run one check and write down what it said: CHECKING, the verdict,
        and the card told both times."""
        prof.raw_state = ProbeState.CHECKING
        repo.upsert(prof)
        self._changed(kind, prof.id)
        try:
            await run()
            prof.raw_state, prof.last_error = ProbeState.ONLINE, None
        except Exception as exc:  # noqa: BLE001 - the verdict is the result
            prof.raw_state = ProbeState.ERROR
            prof.last_error = raw(f"{type(exc).__name__}: {exc}")
            LOG.warning(f"probe {prof.name}: {type(exc).__name__}: {exc}",
                        module=MOD)
        prof.last_check = now_iso()
        repo.upsert(prof)
        self._changed(kind, prof.id)
        return prof

    def delete_api(self, id_: str) -> bool:
        self._invalidate_users(id_)
        ok = self.storage.api_profiles.delete(id_)
        if ok:
            self._drop_api_users(id_)
            self._changed("api_profile", id_)
        return ok

    def _drop_api_users(self, profile_id: str) -> None:
        """Let go of a deleted API profile everywhere it was named.

        These are the keys an account signs in with: without them it cannot
        connect, so it is stopped. The id goes with the profile.
        """
        for account in self._users("api_profile_id", profile_id):
            self.campaigns.stop_account(account, API_DELETED)
        for operator in self.storage.operators.all():
            if operator.api_profile_id != profile_id:
                continue
            operator.api_profile_id = None
            self.storage.operators.upsert(operator)
            self._changed("operator", operator.id)

    # ── proxy profiles ──────────────────────────────────────────────────
    def create_proxy(self, **fields) -> NetworkProfile:
        prof = NetworkProfile(**fields)
        self.storage.network_profiles.add(prof)
        self._changed("network_profile", prof.id)
        return prof

    def update_proxy(self, prof: NetworkProfile, **fields) -> NetworkProfile:
        return self._save(self.storage.network_profiles, "network_profile",
                          prof, fields)

    def delete_proxy(self, id_: str) -> bool:
        self._invalidate_users(id_)
        ok = self.storage.network_profiles.delete(id_)
        if ok:
            self._drop_proxy_users(id_)
            self._changed("network_profile", id_)
        return ok

    def _drop_proxy_users(self, profile_id: str) -> None:
        """Take away the route, and stop until the user has decided.

        The account goes back to the direct route - the only one left - but
        stops rather than quietly sending from the home address the proxy was
        there to avoid. Carrying on without a proxy is the user's answer.
        """
        for account in self._users("network_profile_id", profile_id):
            self.campaigns.stop_account(account, PROXY_DELETED)
        for operator in self.storage.operators.all():
            if operator.network_profile_id != profile_id:
                continue
            # An operator has no switch - it is a person's own account, and the
            # user is holding the chat open - so it gets the note and nothing
            # else.
            operator.network_profile_id = None
            operator.stop_note = PROXY_DELETED_OP
            self.storage.operators.upsert(operator)
            self._changed("operator", operator.id)

    def _users(self, field: str, profile_id: str) -> list:
        """Accounts that named this profile, with the reference taken off.

        Saved here, so no path can stop an account and leave the dead id on it.
        """
        found = []
        for account in self.storage.accounts.all():
            if getattr(account, field) != profile_id:
                continue
            setattr(account, field, None)
            self.storage.accounts.upsert(account)
            self._changed("account", account.id)
            found.append(account)
        return found

    async def probe_proxy(self, prof: NetworkProfile) -> NetworkProfile:
        """Can the proxy be reached, and how long does it take?"""
        def _connect() -> int:
            started = time.perf_counter()
            with socket.create_connection((prof.host, int(prof.port)), timeout=8):
                pass
            return int((time.perf_counter() - started) * 1000)

        async def measure() -> None:
            prof.last_latency_ms = None      # no number until there is one
            prof.last_latency_ms = await asyncio.wait_for(
                asyncio.to_thread(_connect), timeout=12)

        return await self._probe(self.storage.network_profiles,
                                 "network_profile", prof, measure)

    async def probe_all(self) -> None:
        for prof in self.storage.network_profiles.all():
            if prof.enabled and prof.host and prof.port:
                await self.probe_proxy(prof)
