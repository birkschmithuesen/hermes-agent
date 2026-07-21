"""Tests for request_out_of_band_approval — the approval rail used by
non-agent threads (e.g. the in-process anthropic_plan proxy) that know their
own session_key. See docs spec 2026-07-21-egress-rueckfrage-session-rail."""

import threading
import time

import pytest

from tools import approval as ap


@pytest.fixture(autouse=True)
def _clean_registries():
    with ap._lock:
        ap._gateway_queues.clear()
        ap._gateway_notify_cbs.clear()
    yield
    with ap._lock:
        ap._gateway_queues.clear()
        ap._gateway_notify_cbs.clear()


def test_returns_none_when_no_listener_registered():
    """Fail-closed: no notify_cb for this session => no consent, immediately."""
    assert ap.request_out_of_band_approval(
        "agent:main:desktop:sid1",
        command="egress",
        description="d",
        pattern_key="egress_judge",
    ) is None


def test_delivers_choices_and_returns_the_users_choice():
    seen = {}
    key = "agent:main:desktop:sid1"

    def _notify(data):
        seen.update(data)
        # Resolve from another thread, like a real gateway callback would.
        threading.Thread(
            target=lambda: (time.sleep(0.05),
                            ap.resolve_gateway_approval(key, "mask")),
            daemon=True,
        ).start()

    ap.register_gateway_notify(key, _notify)
    choice = ap.request_out_of_band_approval(
        key,
        command="egress to claude",
        description="Kategorien: zugangsdaten",
        pattern_key="egress_judge",
        choices=["allow", "mask", "deny"],
        timeout_seconds=5,
    )
    assert choice == "mask"
    assert seen["choices"] == ["allow", "mask", "deny"]
    assert seen["pattern_key"] == "egress_judge"
    assert seen["allow_permanent"] is False


def test_returns_none_on_timeout():
    key = "agent:main:desktop:sid2"
    ap.register_gateway_notify(key, lambda data: None)
    started = time.monotonic()
    choice = ap.request_out_of_band_approval(
        key, command="c", description="d", pattern_key="p", timeout_seconds=1,
    )
    assert choice is None
    assert time.monotonic() - started < 5


def test_returns_none_when_notify_raises():
    key = "agent:main:desktop:sid3"

    def _boom(data):
        raise RuntimeError("transport down")

    ap.register_gateway_notify(key, _boom)
    assert ap.request_out_of_band_approval(
        key, command="c", description="d", pattern_key="p", timeout_seconds=5,
    ) is None


def test_custom_timeout_overrides_config_default():
    """_await_gateway_decision must honour an explicit timeout_seconds."""
    key = "agent:main:desktop:sid4"
    calls = []
    ap.register_gateway_notify(key, lambda data: calls.append(data))
    started = time.monotonic()
    ap._await_gateway_decision(
        key, lambda data: None, {"command": "c"}, timeout_seconds=1,
    )
    assert time.monotonic() - started < 5
