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
