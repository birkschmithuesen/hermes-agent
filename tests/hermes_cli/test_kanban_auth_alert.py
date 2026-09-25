"""Exactly ONE operator alert per auth outage episode per profile (worker exit 77).

Night 24./25.09.2026 a logged-out ``anthropic_plan`` account produced 24 runs of one card in four
hours with no message to anyone. The opposite failure is just as bad: one Telegram message per
card per respawn would have been ~24 messages.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_auth_alert as alert
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB, and dead workers everywhere."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return home


@pytest.fixture
def sent(monkeypatch):
    """Capture alerts instead of sending them — tests never touch the network."""
    calls: list[tuple[str, str]] = []

    def _capture(target: str, message: str) -> bool:
        calls.append((target, message))
        return True

    monkeypatch.setattr(alert, "_sender", _capture)
    monkeypatch.setattr(alert, "configured_alert_target", lambda: "telegram:570261709:1743586")
    return calls


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _die(conn, task_id: str, pid: int, exit_code: int) -> None:
    """Claim the card, then let its worker die with ``exit_code`` and sweep it."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, task_id, claimer=f"{host}:w{pid}")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, task_id))
    conn.commit()
    kbd._record_worker_exit(pid, _exited_status(exit_code))
    kbd.detect_crashed_workers(conn)


def test_one_alert_per_episode_across_cards_and_respawns(kanban_home, sent):
    """Two cards of the same profile plus a respawn after the cooldown: ONE message. The third
    death runs on a fresh connection — the dedupe lives in the DB, so a dispatcher or gateway
    restart must not re-send it."""
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        b = kb.create_task(conn, title="card b", assignee="birk")
        _die(conn, a, 73001, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
        _die(conn, b, 73002, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
    with kbc.connect() as conn2:
        _die(conn2, a, 73003, kb.KANBAN_AUTH_FAILED_EXIT_CODE)

    assert len(sent) == 1
    target, message = sent[0]
    assert target == "telegram:570261709:1743586"
    assert "birk" in message
    assert "claude /login" in message
    # A snapshot count at alert time: card a had just failed, card b had not yet.
    assert "Wartende Karten: 1" in message
    # No secrets, no tokens, no file contents.
    assert ".credentials.json" not in message


def test_a_successful_run_ends_the_episode_and_rearms_the_alert(kanban_home, sent):
    """The episode ends when a run of that profile ends with any other outcome; the next outage
    alerts again."""
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        _die(conn, a, 73101, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
        assert len(sent) == 1

        # A later completed run of the same profile: the operator logged in, work resumed.
        later = int(time.time()) + 1
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'birk', 'done', 'completed', ?, ?)",
            (a, later, later),
        )
        conn.commit()

        _die(conn, a, 73102, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
        assert len(sent) == 2


def test_no_configured_target_sends_nothing(kanban_home, monkeypatch):
    """Default is opt-out: an install that never set ``kanban.auth_alert.target`` sends nothing."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alert, "_sender", lambda t, m: calls.append((t, m)) or True)
    monkeypatch.setattr(alert, "configured_alert_target", lambda: "")
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        _die(conn, a, 73301, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
    assert calls == []


def test_a_failing_send_neither_breaks_the_tick_nor_burns_the_episode(kanban_home, monkeypatch):
    """A broken transport must not break the dispatcher tick, and must leave the slot free so a
    later tick retries the alert."""
    attempts: list[str] = []

    def _boom(target: str, message: str) -> bool:
        attempts.append(target)
        raise RuntimeError("telegram down")

    monkeypatch.setattr(alert, "_sender", _boom)
    monkeypatch.setattr(alert, "configured_alert_target", lambda: "telegram:1:2")
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        _die(conn, a, 73401, kb.KANBAN_AUTH_FAILED_EXIT_CODE)  # must not raise
        assert kb.get_task(conn, a).status == "ready"
        _die(conn, a, 73402, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
    assert len(attempts) == 2


def test_configured_alert_target_reads_the_real_config_file(kanban_home, monkeypatch):
    """No monkeypatch of ``configured_alert_target`` here: the target must come from the real
    config loader reading the dispatcher's own ``HERMES_HOME`` (the fixture's temp home). Also
    covers the opt-out default: with no config file at all, nothing is sent."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alert, "_sender", lambda t, m: calls.append((t, m)) or True)

    with kbc.connect() as conn:
        no_target = kb.create_task(conn, title="card no-target", assignee="birk")
        _die(conn, no_target, 73601, kb.KANBAN_AUTH_FAILED_EXIT_CODE)
    assert calls == []  # no config.yaml at all -> default "" -> no send

    (kanban_home / "config.yaml").write_text(
        "kanban:\n  auth_alert:\n    target: telegram:1:2\n", encoding="utf-8",
    )
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        _die(conn, a, 73602, kb.KANBAN_AUTH_FAILED_EXIT_CODE)

    assert len(calls) == 1
    assert calls[0][0] == "telegram:1:2"


def test_a_rate_limited_death_sends_no_auth_alert(kanban_home, sent):
    """Acceptance (f): a real quota wall is unchanged — no alert, and no dedupe row."""
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="card a", assignee="birk")
        _die(conn, a, 73501, kb.KANBAN_RATE_LIMIT_EXIT_CODE)
        assert sent == []
        rows = conn.execute("SELECT COUNT(*) FROM kanban_auth_alerts").fetchone()[0]
        assert rows == 0
