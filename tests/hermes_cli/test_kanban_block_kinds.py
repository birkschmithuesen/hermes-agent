"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking —
  unless no parent is open, in which case the wait can never be satisfied
  and the block is recorded as ``needs_input`` (sticky, loop-counted).
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_block_loop as kbl
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t", parents=(), assignee="worker"):
    """Create a task (linked under ``parents`` first) and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    for parent in parents:
        kb.link_tasks(conn, parent_id=parent, child_id=tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Block-loop hold (t_a1deee87): a looped card waits for a reply
# ---------------------------------------------------------------------------


def _block_loop_card(conn, title="t"):
    """Two identical needs_input blocks -> triage with a block_loop_detected event."""
    tid = _running_task(conn, title=title)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_hold_state_awaiting_reply_after_block_loop(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_ignores_assignee_and_auto_decomposer_comments(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, " Worker ", "still need B1 or B2")  # assignee, case/space-insensitive
        kb.add_comment(conn, tid, "auto-decomposer", "audit")
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_comment_before_the_loop_is_not_a_reply(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        kb.add_comment(conn, tid, "birk", "answered before the second block")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_replied_after_foreign_comment(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", "B1.")
        assert kbl.block_loop_hold_state(conn, tid) == "replied"


def test_hold_state_none_for_plain_triage_and_non_triage(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        raw = kb.create_task(conn, title="raw idea", triage=True)
        running = _running_task(conn)
        assert kbl.block_loop_hold_state(conn, raw) == "none"
        assert kbl.block_loop_hold_state(conn, running) == "none"
        assert kbl.block_loop_hold_state(conn, "t_missing") == "none"


@pytest.mark.parametrize("exit_kind", ["specified", "promoted", "decomposed", "status"])
def test_hold_state_none_once_card_left_triage_after_loop(kanban_home: Path, exit_kind: str) -> None:
    """A later exit event means the current triage stay has another cause (human drag-back)."""
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, exit_kind, None)
        assert kbl.block_loop_hold_state(conn, tid) == "none"


def test_release_promotes_replied_card_unchanged(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn, title="Roll out X")
        before = kb.get_task(conn, tid)
        kb.add_comment(conn, tid, "birk", "B1.")
        assert kbl.release_block_loop_hold(conn, tid) == "birk"
        after = kb.get_task(conn, tid)
        specified = [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
        comments = [(c.author, c.body) for c in kb.list_comments(conn, tid)]
    assert after.status in {"todo", "ready"}
    assert (after.title, after.body, after.assignee) == (before.title, before.body, before.assignee)
    assert len(specified) == 1
    assert not (specified[0].payload or {}).get("changed_fields")
    assert comments[-1] == (
        "auto-decomposer",
        "Released from block-loop hold after reply by birk; title and body unchanged, promoted to todo.",
    )


def test_release_is_a_noop_while_awaiting_reply(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        assert kbl.release_block_loop_hold(conn, tid) is None
        assert kb.get_task(conn, tid).status == "triage"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "specified"]


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(
            conn,
            parent_id=parent,
            child_id=child,
            expected_child_run_id=kb.get_task(conn, child).current_run_id,
        )
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_block_with_terminal_parents_parks_then_escalates(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``dependency`` block whose parents are all terminal can never be
    satisfied by ``recompute_ready``: it must park in ``blocked`` as
    ``needs_input`` (no ``dependency_wait``, no re-promotion), say so on the
    CLI, and count toward the loop breaker so a re-block after an unblock
    reaches ``triage``."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="already-done-parent", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        # The edge exists before the run: link_tasks refuses to gate a running child retroactively.
        child = _running_task(conn, title="child-of-done", parents=(parent,))

        # `hermes kanban block <child> --kind dependency waiting on upstream`
        args = argparse.Namespace(task_id=child, ids=None, reason=["waiting", "on", "upstream"], kind="dependency")
        assert kanban_cli._cmd_block(args) == 0
        assert "needs_input" in capsys.readouterr().out
        parked = kb.get_task(conn, child)
        assert (parked.status, parked.block_kind, parked.block_recurrences) == ("blocked", "needs_input", 1)
        events = kb.list_events(conn, child)
        assert not [e for e in events if e.kind == "dependency_wait"]
        blocked = [e for e in events if e.kind == "blocked"][-1].payload
        assert (blocked["requested_kind"], blocked["rekind_reason"]) == ("dependency", "no_open_parent")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "blocked"

        # A cron/human unblocks; the worker re-declares the same impossible wait.
        assert kb.unblock_task(conn, child)
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.block_task(conn, child, reason="still waiting", kind="dependency")
        assert kb.get_task(conn, child).status == "triage"
        loop = [e for e in kb.list_events(conn, child) if e.kind == "block_loop_detected"][-1].payload
        assert loop["recurrences"] == kb.BLOCK_RECURRENCE_LIMIT


def test_dependency_block_with_open_parent_stays_parked_across_dispatch_tick(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    """Control: a genuine wait on an incomplete parent parks in ``todo``
    without counting a recurrence, survives a dispatch tick unspawned, and
    resumes once the parent finishes."""
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="open-parent", assignee="alice")
        # Linked while todo (a running child cannot be gated retroactively); the parent then stays
        # open while the child runs — the reopened-parent shape, forced the same way the loop below does.
        child = kb.create_task(conn, title="waiter", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
            assert kb.block_task(conn, child, reason="wait", kind="dependency")
            parked = kb.get_task(conn, child)
            assert (parked.status, parked.block_kind, parked.block_recurrences) == ("todo", "dependency", 0)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert kb.block_task(conn, child, reason="wait", kind="dependency")
        kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert kb.get_task(conn, child).status == "todo"
        assert child not in spawns
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="alice")
        kb.complete_task(conn, parent, result="done")
        res2 = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert child in [row[0] for row in res2.spawned]


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Loop-parked marker (t_95a7e463, D1)
# ---------------------------------------------------------------------------


def _loop_parked(conn, title="t"):
    tid = _running_task(conn, title=title)
    kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_loop_breaker_triage_card_is_marked_parked(kanban_home: Path) -> None:
    from hermes_cli.kanban_db_loop_breaker import is_loop_parked, loop_parked_ids

    with kbc.connect_closing() as conn:
        parked = _loop_parked(conn)
        fresh = kb.create_task(conn, title="idea", triage=True)
        assert is_loop_parked(conn, parked) is True
        assert is_loop_parked(conn, fresh) is False
        assert loop_parked_ids(conn, [parked, fresh]) == {parked}


def test_marker_follows_the_newest_triage_entry(kanban_home: Path) -> None:
    """A human move into triage after the loop breaker makes it an ordinary triage card;
    leaving triage clears the marker; a new loop re-parks it."""
    from hermes_cli.kanban_db_loop_breaker import is_loop_parked

    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "todo"})
        assert is_loop_parked(conn, tid) is False  # not in triage
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "triage", "requested_status": "triage"})
        assert is_loop_parked(conn, tid) is False  # a human dragged it there
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still which variant?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert is_loop_parked(conn, tid) is True


def test_unblock_releases_a_loop_parked_triage_card(kanban_home: Path) -> None:
    """D4: `kanban_unblock` is the commissioner's explicit way out; recurrences survive."""
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        assert kb.unblock_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"]
        # The loop memory survives: the same block again goes straight back to triage.
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"


