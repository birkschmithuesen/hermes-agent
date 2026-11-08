"""The managed-worker/owner path (``gateway.session_kanban._run_task_turns``) must not treat a
forced-but-unavailable skill as a retryable/unknown execution. Before t_199f842e this raised
``kanban_skills_unavailable`` straight into the generic managed-worker-lost handling, which only
knows the outcome is "unknown" and leaves the card ready to retry a guaranteed-identical failure;
the dispatcher's own crash text then read only "Kanban execution unknown; retained by owner",
naming neither the missing skill nor the profile."""
import json

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing


class _Agent:
    session_id = "s1"


def _claimed_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="needs a disabled skill", assignee="worker")
        task = kb.claim_task(conn, task_id, claimer="private-host:owner")
        assert task is not None
    context = {
        "db": str(path), "task_id": task_id, "run_id": task.current_run_id,
        "claim_lock": task.claim_lock, "skills": ["probe-skill"], "context": "", "profile": "worker",
        "goal_mode": False, "goal_text": "g", "goal_max_turns": 2,
    }
    return path, task_id, context


def test_missing_skill_blocks_card_with_diagnostic_and_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.skill_commands.build_preloaded_skills_prompt", lambda *a, **k: ("", [], ["probe-skill"]),
    )
    path, task_id, context = _claimed_context(tmp_path, monkeypatch)
    agent = _Agent()

    with pytest.raises(ValueError, match="kanban_skills_unavailable"):
        session_kanban._run_task_turns(agent, {"text": "work"}, [], context)

    with connect_closing(path) as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT error, summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    # Criterion 3: blocked on the first occurrence, never left ready to retry the same failure.
    assert task.status == "blocked"
    # Criterion 2: the crash record names the cause and the missing skill, not a generic label.
    assert "kanban_skills_unavailable" in (run["error"] or "")
    assert "probe-skill" in (run["error"] or "")
    assert "worker" in (run["error"] or "")
    assert "kanban_skills_unavailable" in (run["summary"] or "")
