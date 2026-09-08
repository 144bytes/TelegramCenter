"""Only one TelegramCenter may run at a time.

A second backend would bind a second port and put a second scheduler on the
same session files, so both copies would send.
"""
from __future__ import annotations

import sys
import uuid

import pytest

from app.instance import SingleInstance

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the guard uses a Windows named mutex")


def _name() -> str:
    return f"TelegramCenterTest_{uuid.uuid4().hex[:12]}"


def test_first_instance_may_run():
    guard = SingleInstance(_name())
    try:
        assert guard.acquire() is True
    finally:
        guard.release()


def test_second_instance_is_refused():
    name = _name()
    first = SingleInstance(name)
    second = SingleInstance(name)
    try:
        assert first.acquire() is True
        assert second.acquire() is False, "a second copy must not start"
    finally:
        first.release()
        second.release()


def test_the_slot_is_free_again_after_release():
    name = _name()
    first = SingleInstance(name)
    assert first.acquire() is True
    first.release()

    second = SingleInstance(name)
    try:
        assert second.acquire() is True, "releasing must free the slot"
    finally:
        second.release()


def test_different_names_do_not_collide():
    a, b = SingleInstance(_name()), SingleInstance(_name())
    try:
        assert a.acquire() is True
        assert b.acquire() is True
    finally:
        a.release()
        b.release()


def test_release_is_safe_to_call_twice():
    guard = SingleInstance(_name())
    guard.acquire()
    guard.release()
    guard.release()      # must not raise


def test_release_without_acquire_is_safe():
    SingleInstance(_name()).release()


def test_main_returns_immediately_when_another_instance_holds_the_slot(
        app_dir, monkeypatch):  # noqa: ARG001
    """The whole point: launching the exe again does nothing at all."""
    import main

    started = {"telegram": False}

    class _Boom:
        def __init__(self, *a, **kw):
            started["telegram"] = True

    monkeypatch.setattr("app.telegram.service.TelegramService", _Boom)
    monkeypatch.setattr(SingleInstance, "acquire", lambda self: False)

    assert main.main() == 0
    assert started["telegram"] is False, "nothing may be started by a second copy"
