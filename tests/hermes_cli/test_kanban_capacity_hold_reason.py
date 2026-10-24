"""A full concurrency cap is named in the "dispatcher stuck" reason.

Regression: at a full host cap the tick returned 0 spawns with no reason, so
the gateway warned "dispatcher stuck ... check profile health" with nothing
after "Last tick held back", sending the operator after venv/PATH/credentials.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def running(monkeypatch):
    counts = {"board": 0, "others": 0}
    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: counts["board"])
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board: counts["others"])
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: None)
    monkeypatch.setattr(kbd, "configured_max_in_progress_per_board", lambda: {}, raising=False)
    return counts


def _budget(result, **caps):
    return kbd._tick_spawn_budget(None, result, board="b", **caps)


def test_full_host_cap_is_named(running):
    running.update(board=0, others=3)
    res = kbd.DispatchResult()

    assert _budget(res, max_spawn=None, max_in_progress=3) == (False, None)
    assert kbd.describe_suppression([None, res]) == "max_in_progress=3/3"


def test_full_board_max_spawn_is_named(running):
    running.update(board=2)
    res = kbd.DispatchResult()

    assert _budget(res, max_spawn=2, max_in_progress=None) == (False, None)
    assert kbd.describe_suppression([res]) == "max_spawn=2/2"


def test_free_capacity_names_nothing(running):
    running.update(board=1, others=1)
    res = kbd.DispatchResult()

    assert _budget(res, max_spawn=None, max_in_progress=3) == (True, 1)
    assert kbd.describe_suppression([res]) == ""
