"""``kanban_request_review`` refuses while child cards created BEFORE this run are open.

Pre-created review/QA/release children wait on the parent's ``done``; a ``review`` handoff
never releases them, and the card loops in the review lane instead (t_333c5559). Children the
worker created during its own run are its own business and do not trigger the guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _claim_as_worker(monkeypatch, kb, conn, tid):
    claimed = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return kb.get_run(conn, claimed.current_run_id)


def _set_created_at(conn, tid, ts):
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (ts, tid))
    conn.commit()


def test_request_review_refused_while_precreated_child_is_open(monkeypatch, board):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb = board
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="test-worker")
        early = kb.create_task(conn, title="review", assignee="reviewer", parents=[tid])
        run = _claim_as_worker(monkeypatch, kb, conn, tid)
        _set_created_at(conn, early, run.started_at - 60)  # created_at is whole seconds
        before = kb.get_task(conn, tid)
        before_events = kb.list_events(conn, tid)

    out = json.loads(kt._handle_request_review({"summary": "Ready for review."}))

    assert (f"this task has pre-created child card(s) {early}; "
            "finish with kanban_complete so they can start") in out["error"]
    with kbc.connect() as conn:
        after = kb.get_task(conn, tid)
        assert (after.status, after.assignee, after.current_run_id) == (
            "running", before.assignee, before.current_run_id)
        assert kb.list_events(conn, tid) == before_events


def test_child_created_during_the_run_does_not_trigger_the_guard(monkeypatch, board):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb = board
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="test-worker")
        run = _claim_as_worker(monkeypatch, kb, conn, tid)
        own = kb.create_task(conn, title="follow-up", assignee="test-worker", parents=[tid])
        _set_created_at(conn, own, run.started_at + 5)

    out = json.loads(kt._handle_request_review({"summary": "Ready for review."}))

    assert out["ok"] is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "review"


def test_done_precreated_child_does_not_trigger_the_guard(monkeypatch, board):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb = board
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="test-worker")
        early = kb.create_task(conn, title="review", assignee="reviewer", parents=[tid])
        run = _claim_as_worker(monkeypatch, kb, conn, tid)
        _set_created_at(conn, early, run.started_at - 60)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (early,))
        conn.commit()

    out = json.loads(kt._handle_request_review({"summary": "Ready for review."}))

    assert out["ok"] is True
