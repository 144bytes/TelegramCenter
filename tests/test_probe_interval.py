"""The background check waits a different length of time every round.

Every signed-in account stays connected for as long as the app runs and is
touched on this schedule — roughly 2600 times over three days. At a fixed
interval that is a metronome coming from one address.
"""
from __future__ import annotations


def test_the_default_is_a_range(storage):
    assert storage.settings.probe_interval_range() == (900, 1800)


def test_the_range_is_returned_the_right_way_round(storage):
    storage.settings.update({"accounts.probe_interval_min_sec": 1800,
                             "accounts.probe_interval_max_sec": 900})
    assert storage.settings.probe_interval_range() == (900, 1800)


def test_nothing_ever_waits_less_than_a_minute(storage):
    storage.settings.update({"accounts.probe_interval_min_sec": 0,
                             "accounts.probe_interval_max_sec": 5})
    assert storage.settings.probe_interval_range() == (60, 60)


def test_nonsense_falls_back_rather_than_breaking_the_loop(storage):
    storage.settings.set("accounts.probe_interval_min_sec", "часто")
    assert storage.settings.probe_interval_range() == (900, 1800)


def test_the_wait_actually_varies(storage, monkeypatch):
    """Not the middle of the range, not the low end: a fresh number each
    round, which is the whole point of the setting being a range."""
    import random
    storage.settings.update({"accounts.probe_interval_min_sec": 900,
                             "accounts.probe_interval_max_sec": 1800})
    lo, hi = storage.settings.probe_interval_range()
    draws = {random.randint(lo, hi) for _ in range(50)}
    assert len(draws) > 1
    assert all(lo <= d <= hi for d in draws)
