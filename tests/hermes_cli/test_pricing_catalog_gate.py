"""Regression tests for the dashboard `_profile_scope` deadlock fix, levers
(b) and (c) (wedge #2, 2026-07-18):

(b) `_urlopen_model_catalog_request` clamps any caller-supplied timeout to a
    hard, short ceiling (`MODEL_CATALOG_HARD_TIMEOUT`) so an unreachable /
    firewalled catalog host can never tie up a dashboard worker thread
    indefinitely -- see docs/superpowers/plans/2026-07-18-dashboard-profile-
    scope-deadlock-plan.md (profile repo) Task 4.
(c) `get_pricing_for_provider` honors the `model_catalog.enabled` master
    switch: config-off means zero network for the pricing fetch (Task 5).
"""

from __future__ import annotations

import urllib.request

import pytest

from hermes_cli import models


class _DummyResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_catalog_request_clamps_timeout(monkeypatch):
    captured = {}

    def _fake_open(request, *, timeout):
        captured["timeout"] = timeout
        return _DummyResp()

    monkeypatch.setattr(models, "open_credentialed_url", _fake_open, raising=True)
    req = urllib.request.Request("https://example.invalid/catalog.json")
    with models._urlopen_model_catalog_request(req, timeout=999) as _:
        pass
    assert captured["timeout"] <= models.MODEL_CATALOG_HARD_TIMEOUT


def test_catalog_request_leaves_short_timeout_alone(monkeypatch):
    """A caller-supplied timeout already under the ceiling must pass through
    unchanged (the clamp is a ceiling, not a floor/override)."""
    captured = {}

    def _fake_open(request, *, timeout):
        captured["timeout"] = timeout
        return _DummyResp()

    monkeypatch.setattr(models, "open_credentialed_url", _fake_open, raising=True)
    req = urllib.request.Request("https://example.invalid/catalog.json")
    short_timeout = min(1.0, models.MODEL_CATALOG_HARD_TIMEOUT / 2)
    with models._urlopen_model_catalog_request(req, timeout=short_timeout) as _:
        pass
    assert captured["timeout"] == short_timeout


def test_pricing_skips_network_when_catalog_disabled(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.model_catalog._load_catalog_config",
        lambda: {"enabled": False, "url": "x", "ttl_hours": 1, "providers": {}},
        raising=True,
    )
    called = {"net": False}

    def _boom(*a, **k):
        called["net"] = True
        raise AssertionError("network must not be touched when catalog disabled")

    monkeypatch.setattr(models, "_urlopen_model_catalog_request", _boom, raising=True)
    result = models.get_pricing_for_provider("openrouter")
    assert result == {}
    assert called["net"] is False


def test_pricing_still_fetches_when_catalog_enabled(monkeypatch):
    """Sanity companion to the disabled-gate test: the gate must not
    accidentally short-circuit pricing when the switch is ON (fail-open
    correctness, not just fail-open on error)."""
    monkeypatch.setattr(
        "hermes_cli.model_catalog._load_catalog_config",
        lambda: {"enabled": True, "url": "x", "ttl_hours": 1, "providers": {}},
        raising=True,
    )
    called = {"n": 0}

    def _fake_fetch(**kwargs):
        called["n"] += 1
        return {"some/model": {"prompt": "0.000001", "completion": "0.000002"}}

    monkeypatch.setattr(models, "fetch_models_with_pricing", _fake_fetch, raising=True)
    result = models.get_pricing_for_provider("openrouter")
    assert called["n"] == 1
    assert result


def test_pricing_gate_fails_open_when_catalog_config_read_errors(monkeypatch):
    """A broken model_catalog config read must not disable pricing entirely
    (fail-open, per docs/refactoring-guidelines.md SS6.5) -- it should fall
    through to the normal pricing fetch."""
    def _boom_config():
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(
        "hermes_cli.model_catalog._load_catalog_config", _boom_config, raising=True
    )
    called = {"n": 0}

    def _fake_fetch(**kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(models, "fetch_models_with_pricing", _fake_fetch, raising=True)
    models.get_pricing_for_provider("openrouter")
    assert called["n"] == 1, "a broken catalog-config read must not disable pricing"
