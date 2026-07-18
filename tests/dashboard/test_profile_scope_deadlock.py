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

Driver note (CORRECTED from an earlier draft of this file -- see git history):
``starlette.testclient.TestClient`` does NOT reproduce true concurrency here.
Proven experimentally: even a totally unrelated ``time.sleep(3)`` sync handler,
hit from a background thread against a shared ``TestClient`` instance, blocks a
concurrent ``/api/status`` call issued from the main thread for the full sleep
duration -- ``TestClient``'s background portal serializes requests regardless
of any lock. The correct driver (per this plan's Task 1 Step 2 fallback) is
``httpx.AsyncClient`` + ``httpx.ASGITransport``: a sync `def` handler dispatched
through the real ASGI app is offloaded to the anyio worker threadpool while the
event loop -- and any concurrently-awaited coroutine on it -- stays free. This
is what actually reproduces (and disproves, once fixed) the wedge.

Hang-simulation note (also corrected from an earlier draft): the "hanging
catalog fetch" is simulated with a plain ``time.sleep(N)``, not a
``threading.Event().wait(timeout=...)`` gated by the test's own assertions.
An Event-based version was tried first and, in this specific
ASGI-threadpool integration, resolved its wait far earlier than expected for
reasons not fully root-caused (isolated reproductions of `Event.wait()` --
including inside a real anyio worker thread via `anyio.to_thread.run_sync`
-- behaved correctly in every direct test). A deterministic `time.sleep()`
has no Condition-variable/notify machinery to misbehave and was verified
directly (temporarily reverting the web_server.py fix and instrumenting both
`get_config`'s and `get_model_options`'s scope entry/exit) to reproduce the
full multi-second wedge exactly as the root-cause writeup describes:
`get_config` blocked for the whole duration the worker held the lock.

Hermeticity note: ``get_model_options`` -> ``build_models_payload`` ->
``list_authenticated_providers`` unconditionally (regardless of the
``pricing``/``capabilities`` flags) calls ``get_curated_nous_model_ids()`` ->
``hermes_cli.model_catalog.get_curated_nous_models()``, which does a REAL
``urllib`` fetch (``model_catalog.py``'s own ``DEFAULT_FETCH_TIMEOUT=8.0``,
still long enough to multiply across address families the same way this
feat's root-cause writeup describes) plus ``agent.models_dev.fetch_models_dev()``
(a real ``requests.get(..., timeout=15)``). Both are pre-existing, unrelated to
this feat's scope (they live in ``model_catalog.py`` / ``agent/models_dev.py``,
not the ``_profile_scope`` RLock or ``models.py``'s pricing fetch), so this
harness stubs the whole provider-listing seam (``list_authenticated_providers``)
to a small synthetic row instead of trying to disable each real network path
individually -- deterministic, fast, and it still exercises the pricing-fetch
enrichment loop (`_apply_pricing`) with a provider that has models, which is
exactly the hook this suite needs. Note `_apply_pricing` may enrich more than
one row per request (e.g. a built-in "moa" pseudo-provider row gets appended
regardless of the synthetic list), so the mocked pricing fetch can run more
than once sequentially per `/api/model/options` call -- tests must only
assert about `/api/status`/`/api/config` responsiveness, never about the
model-options call's own total duration.
"""

import asyncio
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server

_SYNTHETIC_PROVIDER_ROWS = [
    {
        "slug": "openrouter",
        "name": "OpenRouter",
        "is_current": True,
        "is_user_defined": False,
        "models": ["anthropic/claude-sonnet-5"],
        "total_models": 1,
        "source": "built-in",
    }
]

# How long the mocked pricing fetch takes per provider row it's asked about.
# Long enough to comfortably exceed the responsiveness assertions' 3.0s
# threshold and the 0.5s "let it enter the fetch" settle time below; short
# enough to keep the suite fast (each concurrency test awaits the full
# model-options call in its `finally`, so wall time is roughly
# HANG_SECONDS * rows_enriched).
_HANG_SECONDS = 2.0


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Stub every unconditional real-network seam `get_model_options` walks
    through so no test in this file can reach the real network (see module
    docstring's Hermeticity note). Two independent seams found by tracing an
    actual hang with `faulthandler.dump_traceback_later` in this worktree:

    1. `list_authenticated_providers` (hermes_cli/model_switch.py) always
       calls `get_curated_nous_model_ids()` -> `hermes_cli.model_catalog`'s
       `get_curated_nous_models()`, a real `urllib` fetch -- regardless of
       the `pricing`/`capabilities` flags. Replaced wholesale with a small
       synthetic row (still has a model, so the pricing-enrichment loop this
       suite needs still runs).
    2. `_apply_capabilities` (hermes_cli/inventory.py, run whenever
       `capabilities=True`, which `get_model_options` always passes) calls
       `agent.models_dev.get_model_capabilities`, which calls
       `fetch_models_dev()` -- a real `requests.get(..., timeout=15)`.
       Neutralized directly since it's a shared module-level function (both
       the internal call inside `_get_provider_models` and any external
       caller resolve the same patched global).

    Both are pre-existing, unrelated to this feat's scope (model_catalog.py /
    agent/models_dev.py belong to the sibling `feat/model-picker-offline-
    catalogs`, not the `_profile_scope` RLock or `models.py`'s pricing fetch
    this feat fixes) -- neutralizing them here just keeps this harness fast
    and deterministic regardless of sandbox egress.
    """
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda *a, **k: [dict(row) for row in _SYNTHETIC_PROVIDER_ROWS],
        raising=True,
    )
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda force_refresh=False: {},
        raising=True,
    )