def test_unblock_refuses_a_fresh_triage_card(kanban_home: Path) -> None:
    """D4: an idea card in triage is not "blocked"; unblock must not promote it past specify."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="idea", triage=True)
        assert kb.unblock_task(conn, tid) is False
        assert kb.get_task(conn, tid).status == "triage"


def test_unblock_of_a_loop_parked_card_regates_on_open_parents(kanban_home: Path) -> None:
    """D4: restored like a blocked card, so an open parent keeps it in todo."""
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        parent = kb.create_task(conn, title="upstream", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "todo"


# ---------------------------------------------------------------------------
# A human reply since the last block resets the loop count (t_c3eda1d6)
# ---------------------------------------------------------------------------

from hermes_cli.kanban_db_loop_breaker import AUTO_DECOMPOSER_AUTHOR  # noqa: E402

_CEILING = 5  # D4: kanban_db_loop_breaker.BLOCK_REPLY_CEILING


def _with_release_authors(monkeypatch: pytest.MonkeyPatch, authors) -> None:
    """Inject ``kanban.loop_breaker_release_authors`` into the config the kernel reads."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"loop_breaker_release_authors": authors}},
    )


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _reblock_after(conn, tid, comments=(), reason="go on the next plan revision"):
    """Unblock ``tid``, post ``comments`` ((author, body) pairs), run it again, re-block for a go.

    D9: a reply resets the count only when ``reason`` differs from the previous block's
    reason, so a caller that expects a reset passes a reason not used by the block before."""
    assert kb.unblock_task(conn, tid)
    for author, body in comments:
        kb.add_comment(conn, tid, author, body)
    _make_running_again(conn, tid)
    return kb.block_task(conn, tid, reason=reason, kind="needs_input")


