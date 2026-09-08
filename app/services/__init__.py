"""Service-layer wiring.

Everything hangs off one Services object: the HTTP layer touches only this,
never the repositories directly.
"""
from __future__ import annotations

import asyncio
import random

from ..core.events import EventBus
from ..logging import LOG
from ..state import StateManager
from .accounts import AccountService
from .autoreply import AutoReplyService, AutoResponder
from .bulk import BulkService
from .campaigns import CampaignService, Scheduler
from .checkup import CheckRunner
from .catalog import CatalogService
from .delivery import DeliveryService
from .login import LoginService
from .operators import OperatorService
from .pacing import Pacer
from .presence import Presence
from .profiles import ProfileService
from .spamcheck import SpamCheckService

MOD = "services"


class Services:
    def __init__(self, storage, service, bus: EventBus | None = None):
        self.storage = storage
        self.service = service
        self.bus = bus or EventBus()
        self.state = StateManager(storage)

        # The order an account does things in around a send: scripted for
        # campaigns and auto-replies, following the person for the chat.
        self.presence = Presence(service)
        # Where a message goes and what it replies to, decided once. The
        # scheduler and the channel check ask the same object.
        self.delivery = DeliveryService(storage, service, self.presence)
        # Built before everything whose deletion can knock a campaign out -
        # its account, that account's API profile, its channels - so each of
        # them can tell the campaign service instead of leaving a dead id.
        self.campaigns = CampaignService(storage, service, self.bus, self.state,
                                         self.delivery)
        self.operators = OperatorService(storage, service, self.bus, self.state,
                                         self.presence, self.campaigns)
        # One send at a time per account and the safety net's count: shared
        # by the scheduler, which counts, and the switch, which clears it.
        self.pacer = Pacer(storage)
        self.autoreply = AutoReplyService(storage, self.bus, self.state)
        self.accounts = AccountService(storage, service, self.bus, self.state,
                                       self.campaigns, self.operators, self.pacer,
                                       self.autoreply)
        # a linked operator's API and proxy are its account's
        self.operators.accounts = self.accounts
        self.profiles = ProfileService(storage, service, self.bus, self.campaigns)
        self.catalog = CatalogService(storage, service, self.bus, self.state,
                                      self.delivery, self.campaigns)
        self.responder = AutoResponder(storage, service, self.bus, self.state,
                                       self.presence)
        self.spamcheck = SpamCheckService(storage, service, self.bus, self.state)
        self.checkup = CheckRunner(storage, service, self.bus, self.state,
                                   self.spamcheck, self.catalog)
        self.scheduler = Scheduler(storage, service, self.bus, self.state,
                                   self.campaigns, self.pacer)
        # One action over many records. It owns no behaviour: every action is
        # a method on one of the services above, so a bulk edit and a single
        # edit cannot come to mean different things.
        self.bulk = BulkService(storage, self)
        # the snapshot carries the run's progress, so the button can show it
        self.state.checkup = self.checkup
        # accounts a check found ready start listening for messages
        self.checkup.on_finish = self.responder.refresh

        # the Telegram layer asks us which credentials each session key needs
        service.profile_resolver = self.accounts.creds_for

        self._maintenance: asyncio.Task | None = None

    # ── background ──────────────────────────────────────────────────────
    def start_background(self) -> None:
        """Kick off everything that runs inside the asyncio loop."""
        # Pictures of campaigns that no longer exist, and anything an
        # interrupted save left behind.
        self.campaigns.sweep_media()
        self.scheduler.resume_unfinished()
        self.service.submit(self._start_async())

    async def _start_async(self) -> None:
        self.scheduler.start()
        await self.responder.refresh()
        self._maintenance = asyncio.ensure_future(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        """The background check, at a random moment inside the interval.

        Read each time round: a setting that only took effect after a restart
        would look broken. The run is the same one the buttons start, minus
        the antispam bot.
        """
        while True:
            try:
                lo, hi = self.storage.settings.probe_interval_range()
                await asyncio.sleep(random.randint(lo, hi))
                await self.profiles.probe_all()
                self.checkup.start("background")
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