@pytest.fixture
def client():
    """Loopback-mode dashboard client, authenticated via the ephemeral
    session token (same pattern as tests/hermes_cli/test_web_server_*.py)."""
    c = TestClient(web_server.app)
    c.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return c


def _auth_headers():
    return {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}


def _install_hanging_pricing(monkeypatch, hang_seconds: float = _HANG_SECONDS):
    """Make the pricing catalog fetch take `hang_seconds` (simulating a slow
    outbound network fetch), via a deterministic `time.sleep` -- see the
    module docstring's Hang-simulation note for why this replaced an earlier
    threading.Event-based design.
    """
    def _hang(*a, **k):
        time.sleep(hang_seconds)
        return {}
    # Patch at the seam the picker uses for live pricing.
    monkeypatch.setattr("hermes_cli.models.get_pricing_for_provider", _hang, raising=True)


@pytest.mark.asyncio
async def test_status_stays_responsive_while_model_options_fetch_hangs(monkeypatch):
    """`/api/status` and `/api/config` must not wedge while /api/model/options
    is blocked on a hanging catalog fetch.

    Uses httpx.AsyncClient + ASGITransport (see module docstring) so the sync
    `get_model_options` handler genuinely runs on the anyio worker threadpool
    concurrently with the async loop-side endpoints -- a plain TestClient
    driver here would serialize and mask the bug regardless of the fix.
    """
    _install_hanging_pricing(monkeypatch)

    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=_auth_headers()
    ) as client:
        # Warm up imports/caches OUTSIDE the timed section so first-import
        # cost never gets misread as "wedged".
        warm = await client.get("/api/status")
        assert warm.status_code == 200

        hang_task = asyncio.create_task(client.get("/api/model/options"))
        await asyncio.sleep(0.5)  # let it enter the fetch on its worker thread

        try:
            t0 = time.monotonic()
            r = await client.get("/api/status")
            elapsed = time.monotonic() - t0
            assert elapsed < 3.0, f"dashboard wedged (status blocked for {elapsed:.1f}s)"
            assert r.status_code == 200

            t1 = time.monotonic()
            r2 = await client.get("/api/config")
            elapsed2 = time.monotonic() - t1
            assert elapsed2 < 3.0, f"get_config deadlocked (blocked for {elapsed2:.1f}s)"
            assert r2.status_code == 200
        finally:
            await hang_task


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
        self._real = threading.RLock()
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
    `_config_profile_scope`, never touching `_SKILLS_PROFILE_LOCK`.

    No concurrency involved here (single request, single assertion), so the
    plain sync TestClient is fine -- the serialization pitfall documented in
    the module docstring only matters for the two concurrency-sensitive
    tests above/below.
    """
    spy = _SpyRLock()
    monkeypatch.setattr(web_server, "_SKILLS_PROFILE_LOCK", spy)
    r = client.get("/api/config")
    assert r.status_code == 200
    assert spy.acquired is False, "get_config still acquires the skills RLock"


@pytest.mark.asyncio
async def test_model_options_hang_does_not_hold_rlock(monkeypatch):
    """The /api/model/options network fetch must run with the skills RLock
    free, so a hanging fetch cannot block a concurrent skills/config
    request that needs that lock.

    Probing must happen from a DIFFERENT thread than the one running the
    fetch: ``threading.RLock`` is reentrant, so a non-blocking `acquire()`
    called from the *same* thread that already holds it always succeeds
    regardless of the lock's actual contention state (that pitfall produced
    a false GREEN on the unfixed tree during development of this test --
    the probe must run on the main (event-loop) thread, cross-thread from
    the hung worker, to actually detect the held lock). Also needs the
    ASGITransport driver for the same reason as the responsiveness test
    above: a plain TestClient serializes and would make /api/status "pass"
    only because it was blocked until the hang released, not because the
    lock was actually free.
    """
    _install_hanging_pricing(monkeypatch)

    transport = httpx.ASGITransport(app=web_server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=_auth_headers()
    ) as client:
        warm = await client.get("/api/status")
        assert warm.status_code == 200

        hang_task = asyncio.create_task(client.get("/api/model/options"))
        await asyncio.sleep(0.5)  # let the fetch enter the hang on its worker thread

        try:
            r = await client.get("/api/status")
            assert r.status_code == 200

            # Cross-thread probe: if the fetch (running on another thread) is
            # holding _SKILLS_PROFILE_LOCK, this non-blocking acquire attempt
            # from THIS (event-loop) thread must fail.
            acquired = web_server._SKILLS_PROFILE_LOCK.acquire(blocking=False)
            if acquired:
                web_server._SKILLS_PROFILE_LOCK.release()
            assert acquired, (
                "skills RLock was held (by another thread) while the "
                "model-options catalog fetch was in flight"
            )
        finally:
            await hang_task
