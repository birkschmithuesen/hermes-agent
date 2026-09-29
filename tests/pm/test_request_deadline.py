"""A hung PM worker must never block a lazy/on-demand caller forever.

Regression for the observed 9+ minute agent-turn hang: an egress-blocked
download retried inside the worker while ``pm.client._request`` sat in an
unbounded ``readline()``. A non-explicit request (lazy install, e.g. from
``pm.ensure`` reached via a browser-tool or wake-word install) now carries a
bounded deadline; an explicit ``hermes pm install`` keeps the caller's own
patience (unlimited, same as before).

Avoids the ``client``/``isolated_python`` fixtures from ``_fixtures.py``: those
stage a real uv-built Python 3.14 runtime, which itself needs the same
egress this bug is about and is unavailable in a sandboxed test run. The fake
worker below only needs a stdlib interpreter, so ``sys.executable`` (this
process's own Python) is enough.
"""
from __future__ import annotations

import importlib
import sys
import time

import pytest

from pm.package import InstallError


@pytest.fixture
def client(tmp_path, monkeypatch):
    from pm import paths

    client = importlib.import_module("pm.client")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(paths, "lockfile_path", lambda: tmp_path / "lock.json")
    return client


def _never_answers_worker() -> list[str]:
    """A worker stand-in that reads the request but never writes a response."""
    return [sys.executable, "-I", "-c", "import sys, time; sys.stdin.readline(); time.sleep(999)"]


def test_lazy_ensure_raises_within_deadline_when_worker_hangs(client, monkeypatch):
    monkeypatch.setattr(client, "runtime_command", lambda path, **kwargs: _never_answers_worker())
    monkeypatch.setattr(client, "DEFAULT_LAZY_REQUEST_DEADLINE", 1.0)
    # ensure()'s fast path (missing-or-refuse) must actually dispatch to the worker.
    monkeypatch.setattr(client, "_missing_or_refuse", lambda name: ["node"])

    started = time.monotonic()
    with pytest.raises(InstallError, match="timed out after"):
        client.ensure("node")  # non-explicit: lazy/on-demand path
    elapsed = time.monotonic() - started

    # Bounded by the deadline plus normal scheduling slack, nowhere near the
    # minutes-long hang this guards against.
    assert elapsed < 10


def test_explicit_ensure_keeps_no_deadline(client, monkeypatch):
    """An explicit install (`hermes pm install`) is not subject to the bound."""
    calls = []

    def fake_request(operation, arguments, **kwargs):
        calls.append(kwargs.get("deadline"))
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    client.ensure("node", explicit=True)

    assert calls == [None]


def test_lazy_ensure_passes_default_deadline(client, monkeypatch):
    calls = []

    def fake_request(operation, arguments, **kwargs):
        calls.append(kwargs.get("deadline"))
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(client, "_missing_or_refuse", lambda name: ["node"])
    client.ensure("node")  # explicit=False

    assert calls == [client.DEFAULT_LAZY_REQUEST_DEADLINE]
