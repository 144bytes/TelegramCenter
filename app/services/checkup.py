"""The one check run, for the buttons and for the background alike.

One record at a time: wait a random moment, probe it, ask the antispam
bot if that is on, publish the result, move on. At most one run exists.
The only difference of the background run is that it never messages the
bot; a button pressed while it goes cancels it and starts its own.

What each run covers is its `scope`, which is also how each page tells
its own progress from another page's:

  accounts    the accounts (all, or the ids given)
  operators   independent operators with a session
  targets     channels (all, or the ids given)
  spam        the antispam question alone, no probe
  background  accounts and independent operators, never the bot

A switched-off account is in none of them. An operator made from an
account is checked as that account.
"""
from __future__ import annotations

import asyncio

from ..logging import LOG
from ..messages import AppError
from ..models import Operator
from ..models.enums import AccountState, IssueLevel, SpamState
from .pacing import spread_connect
from .probing import probe as probe_session

MOD = "checkup"

BACKGROUND = "background"

# What `_queue` clears, kept so a cancelled run can put it back.
PROBE_FIELDS = ("raw_state", "last_error")
SPAM_FIELDS = ("spam_state", "spam_detail", "spam_checked_at")


class CheckError(AppError):
    """Why a button's check did not start."""


class CheckRunner:
    def __init__(self, storage, service, bus, state, spamcheck, catalog=None):
        self.storage = storage
        self.service = service
        self.bus = bus
        self.state = state
        self.spamcheck = spamcheck
        self.catalog = catalog
        # awaited after a run that finished, e.g. re-attaching listeners
        self.on_finish = None
        self._future = None
        self._progress = self._idle()
        # record id -> the fields `_queue` cleared, until its turn is over
        self._before: dict[str, tuple[object, dict]] = {}
        # Which run is the current one. A cancelled run may still be unwinding
        # on the loop; it must not touch the progress of the one after it.
        self._generation = 0

    # ── status ──────────────────────────────────────────────────────────
    @staticmethod
    def _idle() -> dict:
        return {"running": False, "total": 0, "done": 0, "current": None,
                "spam": False, "kind": ""}

    @property
    def running(self) -> bool:
        return self._future is not None and not self._future.done()

    def progress(self) -> dict:
        return dict(self._progress)

    # ── start ───────────────────────────────────────────────────────────
    def start(self, scope: str, account_ids: list[str] | None = None,
              target_ids: list[str] | None = None, probe: bool = True,
              spam: bool | None = None,
              operator_ids: list[str] | None = None) -> dict:
        """Queue the records and kick the run off. Returns immediately.

        A button that cannot start is refused, in words: another button's
        run is going, or there is nothing to check. The background run
        never is - it simply comes back next round - and it never stands in
        a button's way: the button stops it and starts its own.
        """
        background = scope == BACKGROUND
        if self.running and (background or self._progress["kind"] != BACKGROUND):
            if background:
                LOG.info("a check is already running", module=MOD)
                return self.progress()
            raise CheckError("err.check.busy")

        if background:
            spam = False
        elif scope == "spam":
            probe, spam = False, True
        else:
            spam = self.spamcheck.enabled if spam is None else bool(spam)
        accounts, operators, targets = self._pick(scope, account_ids, target_ids,
                                                  operator_ids)
        if not accounts and not operators and not targets:
            if background:
                return self.progress()
            if scope == "targets":
                raise CheckError("err.check.no_channels")
            if scope == "operators":
                raise CheckError("err.check.no_operators")
            raise CheckError("err.check.no_accounts")
        if self.running:
            self._cancel()               # the background's, for this button

        self._generation += 1
        self._before = {}
        for owner in accounts + operators:
            self._queue(owner, probe, spam and self._should_ask(owner))
        total = len(accounts) + len(operators) + len(targets)
        self._progress = {"running": True, "total": total, "done": 0,
                          "current": None, "spam": spam, "kind": scope}
        self.bus.publish("checkup.progress", **self.progress())
        LOG.info(f"checking {len(accounts)} account(s), "
                 f"{len(operators)} operator(s) and {len(targets)} channel(s)"
                 f"{' with the antispam pass' if spam else ''} ({scope})",
                 module=MOD)
        self._future = self.service.submit(self._run(
            self._generation, self._before,
            [a.id for a in accounts], [o.id for o in operators],
            [t.id for t in targets], probe, spam))
        return self.progress()

    def _pick(self, scope, account_ids, target_ids,
              operator_ids=None) -> tuple[list, list, list]:
        if scope == "targets":
            targets = (self.storage.targets.all() if target_ids is None else
                       [t for t in map(self.storage.targets.get, target_ids) if t])
            return [], [], targets
        if scope == "operators" and operator_ids is not None:
            picked = [o for o in map(self.storage.operators.get, operator_ids) if o]
            # A linked operator is its account: checking it checks that.
            accounts = [a for a in map(self.storage.linked_account, picked)
                        if a is not None and not a.disabled]
            return accounts, [o for o in picked if not o.linked and o.key], []
        if account_ids is not None:
            accounts = [a for a in map(self.storage.accounts.get, account_ids)
                        if a is not None]
        else:
            accounts = self.storage.accounts.all()
        # Off means off: no probe, no antispam message, no connection.
        accounts = [a for a in accounts if not a.disabled]
        operators = [o for o in self.storage.operators.all()
                     if not o.linked and o.key]
        if scope == "accounts":
            return accounts, [], []
        if scope == "operators":
            return [], operators, []
        if scope == "spam":
            return accounts, [o for o in operators if self._should_ask(o)], []
        return accounts, operators, []           # background

    def _queue(self, owner, probe: bool, spam: bool) -> None:
        """Clear what this run will redo, and only that."""
        cleared = {}
        if probe:
            cleared.update({f: getattr(owner, f) for f in PROBE_FIELDS})
            owner.raw_state = AccountState.QUEUED
            owner.last_error = None
        if spam:
            cleared.update({f: getattr(owner, f) for f in SPAM_FIELDS})
            owner.spam_state = SpamState.UNKNOWN
            owner.spam_detail = None
            owner.spam_checked_at = None
        if cleared:
            self._before[owner.id] = (owner, cleared)
            self._repo(owner).upsert(owner)

    def _repo(self, owner):
        return (self.storage.operators if isinstance(owner, Operator)
                else self.storage.accounts)

    def _cancel(self) -> None:
        """Stop the background run and put back what it had not reached."""
        self._future.cancel()
        self._generation += 1
        self._future = None
        for owner, cleared in self._before.values():
            for field, value in cleared.items():
                setattr(owner, field, value)
            self._repo(owner).upsert(owner)
        self._before = {}
        self._progress = self._idle()
        LOG.info("background check stopped for the one asked for", module=MOD)

    # ── the run ─────────────────────────────────────────────────────────
    def _should_ask(self, owner) -> bool:
        """Operators talk to the bot only when that option is on."""
        return (not isinstance(owner, Operator)
                or self.spamcheck.include_operators)

    async def check_one(self, owner, probe: bool = True, spam: bool = False,
                        pause: float = 0) -> bool:
        """Everything one account or operator goes through, defined once.

        `pause` is waited out just before the bot is spoken to, so a skipped
        record never costs the wait. True when the bot was asked.
        """
        if probe:
            await spread_connect(self.storage)
            await probe_session(owner, self.storage, self.service, self.bus)
        if not (spam and self._should_ask(owner)):
            return False
        if not self._reachable(owner):
            LOG.info(f"{owner.handle}: skipping the antispam check, "
                     f"the account is not ready", module=MOD)
            return False
        if pause:
            await asyncio.sleep(pause)
        await self.spamcheck.check(owner)
        return True

    async def _run(self, gen: int, before: dict, account_ids: list[str],
                   operator_ids: list[str], target_ids: list[str], probe: bool,
                   spam: bool) -> None:
        delay = self.spamcheck.delay_sec
        asked_before = False
        done = 0
        finished = False

        def current() -> bool:
            return gen == self._generation

        def step(record_id: str | None) -> None:
            if current():
                self._progress.update(done=done, current=record_id)
                self.bus.publish("checkup.progress", **self.progress())

        try:
            owners = ([(self.storage.accounts, i) for i in account_ids]
                      + [(self.storage.operators, i) for i in operator_ids])
            for repo, owner_id in owners:
                owner = repo.get(owner_id)
                if owner is not None:            # not deleted mid-run
                    step(owner_id)
                    asked = await self.check_one(
                        owner, probe, spam, pause=delay if asked_before else 0)
                    asked_before = asked_before or asked
                before.pop(owner_id, None)
                done += 1
                step(owner_id)

            for target_id in target_ids:
                target = self.storage.targets.get(target_id)
                if target is not None:
                    step(target_id)
                    await self.catalog.check_target(target)
                done += 1
                step(target_id)
            finished = True
        except asyncio.CancelledError:
            LOG.info("check cancelled", module=MOD)
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"check failed: {exc}", module=MOD)
        finally:
            if current():
                self._progress.update(running=False, current=None)
                self.bus.publish("checkup.progress", **self.progress())
            self.bus.publish("state.recomputed")
            LOG.info("check finished", module=MOD)
        if finished and current() and self.on_finish is not None:
            await self.on_finish()

    def _reachable(self, owner) -> bool:
        """Can this record hold a conversation with the bot right now?"""
        if not owner.key or owner.raw_state != AccountState.READY:
            return False
        if getattr(owner, "disabled", False):
            return False
        issues = (self.state.operator_issues(owner) if isinstance(owner, Operator)
                  else self.state.account_issues(owner))
        return not any(issue.level == IssueLevel.ERROR for issue in issues)

    async def stop(self) -> None:
        if self._future is not None and not self._future.done():
            self._future.cancel()
        self._generation += 1
        self._future = None
        self._progress = self._idle()
