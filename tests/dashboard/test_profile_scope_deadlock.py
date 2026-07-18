"""Regression harness for the dashboard `_profile_scope` RLock deadlock
(wedge #2, 2026-07-18).

Root cause (see docs/superpowers/plans/2026-07-18-dashboard-profile-scope-deadlock-plan.md
in the profile repo): `get_model_options` is a sync `def` handler that runs its
network-enrichment payload build (`build_models_payload` -> ... -> live pricing
fetch) *inside* `with _profile_scope(profile):`, which acquires the
process-global `_SKILLS_PROFILE_LOCK = threading.RLock()` and holds it for the
whole block. If that network fetch hangs (e.g. a firewalled host with no
effective connect timeout), the worker thread running `get_model_options`
holds the RLock indefinitely. Meanwhile `get_config` (an `async def` handler)
does `with _profile_scope(profile):` *synchronously on the event-loop thread*
-- blocking on that same RLock freezes the entire asyncio loop, so even
`/api/status` (a pure loop-side, lock-free handler) stops responding.

Driver note: `starlette.testclient.TestClient` runs its own background
event-loop thread and dispatches each `.get()` synchronously against a shared
httpx.Client, but a sync `def` FastAPI handler is offloaded to the AnyIO
worker threadpool -- so a *second* TestClient call issued concurrently from a
different Python thread is still processed by the (separate) event-loop
thread while the first call's handler sits blocked on the threadpool. This
reproduces the wedge without a real network and without needing
httpx.AsyncClient/ASGITransport.
"""

import threading
import time

import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def client():
    return TestClient(web_server.app)


def _install_hanging_pricing(monkeypatch, release_evt, held_marker=None):
    """Make the pricing catalog fetch block until release_evt is set.

    If `held_marker` (a dict) is given, records whether the skills RLock is
    owned by *some* thread at the moment the hang starts -- best-effort,
    since `threading.RLock` doesn't expose ownership publicly outside CPython
    internals; we instead probe via a non-blocking `acquire` attempt.
    """
    def _hang(*a, **k):
        if held_marker is not None:
            acquired = web_server._SKILLS_PROFILE_LOCK.acquire(blocking=False)
            if acquired:
                web_server._SKILLS_PROFILE_LOCK.release()
            held_marker["val"] = not acquired
        release_evt.wait(timeout=30)
        return {}
    # Patch at the seam the picker uses for live pricing.
    monkeypatch.setattr("hermes_cli.models.get_pricing_for_provider", _hang, raising=True)


def test_status_stays_responsive_while_model_options_fetch_hangs(client, monkeypatch):
    """`/api/status` and `/api/config` must not wedge while /api/model/options
    is blocked on a hanging catalog fetch (reproduces wedge #2 on the
    unfixed tree via a TestClient background-thread driver -- see module
    docstring)."""
    release = threading.Event()
    _install_hanging_pricing(monkeypatch, release)

    # Kick /api/model/options on a background thread so its fetch is "in flight".
    hang_thread = threading.Thread(
        target=lambda: client.get("/api/model/options"), daemon=True
    )
    hang_thread.start()
    time.sleep(0.5)  # let it enter the fetch

    # The loop-side endpoints MUST stay responsive.
    t0 = time.monotonic()
    r = client.get("/api/status")
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"dashboard wedged (status blocked for {elapsed:.1f}s)"
    assert r.status_code == 200

    r2 = client.get("/api/config")
    assert r2.status_code == 200, "get_config deadlocked on the RLock"

    release.set()
    hang_thread.join(timeout=5)


def test_get_config_does_not_take_skills_rlock(client, monkeypatch):
    """`get_config` is a pure config-read; it must use the lock-free
    `_config_profile_scope`, never touching `_SKILLS_PROFILE_LOCK`."""
    held = {"during": False}
    real_acquire = web_server._SKILLS_PROFILE_LOCK.acquire

    def _spy_acquire(*a, **k):
        held["during"] = True
        return real_acquire(*a, **k)

    monkeypatch.setattr(web_server._SKILLS_PROFILE_LOCK, "acquire", _spy_acquire)
    r = client.get("/api/config")
    assert r.status_code == 200
    assert held["during"] is False, "get_config still acquires the skills RLock"


def test_model_options_hang_does_not_hold_rlock(client, monkeypatch):
    """The /api/model/options network fetch must run with the skills RLock
    free, so a hanging fetch cannot block a concurrent skills/config
    request that needs that lock."""
    held_during_fetch = {"val": None}
    release = threading.Event()
    _install_hanging_pricing(monkeypatch, release, held_marker=held_during_fetch)

    t = threading.Thread(target=lambda: client.get("/api/model/options"), daemon=True)
    t.start()
    time.sleep(0.5)
    assert client.get("/api/status").status_code == 200
    release.set()
    t.join(timeout=5)

    # The skills RLock must NOT be owned while the network fetch runs.
    assert held_during_fetch["val"] in (False, None), (
        "skills RLock was held while the model-options catalog fetch was in flight"
    )
