"""Create-time guard: ``kanban_create`` with a forced ``skills=[X]`` that cannot load under the
assignee profile's own config must be rejected, not discovered after a doomed run
(t_199f842e: a forced-but-disabled skill died in ``_run_task_turns`` on every spawn and the
dispatcher reported only "Kanban execution unknown; retained by owner")."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_profile_with_disabled_skill(home: Path, profile: str, disabled_skill: str) -> None:
    profile_dir = home / "profiles" / profile
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        f"skills:\n  disabled:\n    - {disabled_skill}\n", encoding="utf-8",
    )


def test_create_task_rejects_skill_disabled_for_assignee_profile(kanban_home):
    _make_profile_with_disabled_skill(kanban_home, "worker", "delegated-research-and-verification")
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(kanban_home / "kanban.db") as conn:
        with pytest.raises(ValueError) as exc_info:
            kb.create_task(
                conn, title="t", assignee="worker",
                skills=["delegated-research-and-verification"],
            )
    message = str(exc_info.value)
    assert "delegated-research-and-verification" in message
    assert "worker" in message


def test_create_task_allows_skill_enabled_for_assignee_profile(kanban_home):
    profile_dir = kanban_home / "profiles" / "worker"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    skill_dir = profile_dir / "skills" / "probe-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: A probe skill.\n---\n\n# probe-skill\n\nDo the thing.\n",
        encoding="utf-8",
    )
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(kanban_home / "kanban.db") as conn:
        task_id = kb.create_task(conn, title="t", assignee="worker", skills=["probe-skill"])
    assert task_id
