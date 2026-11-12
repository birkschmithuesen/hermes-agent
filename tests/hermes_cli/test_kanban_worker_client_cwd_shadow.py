"""A dispatched worker must not import Hermes from its cwd (the task workspace).

The worker is spawned with ``cwd=<task workspace>``. For a ``.worktrees/<id>`` of hermes-agent that
workspace is itself a Hermes checkout; without ``-P`` its ``hermes_bootstrap`` shadowed the pinned
tree and every spawn died with "no dependency environment is committed for this install
(<worktree>)" (t_79f0f309, t_02c6d7b6).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db import Task

_REPO_ROOT = Path(kbd.__file__).resolve().parents[1]


def _task() -> Task:
    return Task(
        id="t_shadow", title="t", body="", assignee="worker", status="running", priority=0,
        created_by=None, created_at=1, started_at=None, completed_at=None,
        workspace_kind="worktree", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None,
    )


def test_worker_argv_runs_the_client_in_safe_path_mode():
    argv = kbd._worker_argv(_task(), "worker", None)
    assert argv == [sys.executable, "-P", "-m", "hermes_cli.kanban_worker_client"]


def test_worker_interpreter_ignores_a_hermes_checkout_in_its_cwd(tmp_path):
    # A worktree-shaped cwd whose bootstrap dies the way an unbuilt checkout's does.
    (tmp_path / "hermes_bootstrap.py").write_text(
        "raise SystemExit('hermes: no dependency environment is committed for this install')\n",
        encoding="utf-8",
    )
    argv = kbd._worker_argv(_task(), "worker", None)
    interpreter_and_flags = argv[: argv.index("-m")]
    probe = "import hermes_bootstrap; print(hermes_bootstrap.__file__)"
    r = subprocess.run(
        [*interpreter_and_flags, "-c", probe],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert r.returncode == 0, r.stderr[-300:]
    assert Path(r.stdout.strip()).resolve().parent == _REPO_ROOT
