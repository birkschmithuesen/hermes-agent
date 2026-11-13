"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from gateway.kanban_watchers_common import _resolve_auto_decompose_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Real default board under a temp HERMES_HOME; the tick sees only that board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: [kb.DEFAULT_BOARD])
    return home


def _dispatcher():
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD=kb.DEFAULT_BOARD), settings)


def _llm(content: str):
    """Stub the aux LLM the decomposer calls; the mock records whether it was billed."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return patch("agent.auxiliary_client.call_llm", return_value=resp)


_REWRITE = json.dumps({"fanout": False, "rationale": "one unit", "title": "LLM title", "body": "LLM rewritten body"})


def _loop_park(conn, *, body: str = "binding body from the commissioner") -> str:
    """Drive a card into triage the way production does: block needs_input, unblock, re-block."""
    tid = kb.create_task(conn, title="needs the owner's answer", body=body, assignee="worker")
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_disabled_when_flag_false():
    enabled, _per_tick, _authors = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_tick_leaves_a_loop_parked_card_in_triage(board):
    """D1/D2 (t_95a7e463): the unblock-loop breaker parked the card in triage; the auto-decompose tick
    must neither bill the LLM for it nor promote it (it re-spawned a worker into the same wall 13x)."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.body) == ("triage", "binding body from the commissioner")
    assert llm.call_count == 0


def test_tick_still_decomposes_a_fresh_triage_card(board):
    """Regression control (D2): a card created in triage is still auto-specified by the same tick."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", assignee="worker", triage=True)
    with _llm(_REWRITE) as llm:
        assert _dispatcher().auto_decompose_tick(5) == 1
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.body) == ("ready", "LLM rewritten body")
    assert llm.call_count == 1


def test_worker_comment_does_not_release_a_loop_parked_card(board):
    """D3: the card's own worker (assignee) commenting is not an answer from the commissioner."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="worker", body="still waiting for the variant")
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
    assert llm.call_count == 0


def test_foreign_comment_releases_the_card_as_is(board):
    """D3: a comment by someone else promotes the card on the next tick WITHOUT an LLM rewrite."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="birk", body="variant B")
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.title, task.body) == (
        "ready", "needs the owner's answer", "binding body from the commissioner")
    assert llm.call_count == 0


def test_comment_before_the_loop_detection_does_not_release(board):
    """D3: only comments newer than the parking event count."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs the owner's answer", body="b", assignee="worker")
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="?", kind="needs_input")
        kb.add_comment(conn, tid, author="birk", body="first answer")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_release_authors_narrow_who_may_wake_the_card(board):
    """D3 narrowing: with kanban.loop_breaker_release_authors set, an automation comment does not release."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="factory-lead", body="parking note")
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5, ("birk",))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        kb.add_comment(conn, tid, author="birk", body="variant B")
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5, ("birk",))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize(("raw", "expected"), [
    (None, ()), ([], ()), ("", ()), (["birk", " ", 3], ("birk",)), ("birk, robo", ("birk", "robo")),
])
def test_release_authors_setting_is_parsed(raw, expected):
    _enabled, _per_tick, authors = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"loop_breaker_release_authors": raw}}
    )
    assert authors == expected


def test_unreadable_config_fails_safe_with_no_release_authors():
    def boom():
        raise OSError("unreadable")

    assert _resolve_auto_decompose_settings(boom) == (False, 3, ())
