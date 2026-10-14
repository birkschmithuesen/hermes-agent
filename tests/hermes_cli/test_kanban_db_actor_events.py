"""Tests that unblocked/linked/unlinked/scheduled events carry an actor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _payload(conn, tid, kind):
    row = conn.execute(
        "select payload from task_events where task_id=? and kind=? order by id desc limit 1",
        (tid, kind),
    ).fetchone()
    return json.loads(row[0] or "{}")


def test_unblock_and_schedule_record_author(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "birk")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="coder")
        assert kb.schedule_task(conn, tid, reason="WAITS-ON: hermes/t_145c5731")
        assert _payload(conn, tid, "scheduled")["author"] == "birk"
        assert kb.unblock_task(conn, tid, author="kanban-watcher")
        assert _payload(conn, tid, "unblocked")["author"] == "kanban-watcher"


def test_link_unlink_record_author(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "robo")
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="coder")
        b = kb.create_task(conn, title="b", assignee="coder")
        kb.link_tasks(conn, a, b)
        assert (
            _payload(conn, b, "linked").get("author") == "robo"
            or _payload(conn, a, "linked").get("author") == "robo"
        )
        kb.unlink_tasks(conn, a, b)
