"""Review-handoff guard: a ``request_review`` whose review-lane worker cannot load ``sdlc-review``
must be refused at handoff time, not discovered as ``kanban_skills_unavailable`` at spawn
(cothinker/t_6c6b16fb run 3233: coder handed off without ``reviewer=``, the card stayed on
``coder``, the review lane force-loaded ``sdlc-review``, which coder disables)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_SKILL = "---\nname: sdlc-review\ndescription: Review stub.\n---\n\n# sdlc-review\n\nReview.\n"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name, cfg in (("coder", "skills:\n  disabled:\n    - sdlc-review\n"), ("reviewer", "{}\n")):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / "config.yaml").write_text(cfg, encoding="utf-8")
    skill_dir = home / "profiles" / "reviewer" / "skills" / "devops" / "sdlc-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _review_dispatch(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: enabled)


def _running_coder_card(conn) -> tuple[str, int]:
    tid = kb.create_task(conn, title="impl", assignee="coder")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return tid, claimed.current_run_id


def test_request_review_without_reviewer_refused_when_assignee_cannot_load_sdlc_review(kanban_home, monkeypatch):
    _review_dispatch(monkeypatch, True)
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
        ok, reason = kb.request_review(conn, tid, summary="done", expected_run_id=run_id, with_reason=True)
        assert ok is False
        assert "sdlc-review" in reason and "'coder'" in reason and "reviewer=" in reason
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee, task.current_run_id) == ("running", "coder", run_id)
        assert [e for e in kb.list_events(conn, tid) if e.kind == "review_requested"] == []


def test_request_review_with_capable_reviewer_is_accepted(kanban_home, monkeypatch):
    _review_dispatch(monkeypatch, True)
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer="reviewer", expected_run_id=run_id, with_reason=True)
        assert (ok, reason) == (True, None)
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("review", "reviewer")


def test_request_review_names_incapable_reviewer(kanban_home, monkeypatch):
    _review_dispatch(monkeypatch, True)
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
        ok, reason = kb.request_review(
            conn, tid, summary="done", reviewer="coder", expected_run_id=run_id, with_reason=True)
        assert ok is False and "'coder'" in reason
        assert kb.get_task(conn, tid).status == "running"


def test_request_review_unchanged_when_review_dispatch_is_off(kanban_home, monkeypatch):
    """Human-only review boards spawn nobody in the review lane: nothing to protect."""
    _review_dispatch(monkeypatch, False)
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
        assert kb.request_review(conn, tid, summary="done", expected_run_id=run_id) is True
        assert kb.get_task(conn, tid).status == "review"


def test_review_lane_never_spawns_a_worker_that_cannot_load_sdlc_review(kanban_home, monkeypatch):
    """Card acceptance: a reviewer-less handoff on a coder card never reaches a review-lane spawn
    whose forced skills fail for the spawned profile (= no ``kanban_skills_unavailable``)."""
    _review_dispatch(monkeypatch, True)
    spawned: list = []
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
        assert kb.request_review(conn, tid, summary="done", expected_run_id=run_id) is False
        assert kb.request_review(conn, tid, summary="done", reviewer="reviewer", expected_run_id=run_id) is True
        kbd.dispatch_once(conn, spawn_fn=lambda task, ws, *a, **k: spawned.append(task) or 4242)
    assert [t.id for t in spawned] == [tid]
    task = spawned[0]
    assert task.assignee == "reviewer" and "sdlc-review" in (task.skills or [])
    # Same loader + profile home the review worker uses (gateway/session_kanban.py _run_task_turns).
    from agent.skill_commands import build_preloaded_skills_prompt
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(get_profile_dir(task.assignee)))
    try:
        _, loaded, missing = build_preloaded_skills_prompt(list(task.skills), task_id=None)
    finally:
        reset_hermes_home_override(token)
    assert missing == [] and "sdlc-review" in loaded


def test_tool_handoff_without_reviewer_returns_actionable_error(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    _review_dispatch(monkeypatch, True)
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    with kbc.connect() as conn:
        tid, run_id = _running_coder_card(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = json.loads(kt._handle_request_review({"summary": "Ready for review."}))
    assert "sdlc-review" in out["error"] and "reviewer=" in out["error"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
