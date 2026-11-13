"""Auto-decompose tick vs. block-loop triage cards (t_a1deee87).

Regression for t_72559b11: a card the unblock-loop breaker routed to triage was
rewritten by the aux LLM and promoted seven times in 17 minutes while its
question stayed unanswered. The tick must hold such a card until someone other
than the blocker comments, then promote it unchanged.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb_mod: ["default"])
    kb.init_db()
    return home


@pytest.fixture
def call_llm():
    """Aux LLM stub; tests assert on ``call_count``."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = jsonlib.dumps(
        {"fanout": False, "title": "LLM rewrite", "body": "LLM rewritten body"})
    stub = MagicMock(return_value=resp)
    with patch("agent.auxiliary_client.call_llm", stub):
        yield stub


def _tick() -> int:
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings).auto_decompose_tick(5)


def _claim(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_loop_card(conn) -> str:
    """Two identical needs_input blocks with no other comment -> triage + block_loop_detected."""
    tid = kb.create_task(conn, title="Roll out X", body="Planned body.\n- step 1", assignee="worker")
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _snapshot(tid):
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    return task, events


def test_tick_holds_block_loop_card_without_reply(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    before, _ = _snapshot(tid)
    _tick()
    after, events = _snapshot(tid)
    assert call_llm.call_count == 0
    assert after.status == "triage"
    assert (after.title, after.body) == (before.title, before.body)
    assert not [e for e in events if e.kind == "specified"]


def test_tick_ignores_assignee_comment(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "worker", "still need B1 or B2")
    _tick()
    after, events = _snapshot(tid)
    assert call_llm.call_count == 0
    assert after.status == "triage"
    assert not [e for e in events if e.kind == "specified"]


def test_tick_releases_replied_card_unchanged(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    before, _ = _snapshot(tid)
    _tick()  # held
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "birk", "B1.")
    assert _tick() == 1
    after, events = _snapshot(tid)
    specified = [e for e in events if e.kind == "specified"]
    assert call_llm.call_count == 0
    assert after.status in {"todo", "ready"}
    assert (after.title, after.body) == (before.title, before.body)
    assert len(specified) == 1
    assert not (specified[0].payload or {}).get("changed_fields")


def test_tick_holds_again_after_third_identical_block(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", "B1.")
    assert _tick() == 1
    with kbc.connect_closing() as conn:
        first_loop = max(e.id for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected")
        _claim(conn, tid)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    after, events = _snapshot(tid)
    assert after.status == "triage"
    assert max(e.id for e in events if e.kind == "block_loop_detected") > first_loop
    _tick()
    held, events = _snapshot(tid)
    assert held.status == "triage"
    assert call_llm.call_count == 0
    assert len([e for e in events if e.kind == "specified"]) == 1


def test_tick_still_decomposes_plain_triage_card(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="raw idea", triage=True, assignee="worker")
    assert _tick() == 1
    after, _ = _snapshot(tid)
    assert call_llm.call_count == 1
    assert after.status in {"todo", "ready"}
    assert after.title == "LLM rewrite"
