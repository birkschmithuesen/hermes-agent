"""Tests for kanban_show's slim default and its full=true escape hatch.

Fixtures are synthetic: an isolated HERMES_HOME with a task built by
hermes_cli.kanban_db directly. No real board data is read here — the
size acceptance against the frozen board copy is a manual measurement,
not a test (see docs/superpowers/plans/2026-09-25-kanban-show-slim.md).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """A claimed task owned by this process, on a throwaway board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker",
                             body="BODY-SENTINEL body text")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def test_full_true_keeps_task_body_and_every_legacy_key(worker_env):
    """full=true is the pre-change payload: same keys, same order, body present."""
    from tools import kanban_tools as kt
    d = json.loads(kt._handle_show({"full": True}))
    assert list(d) == ["task", "parents", "unsatisfied_parents", "children",
                       "comments", "events", "runs", "worker_context"]
    assert d["task"]["body"] == "BODY-SENTINEL body text"
    assert "slim" not in d


def test_full_accepts_the_string_form_like_every_other_bool_arg(worker_env):
    from tools import kanban_tools as kt
    assert "body" in json.loads(kt._handle_show({"full": "true"}))["task"]
    assert "body" not in json.loads(kt._handle_show({"full": "false"}))["task"]


def test_full_is_declared_in_the_model_facing_schema():
    from tools.kanban_tools_schemas import KANBAN_SHOW_SCHEMA
    props = KANBAN_SHOW_SCHEMA["parameters"]["properties"]
    assert props["full"]["type"] == "boolean"
    assert "full" not in KANBAN_SHOW_SCHEMA["parameters"]["required"]


# ---------------------------------------------------------------------------
# _slim_runs — pure, no DB
# ---------------------------------------------------------------------------

def _run(rid, outcome, summary=None, ended=True):
    return {"id": rid, "profile": "p", "status": outcome or "running",
            "outcome": outcome, "summary": summary, "error": None,
            "metadata": None, "started_at": rid, "ended_at": rid if ended else None}


def test_slim_runs_collapses_rate_limited_into_one_line():
    from tools.kanban_tools import _slim_runs
    runs = [_run(i, "rate_limited") for i in range(1, 25)] + [_run(99, "completed", "done")]
    kept, note = _slim_runs(runs)
    assert [r["id"] for r in kept] == [99]
    assert note == "24 Laeufe rate_limited, 0 Calls"


def test_slim_runs_drops_runs_without_an_outcome():
    from tools.kanban_tools import _slim_runs
    kept, note = _slim_runs([_run(1, None, ended=False), _run(2, "completed", "done")])
    assert [r["id"] for r in kept] == [2]
    assert note is None


def test_slim_runs_keeps_at_most_three():
    from tools.kanban_tools import _slim_runs
    runs = [_run(i, "crashed") for i in range(1, 8)]
    kept, _ = _slim_runs(runs)
    assert [r["id"] for r in kept] == [5, 6, 7]


def test_slim_runs_never_loses_the_last_finished_run_with_a_summary():
    """The summary-carrying run is old enough to fall out of the last-3 window;
    it must be pulled back in — it is the handoff a retry actually needs."""
    from tools.kanban_tools import _slim_runs
    runs = [_run(1, "completed", "THE HANDOFF")] + [_run(i, "crashed") for i in range(2, 9)]
    kept, _ = _slim_runs(runs)
    assert [r["id"] for r in kept] == [1, 7, 8]


def test_slim_runs_falls_back_to_the_newest_finished_run_when_none_has_a_summary():
    from tools.kanban_tools import _slim_runs
    runs = [_run(1, "crashed"), _run(2, "crashed"), _run(3, "blocked", "  ")]
    kept, _ = _slim_runs(runs)
    assert [r["id"] for r in kept] == [1, 2, 3]


def test_slim_runs_on_an_empty_history():
    from tools.kanban_tools import _slim_runs
    assert _slim_runs([]) == ([], None)


# ---------------------------------------------------------------------------
# _slim_events — pure, no DB
# ---------------------------------------------------------------------------

def _ev(kind, at):
    return {"kind": kind, "payload": None, "created_at": at, "run_id": None}


def test_slim_events_drops_heartbeat_and_other_noise():
    from tools.kanban_tools import _slim_events
    events = [_ev("heartbeat", 1), _ev("spawned", 2), _ev("claimed", 3),
              _ev("respawn_guarded", 4), _ev("commented", 5), _ev("blocked", 6)]
    assert [e["kind"] for e in _slim_events(events)] == ["blocked"]


def test_slim_events_keeps_all_five_lifecycle_kinds():
    from tools.kanban_tools import _slim_events
    kinds = ["blocked", "unblocked", "review_requested", "changes_requested", "completed"]
    events = [_ev(k, i) for i, k in enumerate(kinds)]
    assert [e["kind"] for e in _slim_events(events)] == kinds


def test_slim_events_keeps_the_newest_ten():
    from tools.kanban_tools import _slim_events
    events = [_ev("blocked" if i % 2 else "unblocked", i) for i in range(30)]
    kept = _slim_events(events)
    assert len(kept) == 10
    assert [e["created_at"] for e in kept] == list(range(20, 30))


def test_slim_events_on_a_card_with_nothing_but_heartbeats():
    from tools.kanban_tools import _slim_events
    assert _slim_events([_ev("heartbeat", i) for i in range(200)]) == []
