"""The "check all" run.

One account at a time, start to finish: probe it, ask the antispam bot about it
if that is switched on, publish its result, then move to the next. The account
only gets its colour once its own turn is over - showing every account green
after a fast first pass and running the slow antispam pass afterwards made the
green mean nothing.

The run lives on the asyncio loop and the HTTP call returns as soon as it has
started, so the interface stays usable and campaigns and auto-replies keep
working while it goes.
"""
from __future__ import annotations

import asyncio

from ..logging import LOG
from ..models import Operator
from ..models.enums import AccountState, IssueLevel, SpamState
from .probing import probe as probe_session

MOD = "checkup"


class CheckRunner:
    def __init__(self, storage, service, bus, state, spamcheck):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.spamcheck = spamcheck
        self._future = None
        self._progress = self._idle()

    # ── status ──────────────────────────────────────────────────────────
    @staticmethod
    def _idle() -> dict:
        return {"running": False, "total": 0, "done": 0, "current": None,
                "spam": False}

    @property
    def running(self) -> bool:
        return self._future is not None and not self._future.done()

    def progress(self) -> dict:
        return dict(self._progress)

    # ── start ───────────────────────────────────────────────────────────
    def start(self, probe: bool = True, spam: bool | None = None,
              account_ids: list[str] | None = None,
              scope: str = "all") -> dict:
        """Queue the records and kick the run off. Returns immediately.

        `account_ids` narrows it to one account without changing anything else:
        the same queueing, the same probe, the same antispam pass, the same
        progress. Checking one account has to mean exactly what checking all of
        them means, or the two would drift apart.

        `scope` is "all", "accounts" or "operators" - which is how each page's
        button checks the records that page is about while still being this one
        run. Only one run exists at a time either way.
        """
        if self.running:
            LOG.info("a check is already running", module=MOD)
            return self.progress()

        spam = self.spamcheck.enabled if spam is None else bool(spam)
        if account_ids is not None:
            # A targeted run reaches what the bulk run leaves alone: a
            # switched-off account is precisely the one worth re-checking.
            accounts = [a for a in (self.storage.accounts.get(i)
                                    for i in account_ids) if a is not None]
            operators = []
        else:
            accounts = ([] if scope == "operators" else
                        [a for a in self.storage.accounts.all() if not a.disabled])
            # Operators connect the same way and can go stale the same way, so
            # they are checked by the same run. Whether they are also asked
            # about spam is a setting, off by default.
            operators = ([] if scope == "accounts" else
                         [o for o in self.storage.operators.all() if o.key])
        if not accounts and not operators:
            return self.progress()

        for account in accounts:
            self._queue(account, probe)
        for operator in operators:
            self._queue(operator, probe)
        total = len(accounts) + len(operators)
        self.bus.publish("checkup.queued", total=total)

        self._progress = {"running": True, "total": total, "done": 0,
                          "current": None, "spam": spam}
        LOG.info(f"checking {len(accounts)} account(s) and "
                 f"{len(operators)} operator(s)"
                 f"{' with the antispam pass' if spam else ''}", module=MOD)
        self._future = self.service.submit(self._run(
            [a.id for a in accounts], [o.id for o in operators], probe, spam))
        return self.progress()

    def _queue(self, owner, probe: bool) -> None:
        """Clear what the previous run concluded.

        Anything this run is about to redo is dropped, so a stale verdict
        cannot be mistaken for a fresh one. Problems that are true regardless
        of any check - a missing API profile, no session file - keep showing,
        because hiding a known fault would be worse than the staleness it
        avoids.

        Only what gets redone is cleared: an antispam-only run must not blank
        the account's readiness, or every account would look unready and be
        skipped for being so.
        """
        if probe:
            owner.raw_state = AccountState.QUEUED
            owner.last_error = None
        if hasattr(owner, "spam_state"):     # operators are never spam-checked
            owner.spam_state = SpamState.UNKNOWN
            owner.spam_detail = ""
            owner.spam_checked_at = None
        repo = (self.storage.operators if isinstance(owner, Operator)
                else self.storage.accounts)
        repo.upsert(owner)

    # ── the run ─────────────────────────────────────────────────────────
    def _should_ask(self, owner, spam: bool | None) -> bool:
        """Whether the antispam bot is spoken to for this record.

        One place decides, so the bulk run, a targeted re-check and the UI can
        never disagree. The operator option has the last word: with it off,
        even a deliberate re-check of an operator must not message the bot.
        """
        if isinstance(owner, Operator) and not self.spamcheck.include_operators:
            return False
        return self.spamcheck.enabled if spam is None else bool(spam)

    async def check_one(self, owner, probe: bool = True,
                        spam: bool | None = None, pause: float = 0) -> bool:
        """Everything one account or operator goes through, defined once.

        Checking all of them is this, in a loop; checking one is this, once.
        Two separate versions would drift, and the hard-to-reproduce half would
        be the bulk one. Operators used to have their own shorter version, and
        that is precisely why their state lagged behind what accounts got.

        `pause` is waited out immediately before the bot is spoken to, so a
        skipped record never costs the wait. Returns whether the bot was
        actually asked, which is what tells the caller to pause next time.
        """
        if probe:
            await probe_session(owner, self.storage, self.service, self.bus)
        if not self._should_ask(owner, spam):
            return False
        if not self._reachable(owner):
            LOG.info(f"{owner.handle}: skipping the antispam check, "
                     f"the account is not ready", module=MOD)
            return False
        if pause:
            await asyncio.sleep(pause)
        await self.spamcheck.check(owner)
        return True

    async def _run(self, account_ids: list[str], operator_ids: list[str],
                   probe: bool, spam: bool) -> None:
        delay = self.spamcheck.delay_sec
        asked_before = False
        done = 0
        try:
            for account_id in account_ids:
                account = self.storage.accounts.get(account_id)
                if account is None:            # deleted mid-run
                    continue

                self._progress.update(done=done, current=account_id)
                self.bus.publish("checkup.progress", **self.progress())

                asked = await self.check_one(
                    account, probe, spam,
                    pause=delay if asked_before else 0)
                asked_before = asked_before or asked

                done += 1
                self._progress.update(done=done)
                self.bus.publish("checkup.progress", **self.progress())

            for operator_id in operator_ids:
                operator = self.storage.operators.get(operator_id)
                if operator is None:
                    continue
                self._progress.update(done=done, current=operator_id)
                self.bus.publish("checkup.progress", **self.progress())
                asked = await self.check_one(
                    operator, probe, spam,
                    pause=delay if asked_before else 0)
                asked_before = asked_before or asked
                done += 1
                self._progress.update(done=done)
                self.bus.publish("checkup.progress", **self.progress())
        except asyncio.CancelledError:
            LOG.info("check cancelled", module=MOD)
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"check failed: {exc}", module=MOD)
        finally:
            self._progress.update(running=False, current=None)
            self.bus.publish("checkup.progress", **self.progress())
            self.bus.publish("state.recomputed")
            LOG.info("check finished", module=MOD)

    def _reachable(self, owner) -> bool:
        """Can this record hold a conversation with the bot right now?

        Deliberately blind to the on/off switch: a switched-off account is the
        one a targeted check exists for, and the bulk run has already left
        those out. A real fault still counts.
        """
        if not owner.key or owner.raw_state != AccountState.READY:
            return False
        issues = (self.state.operator_issues(owner) if isinstance(owner, Operator)
                  else self.state.account_issues(owner))
        return not any(issue.level == IssueLevel.ERROR for issue in issues)

    async def stop(self) -> None:
        if self._future is not None and not self._future.done():
            self._future.cancel()
        self._future = None
        self._progress = self._idle()
