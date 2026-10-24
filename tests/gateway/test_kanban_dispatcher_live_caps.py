"""The embedded dispatcher re-reads its concurrency caps every tick.

Regression: the caps were snapshotted at gateway boot, so raising
``kanban.max_in_progress`` left the gateway at its old host cap (0 spawns,
"dispatcher stuck") while ``hermes kanban dispatch --dry-run`` read the new
value and proposed spawns.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db_dispatch as kbd


def _dispatcher(max_in_progress=3, per_profile=3):
    settings = kwd._DispatcherSettings(60.0, None, max_in_progress, 2, 0, True, None, per_profile)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


@pytest.fixture
def kanban_cfg(monkeypatch):
    cfg: dict = {"kanban": {}}
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    return cfg["kanban"]


def test_cap_raise_applies_on_next_tick_without_restart(kanban_cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb: ["a"])
    disp = _dispatcher(3, 3)
    monkeypatch.setattr(disp, "tick_once_for_board", lambda slug: seen.append(
        (disp.settings.max_in_progress, disp.settings.max_in_progress_per_profile)))

    kanban_cfg.update(max_in_progress=5, max_in_progress_per_profile=4)
    disp.tick_once()

    assert seen == [(5, 4)]


def test_removed_caps_fall_back_like_the_cli(kanban_cfg, monkeypatch):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 7)
    disp = _dispatcher(3, 3)

    disp.refresh_caps()

    assert disp.settings.max_in_progress == 7
    assert disp.settings.max_in_progress_per_profile is None


def test_invalid_values_are_ignored_not_applied(kanban_cfg, monkeypatch):
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: None)
    kanban_cfg.update(max_in_progress="lots", max_in_progress_per_profile=0)
    disp = _dispatcher(3, 3)

    disp.refresh_caps()

    assert disp.settings.max_in_progress is None
    assert disp.settings.max_in_progress_per_profile is None


def test_unreadable_config_keeps_current_caps(monkeypatch):
    import hermes_cli.config as config

    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr(config, "load_config_readonly", boom)
    disp = _dispatcher(3, 2)

    disp.refresh_caps()

    assert (disp.settings.max_in_progress, disp.settings.max_in_progress_per_profile) == (3, 2)
