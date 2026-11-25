"""A worker that dies on a skill-preload error is blocked after ONE spawn.

The error is deterministic (skill missing or disabled in the assignee profile; the review lane
force-adds ``sdlc-review``), so the crash budget only multiplied identical deaths: t_333c5559
was claimed 10 times. The stale ``rc=0`` trailer of an older run must not turn such a death
into a protocol violation either.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import main as hmain
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

OLD_RUN = (
    "Task handed off for review with full test evidence cited.\n\n"
    "Resume this session with:\n  hermes --resume 20261020_052503_93f2f4 -p coder\n\n"
    f"{KANBAN_WORKER_EXIT_TRAILER}0\n"
)
PRELOAD_CRASH = "Query: work kanban task {tid}\nInitializing agent...\nError: Unknown skill(s): sdlc-review\n"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _append_log(tid: str, text: str) -> None:
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


@pytest.mark.parametrize("marked", [True, False], ids=["marked-log", "legacy-log"])
@pytest.mark.parametrize("reaped_rc", [1, None], ids=["reaped-rc1", "per-tick-sweep"])
def test_preload_failure_blocks_review_card_after_one_spawn(kanban_home, reaped_rc, marked):
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="impl", assignee="coder", max_retries=5)
        claimed = kb.claim_task(conn, tid, claimer=f"{host}:w0")
        # No reviewer: the card stays assigned to the implementer (the t_333c5559 shape).
        assert kb.request_review(conn, tid, summary="done", expected_run_id=claimed.current_run_id)
        _append_log(tid, OLD_RUN)
        review = kb.claim_review_task(conn, tid, claimer=f"{host}:r0")
        assert review is not None and review.assignee == "coder"
        marker = f"{kbd.KANBAN_WORKER_SPAWN_MARKER}{review.current_run_id}\n" if marked else ""
        _append_log(tid, marker + PRELOAD_CRASH.format(tid=tid))
        pid = 72000
        conn.execute(
            "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
            (pid, int(time.time()) - 120, tid),
        )
        conn.commit()
        if reaped_rc is not None:
            kbd._record_worker_exit(pid, reaped_rc << 8)

        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.consecutive_failures == 1  # one spawn, not max_retries of them
        reason = task.last_failure_error or ""
        assert "Unknown skill(s): sdlc-review" in reason
        assert "'coder'" in reason
        assert ("reassign to a profile that has the skill (review lane: reviewer), "
                "or enable the skill in that profile") in reason
        assert "protocol violation" not in reason
        assert "handed off for review" not in reason
        crashed_runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND outcome='crashed'", (tid,),
        ).fetchone()[0]
        assert crashed_runs == 1
        gave_up = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,),
        ).fetchone()
        assert json.loads(gave_up["payload"])["preload_failed"] is True

        # Sticky: stays parked until an operator reassigns or enables the skill.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_stale_trailer_of_an_older_run_is_not_this_spawns_exit_code(kanban_home):
    _append_log("t_1", OLD_RUN + "Query: work kanban task t_1\nsomething else died\n")
    assert kbd._worker_log_exit_code("t_1") is None
    _append_log("t_2", OLD_RUN)
    assert kbd._worker_log_exit_code("t_2") == 0


def test_ambiguous_skill_is_a_preload_failure_too(kanban_home):
    from agent.skill_utils import AMBIGUOUS_SKILL_PREFIX

    _append_log("t_1", f"Error: {AMBIGUOUS_SKILL_PREFIX}'review' matches 2 skills\n")
    assert kbd._worker_preload_failure("t_1").startswith(AMBIGUOUS_SKILL_PREFIX)
    _append_log("t_2", "Error: something unrelated\n")
    assert kbd._worker_preload_failure("t_2") == ""


def test_chat_value_error_exits_1_with_trailer_for_kanban_worker(monkeypatch, capsys):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    with pytest.raises(SystemExit) as exc:
        hmain._exit_on_chat_value_error(ValueError("Unknown skill(s): sdlc-review"))
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Error: Unknown skill(s): sdlc-review" in captured.out
    assert f"{KANBAN_WORKER_EXIT_TRAILER}1" in captured.err
