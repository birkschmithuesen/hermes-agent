"""Dashboard Specify on a held block-loop card (t_a1deee87): an explicit click is a human decision."""

from __future__ import annotations

import importlib.util
import json as jsonlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_router():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_block_loop_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _block_loop_card(conn):
    tid = kb.create_task(conn, title="Roll out X", body="Planned body.", assignee="worker")
    for _ in range(2):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        if kb.get_task(conn, tid).status == "blocked":
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_dashboard_specify_rewrites_held_card(client, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = jsonlib.dumps({"title": "Clicked", "body": "rewrite"})
    call_llm = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", call_llm)

    body = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={"author": "birk"}).json()

    assert body["ok"] is True, body
    assert call_llm.call_count == 1
