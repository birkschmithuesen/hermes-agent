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
    """Loopback-mode dashboard client, authenticated via the ephemeral
    session token (same pattern as tests/hermes_cli/test_web_server_*.py)."""
    c = TestClient(web_server.app)
    c.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return c


def _install_hanging_pricing(monkeypatch, release_evt):
    """Make the pricing catalog fetch block until release_evt is set."""
    def _hang(*a, **k):
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


class _SpyRLock:
    """Records whether it was ever acquired, while delegating to a real RLock.

    `threading.RLock()` instances are the C-implemented `_thread.RLock` type,
    whose `acquire`/`release` attributes are read-only -- `monkeypatch.setattr`
    on the *instance* raises `AttributeError: attribute 'acquire' is
    read-only`. Substituting the whole module-level `_SKILLS_PROFILE_LOCK`
    name with this wrapper (rather than patching an attribute on the real
    lock) achieves the same spy behavior: `_profile_scope`/`_config_profile_scope`
    look up `_SKILLS_PROFILE_LOCK` by module-global name on each call, so the
    swap is transparent to callers, including the `with _SKILLS_PROFILE_LOCK:`
    usage inside `_profile_scope`.
    """

    def __init__(self):
        self._real = __import__("threading").RLock()
        self.acquired = False

    def acquire(self, *a, **k):
        self.acquired = True
        return self._real.acquire(*a, **k)

    def release(self, *a, **k):
        return self._real.release(*a, **k)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def test_get_config_does_not_take_skills_rlock(client, monkeypatch):
    """`get_config` is a pure config-read; it must use the lock-free
    `_config_profile_scope`, never touching `_SKILLS_PROFILE_LOCK`."""
    spy = _SpyRLock()
    monkeypatch.setattr(web_server, "_SKILLS_PROFILE_LOCK", spy)
    r = client.get("/api/config")
    assert r.status_code == 200
    assert spy.acquired is False, "get_config still acquires the skills RLock"


def test_model_options_hang_does_not_hold_rlock(client, monkeypatch):
    """The /api/model/options network fetch must run with the skills RLock
    free, so a hanging fetch cannot block a concurrent skills/config
    request that needs that lock.

    Probing must happen from a DIFFERENT thread than the one running the
    fetch: ``threading.RLock`` is reentrant, so a non-blocking `acquire()`
    called from the *same* thread that already holds it always succeeds
    regardless of the lock's actual contention state (that pitfall produced
    a false GREEN on the unfixed tree during development of this test --
    the probe must run on the main thread, cross-thread from the hung
    worker, to actually detect the held lock).
    """
    release = threading.Event()
    _install_hanging_pricing(monkeypatch, release)

    t = threading.Thread(target=lambda: client.get("/api/model/options"), daemon=True)
    t.start()
    time.sleep(0.5)  # let the fetch enter the hang on its worker thread
    try:
        assert client.get("/api/status").status_code == 200

        # Cross-thread probe: if the fetch (running on another thread) is
        # holding _SKILLS_PROFILE_LOCK, this non-blocking acquire attempt
        # from the main thread must fail.
        acquired = web_server._SKILLS_PROFILE_LOCK.acquire(blocking=False)
        if acquired:
            web_server._SKILLS_PROFILE_LOCK.release()
        assert acquired, (
            "skills RLock was held (by another thread) while the "
            "model-options catalog fetch was in flight"
        )
    finally:
        release.set()
        t.join(timeout=5)
