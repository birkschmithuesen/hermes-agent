"""The dead-worker sweep reads only what the CURRENT spawn wrote to its log.

The per-task worker log is append-mode across spawns. Cutting at the LAST exit summary
returned an older run's text whenever the current spawn died before printing one (t_333c5559:
ten skill-preload crashes, each booked with run 475's last words).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

OLD_RUN = (
    "Task handed off for review with full test evidence cited.\n\n"
    "Resume this session with:\n  hermes --resume 20261020_052503_93f2f4 -p coder\n\n"
    "Session:        20261020_052503_93f2f4\n\n"
    f"{KANBAN_WORKER_EXIT_TRAILER}0\n"
)
PRELOAD_CRASH = "Query: work kanban task t_1\nInitializing agent...\nError: Unknown skill(s): sdlc-review\n"


def _marker(run_id) -> str:
    return f"{kbd.KANBAN_WORKER_SPAWN_MARKER}{run_id}\n"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_log(tid: str, text: str) -> None:
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")


def test_legacy_log_reports_the_new_spawns_preload_error_not_the_old_run(kanban_home):
    """No spawn marker (logs written before this fix): the old run's trailer closes its segment."""
    _write_log("t_1", OLD_RUN + PRELOAD_CRASH)
    out = kbd._worker_final_output("t_1")
    assert "Unknown skill(s): sdlc-review" in out
    assert "handed off for review" not in out


def test_marked_log_reports_only_the_last_spawn(kanban_home):
    _write_log("t_1", _marker(475) + OLD_RUN + _marker(489) + PRELOAD_CRASH)
    out = kbd._worker_final_output("t_1")
    assert out == "Error: Unknown skill(s): sdlc-review"


def test_current_spawn_that_wrote_nothing_reports_empty(kanban_home):
    _write_log("t_1", _marker(475) + OLD_RUN + _marker(489))
    assert kbd._worker_final_output("t_1") == ""


@pytest.mark.parametrize("prefix", ["", _marker(1)], ids=["legacy", "marked"])
def test_single_run_log_reads_as_before(kanban_home, prefix):
    _write_log("t_1", prefix + "the model said something\n\nResume this session with:\n"
                      f"  hermes --resume x\n\n{KANBAN_WORKER_EXIT_TRAILER}0\n")
    assert kbd._worker_final_output("t_1") == "the model said something"


def test_open_worker_log_writes_the_spawn_marker(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        task = kb.claim_task(conn, tid)
    log_f = kbd._open_worker_log(task, None)
    log_f.close()
    assert kb.worker_log_path(tid).read_text(encoding="utf-8") == _marker(task.current_run_id)
