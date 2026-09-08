"""What paces a send beyond the campaign's own interval.

Two things only: one send at a time per account, and the error-rate
guard that switches off an account that keeps failing. The waits Telegram
names are not here: a slow mode or a mute is written on the campaigns'
results and a flood wait on the account, and the scheduler skips or
waits before it ever takes the lock.
"""
from __future__ import annotations

import asyncio
import random
import time

from ..messages import msg

MOD = "pacing"

HOUR = 3600

async def spread_connect(storage) -> None:
    """Wait a random moment before the next account reaches Telegram.

    Flat rather than skewed: a dozen unrelated people, not one person
    working through a list.
    """
    span = storage.settings.number("accounts.connect_spread_sec")
    if span:
        await asyncio.sleep(random.uniform(0, span))


# How strongly a pause leans towards the short end. 1 is flat; 2.5 puts
# about four fifths of the draws in the lower half.
PAUSE_SKEW = 2.5


def human_pause(low: int, high: int) -> float:
    """A pause inside [low, high], weighted towards the short end."""
    low, high = (low, high) if low <= high else (high, low)
    if high <= low:
        return float(low)
    return low + (high - low) * (random.random() ** PAUSE_SKEW)


class Pacer:
    def __init__(self, storage):
        self.storage = storage
        self._locks: dict[str, asyncio.Lock] = {}
        # outcomes of the last hour per account: (when, succeeded, chat)
        self._outcomes: dict[str, list[tuple[float, bool, str]]] = {}

    def setting(self, name: str) -> int:
        return self.storage.settings.number(f"campaign.{name}")

    def lock(self, account_id: str) -> asyncio.Lock:
        """One account, one send at a time. Held around the requests only,
        never across a wait."""
        lock = self._locks.get(account_id)
        if lock is None:
            lock = self._locks[account_id] = asyncio.Lock()
        return lock

    def note_outcome(self, account_id: str, target_id: str, ok: bool) -> None:
        """Count a send towards the guard. Only sends that tell about the
        account or the network belong here - a chat that refuses does not.

        A failure in a chat that has already failed since its last success is
        the same failure: two campaigns sending there do not count it twice.
        """
        now = time.monotonic()
        window = [row for row in self._outcomes.get(account_id, [])
                  if row[0] >= now - HOUR]
        if not ok:
            for _at, succeeded, chat in reversed(window):
                if chat != target_id:
                    continue
                if not succeeded:
                    self._outcomes[account_id] = window
                    return
                break
        window.append((now, ok, target_id))
        self._outcomes[account_id] = window

    def forget(self, account_id: str) -> None:
        """Start the account's count afresh: switching it back on by hand
        means the user has dealt with what the count was about."""
        self._outcomes.pop(account_id, None)

    def error_rate_exceeded(self, account_id: str) -> dict | None:
        """Has this account failed too much in the last hour to keep going?

        Both halves matter: a percentage alone fires on the first failure.
        """
        percent = self.setting("error_stop_percent")
        minimum = self.setting("error_stop_min")
        if not percent or not minimum:
            return None
        now = time.monotonic()
        window = [row for row in self._outcomes.get(account_id, [])
                  if row[0] >= now - HOUR]
        if len(window) < minimum:
            return None
        failed = sum(1 for _at, ok, _chat in window if not ok)
        share = failed * 100 // len(window)
        if share < percent:
            return None
        return msg("note.error_rate", failed=failed, total=len(window),
                   share=share)
