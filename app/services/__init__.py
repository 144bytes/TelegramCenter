"""Service-layer wiring.

Everything hangs off one Services object: the HTTP layer touches only this,
never the repositories directly.
"""
from __future__ import annotations

import asyncio

from ..core.events import EventBus
from ..logging import LOG
from ..state import StateManager
from .accounts import AccountService
from .autoreply import AutoReplyService, AutoResponder
from .campaigns import CampaignService, Scheduler
from .checkup import CheckRunner
from .catalog import CatalogService
from .login import LoginService
from .operators import OperatorService
from .profiles import ProfileService
from .spamcheck import SpamCheckService

MOD = "services"


class Services:
    def __init__(self, storage, service, bus: EventBus | None = None):
        self.storage = storage
        self.service = service
        self.bus = bus or EventBus()
        self.state = StateManager(storage)

        self.accounts = AccountService(storage, service, self.bus, self.state)
        self.operators = OperatorService(storage, service, self.bus, self.state)
        self.profiles = ProfileService(storage, service, self.bus)
        self.catalog = CatalogService(storage, service, self.bus, self.state)
        self.autoreply = AutoReplyService(storage, self.bus, self.state)
        self.responder = AutoResponder(storage, service, self.bus, self.state)
        self.spamcheck = SpamCheckService(storage, service, self.bus, self.state)
        self.checkup = CheckRunner(storage, service, self.bus, self.state,
                                   self.spamcheck)
        self.campaigns = CampaignService(storage, service, self.bus, self.state)
        self.scheduler = Scheduler(storage, service, self.bus, self.state,
                                   self.campaigns)
        # the snapshot carries the run's progress, so the button can show it
        self.state.checkup = self.checkup

        # the Telegram layer asks us which credentials each session key needs
        service.profile_resolver = self.accounts.creds_for
        default = storage.default_api_profile()
        if default is not None:
            service.configure_default_api(default.api_id, default.api_hash)

        self._maintenance: asyncio.Task | None = None

    # ── background ──────────────────────────────────────────────────────
    def start_background(self) -> None:
        """Kick off everything that runs inside the asyncio loop."""
        self.scheduler.resume_unfinished()
        self.service.submit(self._start_async())

    async def _start_async(self) -> None:
        self.scheduler.start()
        await self.responder.refresh()
        self._maintenance = asyncio.ensure_future(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        """Periodic re-probe, and re-attach listeners after state changes."""
        interval = max(60, int(self.storage.settings.get(
            "accounts.probe_interval_sec", 300)))
        while True:
            try:
                await asyncio.sleep(interval)
                if self.checkup.running:
                    # a run started from the button owns the accounts right
                    # now; re-probing underneath it would fight its results
                    continue
                await self.profiles.probe_all()
                # The same per-account check the button runs, minus the
                # antispam pass - that would have every account message the bot
                # on a timer, unasked - and minus the greying, because a quiet
                # background refresh should not look like someone pressed
                # something.
                for account in self.storage.accounts.all():
                    if account.disabled:
                        continue
                    await self.checkup.check_one(account, spam=False)
                await self.responder.refresh()
                self.bus.publish("state.recomputed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"maintenance loop: {exc}", module=MOD)

    async def stop_background(self) -> None:
        if self._maintenance is not None:
            self._maintenance.cancel()
            try:
                await self._maintenance
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._maintenance = None
        await self.checkup.stop()
        await self.scheduler.stop()
        await self.responder.stop()

    def shutdown(self) -> None:
        try:
            self.service.run(self.stop_background(), timeout=15)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"stop_background: {exc}", module=MOD)
        self.bus.close()

    # ── used by the HTTP layer after anything changes ───────────────────
    def recompute(self) -> None:
        self.bus.publish("state.recomputed")

    def sync_listeners(self) -> None:
        """Re-evaluate which accounts should be listening for messages."""
        self.service.submit(self.responder.refresh())


# LoginService needs `accounts`, so it is attached after construction to keep
# the dependency direction obvious.
def build_services(storage, service, bus: EventBus | None = None) -> Services:
    services = Services(storage, service, bus)
    services.login = LoginService(storage, service, services.bus,
                                  services.accounts, services.profiles)
    return services