def test_reblock_after_a_human_reply_starts_a_fresh_count(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (t_37039be4): run 1 asked for a go on a plan, the commissioner answered with
    amendments, the card was unblocked, run 2 asked for a go on the REVISED plan. That is a new
    question, not an unblock loop. The worker's own comment in between must not be the reason."""
    _with_release_authors(monkeypatch, "birk")
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        assert kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        kb.add_comment(conn, tid, "birk", "amend step 3")
        assert kb.unblock_task(conn, tid)
        kb.add_comment(conn, tid, "birk", "decision: variant B")
        _make_running_again(conn, tid)
        kb.add_comment(conn, tid, "architekt", "revising the plan")
        assert kb.block_task(conn, tid, reason="go on revised plan v2", kind="needs_input")

        task = kb.get_task(conn, tid)
        assert (task.status, task.block_kind, task.block_recurrences) == ("blocked", "needs_input", 1)
        last = kb.list_events(conn, tid)[-1]
        assert last.kind == "blocked"
        assert (last.payload["recurrences"], last.payload["reset_by_reply"]) == (1, "birk")
        assert not _events(conn, tid, "block_loop_detected")


def test_breaker_still_fires_after_a_reset_when_nobody_replies(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_release_authors(monkeypatch, "birk")
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert _reblock_after(conn, tid, [("birk", "amend step 3")])
        assert kb.get_task(conn, tid).status == "blocked"

        # Unblocked, nobody answered. The reason is new, so only the missing reply makes it a loop.
        assert _reblock_after(conn, tid, reason="go on plan v3")
        assert kb.get_task(conn, tid).status == "triage"
        loop = _events(conn, tid, "block_loop_detected")[-1].payload
        assert loop["recurrences"] == 2
        assert "reset_by_reply" not in loop


@pytest.mark.parametrize(("release_authors", "author", "body"), [
    (None, "architekt", "plan v2 is ready"),
    (None, AUTO_DECOMPOSER_AUTHOR, "decomposed into 6 children"),
    (None, "ops", "BLOCKED: waiting for the go"),
    (None, "ops", "UNBLOCK: go ahead"),
    (None, "ops", "SCHEDULED: retry tomorrow"),
    (None, "ops", "CHANGES REQUESTED: tighten step 2"),
    (("birk",), "ops", "go"),
], ids=["assignee", "auto-decomposer", "blocked-note", "unblock-note", "scheduled-note",
        "changes-requested-note", "not-a-release-author"])
def test_a_comment_that_is_not_a_reply_keeps_counting(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, release_authors, author, body,
) -> None:
    _with_release_authors(monkeypatch, release_authors)
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert _reblock_after(conn, tid, [(author, body)])
        assert kb.get_task(conn, tid).status == "triage"
        assert _events(conn, tid, "block_loop_detected")[-1].payload["recurrences"] == 2


def test_a_reply_followed_by_the_identical_question_is_still_a_loop(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D9: the commissioner answered, but the worker re-asks verbatim. That is an automated
    re-ask (real new questions differ in 436 of 437 measured loop events), so it still loops."""
    _with_release_authors(monkeypatch, "birk")
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert _reblock_after(conn, tid, [("birk", "amend step 3")], reason="  go on plan v1 ")
        assert kb.get_task(conn, tid).status == "triage"
        loop = _events(conn, tid, "block_loop_detected")[-1].payload
        assert loop["recurrences"] == 2
        assert "reset_by_reply" not in loop


def test_unreadable_config_fails_closed(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom():
        raise OSError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert _reblock_after(conn, tid, [("birk", "amend step 3")])
        assert kb.get_task(conn, tid).status == "triage"


def test_no_candidate_reply_reads_no_config(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: the config is read only once a comment passed the author and audit-note checks."""
    calls: list[int] = []
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: calls.append(1) or {})
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        kb.add_comment(conn, tid, "architekt", "still on it")
        kb.add_comment(conn, tid, "ops", "UNBLOCK: retry")
        _make_running_again(conn, tid)
        calls.clear()
        assert kb.block_task(conn, tid, reason="go on plan v1 again", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert calls == []


def test_a_comment_before_the_previous_block_is_not_a_reply(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order is the event id: rows sharing one ``created_at`` second must not leak across a block."""
    _with_release_authors(monkeypatch, None)
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.add_comment(conn, tid, "birk", "ask me before you start")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = 1000 WHERE task_id = ?", (tid,))
            conn.execute("UPDATE task_comments SET created_at = 1000 WHERE task_id = ?", (tid,))
        assert _reblock_after(conn, tid)
        assert kb.get_task(conn, tid).status == "triage"


def test_a_reply_in_the_same_second_as_the_block_still_counts(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_release_authors(monkeypatch, None)
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        kb.add_comment(conn, tid, "birk", "amend step 3")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = 1000 WHERE task_id = ?", (tid,))
            conn.execute("UPDATE task_comments SET created_at = 1000 WHERE task_id = ?", (tid,))
        assert _reblock_after(conn, tid)
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_recurrences) == ("blocked", 1)
