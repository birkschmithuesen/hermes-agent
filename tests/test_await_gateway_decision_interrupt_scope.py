"""Regression tests (finding I4, whole-branch review 2026-07-22).

``_await_gateway_decision``'s wait loop calls ``tools.interrupt.is_interrupted()``,
which checks a set keyed by ``threading.current_thread().ident``
(``tools/interrupt.py``). That is meaningful for callers that run the wait on
the AGENT's own execution thread (the terminal command guard, the
execute_code guard) — ``AIAgent.interrupt()`` sets the bit on exactly that
thread.

``request_out_of_band_approval`` (the egress-judge rail) is explicitly
documented to run on a thread that is NOT inside an agent turn — a
``ThreadingHTTPServer`` handler thread for the in-process proxy. On such a
freshly created thread the check is always False (dead code). Worse, CPython
recycles thread idents: if a *different*, unrelated thread happened to set an
interrupt bit on the ident the handler thread now holds (e.g. a prior agent
thread died without clearing its bit), the wait would resolve to "deny"
INSTANTLY without ever showing the user a prompt — a phantom deny with no
explanation.

The fix scopes the interrupt check to callers that opt in (``check_interrupt``,
default True, preserving existing agent-thread behavior) and has the
out-of-band rail opt out, since it has no ownership relationship with
whatever thread happens to be running it.
"""

import threading
import time

import pytest

from tools import approval as ap
from tools.interrupt import set_interrupt


@pytest.fixture(autouse=True)
def _clean_state():
    with ap._lock:
        ap._gateway_queues.clear()
        ap._gateway_notify_cbs.clear()
    set_interrupt(False)  # clear current thread's bit before/after each test
    yield
    with ap._lock:
        ap._gateway_queues.clear()
        ap._gateway_notify_cbs.clear()
    set_interrupt(False)


def test_agent_thread_caller_still_honors_interrupt():
    """Preserve existing behavior: an agent-thread caller (check_interrupt=True,
    the default) must still resolve to "deny" instantly when the CURRENT
    thread's interrupt bit is set — this is what the terminal command guard
    and execute_code guard depend on."""
    key = "agent:main:desktop:sidX"
    ap.register_gateway_notify(key, lambda data: None)
    set_interrupt(True)  # simulate AIAgent.interrupt() having flagged this thread
    try:
        started = time.monotonic()
        decision = ap._await_gateway_decision(
            key, lambda data: None, {"command": "c"}, timeout_seconds=30,
        )
    finally:
        set_interrupt(False)
    elapsed = time.monotonic() - started
    assert decision["choice"] == "deny"
    assert elapsed < 5, "should resolve immediately on the interrupt check, not the timeout"


def test_out_of_band_rail_ignores_stale_interrupt_bit_on_recycled_ident():
    """Simulates CPython thread-ident reuse: a stale interrupt bit sits on the
    CURRENT thread's ident (as if a previous, unrelated agent thread died
    without clearing it and a proxy handler thread inherited the ident).

    Before the fix, ``request_out_of_band_approval`` shares the same wait
    loop as agent-thread callers and would misfire "deny" instantly. After
    the fix it must not consult the interrupt flag at all, and should behave
    exactly like a normal out-of-band wait (blocks until resolved or times
    out).
    """
    key = "agent:main:desktop:sidY"
    seen = {}

    def _notify(data):
        seen.update(data)
        threading.Thread(
            target=lambda: (time.sleep(0.05), ap.resolve_gateway_approval(key, "mask")),
            daemon=True,
        ).start()

    ap.register_gateway_notify(key, _notify)
    set_interrupt(True)  # stale bit on this thread's (possibly recycled) ident
    try:
        choice = ap.request_out_of_band_approval(
            key,
            command="egress to claude",
            description="Grund: test",
            pattern_key="egress_judge",
            choices=["allow", "mask", "deny"],
            timeout_seconds=5,
        )
    finally:
        set_interrupt(False)

    assert choice == "mask", (
        "out-of-band wait must resolve from the real notify/resolve exchange, "
        "not misfire 'deny' off a stale/recycled thread-ident interrupt bit"
    )
