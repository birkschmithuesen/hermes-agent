"""Regression test (finding I3, whole-branch review 2026-07-22): /stop and /new
on messaging platforms (e.g. Telegram) must free a pending out-of-band
approval, mirroring what tui_gateway's ``session.interrupt`` handler already
does (``resolve_gateway_approval(session_key, "deny", resolve_all=True)`` on
interrupt — see ``tui_gateway/server.py``).

Without this, a session blocked in ``request_out_of_band_approval`` (the
egress-judge rail, running on the proxy's HTTP handler thread — NOT the
agent's execution thread) ignores /stop entirely: the command appears to do
nothing until the full approval timeout elapses, and the gateway executor
worker handling it stays held the whole time.

``_interrupt_and_clear_session`` is the shared interrupt path used by /stop,
/reset and /new across every messaging platform (gateway/run.py and
gateway/slash_commands.py), so fixing it there covers Telegram and friends.
"""

import threading
import time

import pytest

from gateway.run import GatewayRunner, _INTERRUPT_REASON_STOP
from gateway.session import SessionSource, build_session_key
from gateway.platforms.base import Platform
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


def _source(uid="userA", chat_id="chan1"):
    return SessionSource(
        platform=Platform.DISCORD, chat_type="dm", chat_id=chat_id, user_id=uid,
    )


@pytest.mark.asyncio
async def test_interrupt_and_clear_session_frees_pending_egress_approval():
    source = _source()
    session_key = build_session_key(source)

    # Simulate a proxy handler thread blocked in the out-of-band approval
    # rail on behalf of this session (this is exactly what egress_judge does
    # while the surface shows the user a Rückfrage prompt).
    ap.register_gateway_notify(session_key, lambda data: None)

    result_box = {}

    def _blocked_wait():
        result_box["choice"] = ap.request_out_of_band_approval(
            session_key,
            command="egress to claude",
            description="Grund: test",
            pattern_key="egress_judge",
            choices=["allow", "mask", "deny"],
            timeout_seconds=30,
        )

    waiter = threading.Thread(target=_blocked_wait, daemon=True)
    waiter.start()

    # Give the waiter thread time to register its entry in the gateway queue.
    deadline = time.monotonic() + 5
    while not ap.has_blocking_approval(session_key) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ap.has_blocking_approval(session_key), "waiter never queued its approval"

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {}
    runner._pending_messages = {}

    await runner._interrupt_and_clear_session(
        session_key,
        source,
        interrupt_reason=_INTERRUPT_REASON_STOP,
        invalidation_reason="stop_command",
        release_running_state=False,
    )

    waiter.join(timeout=5)
    assert not waiter.is_alive(), "interrupt did not free the blocked approval wait"
    assert result_box.get("choice") == "deny"
    assert not ap.has_blocking_approval(session_key)
