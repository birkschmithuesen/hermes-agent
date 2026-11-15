"""An update drain is a run-state event, not a card failure (card t_bb79440f).

On 2026-11-15 two core updates killed 40 workers with their own final text
``Kanban owner refused: ... Gateway draining: update_paused`` (or an ImportError from a
half-updated install); 31 cards were given up after ONE failure because identical fingerprints
tripped the systemic same-error rule. Spec: docs/superpowers/specs/2026-11-15-update-drain-requeue-design.md
(each test names its D-number).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_update_drain as kud

DRAIN_OUTPUT = (
    "Query: work kanban task {tid}\n"
    "Initializing agent...\n"
    "hermes: a source update is unfinished; run `hermes update` from a shell to finish it\n"
    "Kanban owner refused: Kanban owner silent for 181s: Gateway draining: update_paused\n"
)
PLAIN_CRASH = "Query: work kanban task {tid}\nTraceback (most recent call last):\nValueError: boom\n"


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


@pytest.fixture
def install_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake live install; a card's worktree lives elsewhere under tmp_path."""
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(kud, "_install_root", lambda: root)
    return root


def _module_file(base: Path, mtime: float) -> Path:
    mod = base / "hermes_cli" / "_subprocess_compat.py"
    mod.parent.mkdir(parents=True, exist_ok=True)
    mod.write_text("")
    os.utime(mod, (mtime, mtime))
    return mod


def _import_error(tid: str, path: Path) -> str:
    return (
        f"Query: work kanban task {tid}\nTraceback (most recent call last):\n"
        '  File "worker.py", line 1, in <module>\n'
        f"ImportError: cannot import name 'run_quiet' from 'hermes_cli._subprocess_compat' ({path})\n"
    )


# --- D2: the drain text, anchored at the end of the worker's final output ---------------------


@pytest.mark.parametrize("output, kind", [
    ("x Kanban owner refused: Kanban owner silent for 181s: Gateway draining: update_paused", "drain"),
    ("x Kanban owner refused: Gateway draining: update_paused  ", "drain"),
    ("Kanban owner refused: Gateway draining: update_paused Traceback (most recent call last): "
     "ValueError: boom", None),
    ("connection: Gateway draining: update_paused. Your message was not sent. Run `hermes doctor` "
     "to check the setup, or /model to pick a different provider. Goodbye! ☤", None),
    ("Kanban owner refused: Gateway inaccessible: service_identity_unverified", None),
    ("", None),
], ids=["owner-silent", "owner-refused", "drain-not-last", "one-shot-worker-text",
        "service-identity", "no-output"])
def test_the_drain_text_counts_only_as_the_final_output(output, kind):
    """D2 (+ D7 out-of-scope shapes): only our own owner-refused drain text, at the very end."""
    assert kud.update_drain_kind(output, None) == kind


# --- D3: a mixed-version ImportError ----------------------------------------------------------


@pytest.mark.parametrize("where, offset, kind", [
    ("install", +300, "mixed_import"),
    ("install", -300, None),
    ("worktree", +300, None),
    ("missing", +300, None),
], ids=["new-file-in-install", "file-older-than-run", "card-worktree-file", "file-gone"])
def test_a_mixed_import_needs_a_new_file_inside_the_install(install_root, tmp_path, where, offset, kind):
    """D3: path inside the install root AND mtime after the run started; anything else counts."""
    run_start = time.time() - 600
    if where == "missing":
        path = install_root / "hermes_cli" / "gone.py"
    else:
        base = install_root if where == "install" else tmp_path / "worktree"
        path = _module_file(base, run_start + offset)
    output = (
        "Traceback (most recent call last): ImportError: cannot import name 'run_quiet' "
        f"from 'hermes_cli._subprocess_compat' ({path})"
    )
    assert kud.update_drain_kind(output, run_start) == kind


# --- D1: the fence probe ----------------------------------------------------------------------


def test_install_update_fenced_reads_the_real_update_marker(kanban_home):
    """D1: the real gateway_runtime judge over this home. An empty marker is an update's
    exclusive publish in flight and fences for EMPTY_MARKER_GRACE_SECONDS (5 s)."""
    from hermes_cli import update_lock

    assert kud.install_update_fenced() is False
    (kanban_home / update_lock.MARKER_NAME).write_bytes(b"")
    assert kud.install_update_fenced() is True


def test_a_broken_fence_probe_fails_open(kanban_home, monkeypatch):
    """D1: a probe that raises must not stop the factory (failure mode = today's behaviour)."""
    from hermes_cli import gateway_runtime

    def broken(_homes):
        raise OSError("probe broke")

    monkeypatch.setattr(gateway_runtime, "_update_fenced", broken)
    assert kud.install_update_fenced() is False


# --- D4 / D5 / D6: booking a dead worker --------------------------------------------------------


def _host() -> str:
    return kb._claimer_id().split(":", 1)[0]


def _claim_dead(conn, tid: str, pid: int, *, review: bool = False, raw_status: int = 1 << 8):
    """Claim ``tid`` on this host as a spawn whose worker ``pid`` has already exited."""
    claim = kb.claim_review_task if review else kb.claim_task
    claimed = claim(conn, tid, claimer=f"{_host()}:w{pid}")
    assert claimed is not None
    # This spawn's log segment starts here (_current_spawn_segment reads only this spawn).
    _append_log(tid, f"{kbd.KANBAN_WORKER_SPAWN_MARKER}{claimed.current_run_id}\n")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    kbd._record_worker_exit(pid, raw_status)
    return claimed


def _running(conn, title: str = "impl", *, review: bool = False, pid: int = 72000) -> str:
    tid = kb.create_task(conn, title=title, assignee="coder")
    if review:
        first = kb.claim_task(conn, tid, claimer=f"{_host()}:w0")
        assert kb.request_review(conn, tid, summary="done", expected_run_id=first.current_run_id)
    _claim_dead(conn, tid, pid, review=review)
    return tid


def _append_log(tid: str, text: str) -> None:
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


def _outcomes(conn, tid: str) -> list[str]:
    return [r["outcome"] for r in conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id", (tid,))]


def _last_crashed_payload(conn, tid: str) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='crashed' ORDER BY id DESC", (tid,),
    ).fetchone()
    return json.loads(row["payload"])


@pytest.mark.parametrize("review", [False, True], ids=["ready-lane", "review-lane"])
def test_drain_death_requeues_without_counting(kanban_home, review):
    """D2/D4 (§3.1): back to the source phase, failures unchanged, outcome update_drain, no gave_up."""
    with kbc.connect() as conn:
        tid = _running(conn, review=review)
        before = kb.get_task(conn, tid).consecutive_failures
        _append_log(tid, DRAIN_OUTPUT.format(tid=tid))

        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == ("review" if review else "ready")
        assert task.claim_lock is None
        assert task.consecutive_failures == before
        assert "Gateway draining: update_paused" in (task.last_failure_error or "")
        assert _outcomes(conn, tid)[-1] == "update_drain"
        assert _last_crashed_payload(conn, tid)["update_drain"] == "drain"
        gave_up = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,),
        ).fetchone()[0]
        assert gave_up == 0


def test_three_drain_deaths_in_one_sweep_block_none(kanban_home):
    """D4 (§3.2): drained deaths never reach _account_crashes, so the systemic rule (3 identical
    fingerprints -> sticky gave_up at 1, the 15.11. incident) cannot see them."""
    with kbc.connect() as conn:
        tids = [_running(conn, title=f"c{i}", pid=72000 + i) for i in range(3)]
        for tid in tids:
            _append_log(tid, DRAIN_OUTPUT.format(tid=tid))

        res = kbd.dispatch_once(conn, dry_run=True)

        assert sorted(res.update_drained) == sorted(tids)
        assert res.crashed == [] and res.auto_blocked == []
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert (task.status, task.consecutive_failures) == ("ready", 0)


@pytest.mark.parametrize("output", [
    PLAIN_CRASH,
    "Query: work kanban task {tid}\nKanban owner refused: Gateway draining: update_paused\n"
    "Traceback (most recent call last):\nValueError: boom\n",
    "Query: work kanban task {tid}\nconnection: Gateway draining: update_paused. Your message was "
    "not sent. Run `hermes doctor` to check the setup, or /model to pick a different provider. "
    "Goodbye! ☤\n",
    "Query: work kanban task {tid}\nKanban owner refused: Gateway inaccessible: "
    "service_identity_unverified\n",
], ids=["plain-crash", "drain-not-last", "one-shot-worker-text", "service-identity"])
def test_other_deaths_still_count(kanban_home, output):
    """D6 (§3.3) regression, D7 shapes: any other final output books exactly as before."""
    with kbc.connect() as conn:
        tid = _running(conn)
        _append_log(tid, output.format(tid=tid))

        kbd.detect_crashed_workers(conn)

        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert _outcomes(conn, tid) == ["crashed"]
        assert "update_drain" not in _last_crashed_payload(conn, tid)


def test_the_systemic_rule_still_trips_on_identical_plain_crashes(kanban_home):
    """D6: the systemic rule itself stays; only drained deaths stopped feeding it."""
    with kbc.connect() as conn:
        tids = [_running(conn, title=f"c{i}", pid=72000 + i) for i in range(3)]
        for tid in tids:
            _append_log(tid, PLAIN_CRASH.format(tid=tid))

        kbd.detect_crashed_workers(conn)

        assert sorted(kbd.detect_crashed_workers._last_auto_blocked) == sorted(tids)
        assert {kb.get_task(conn, t).status for t in tids} == {"blocked"}


@pytest.mark.parametrize("where, offset, outcome, failures", [
    ("install", +300, "update_drain", 0),
    ("install", -300, "crashed", 1),
    ("worktree", +300, "crashed", 1),
], ids=["new-file-in-install", "file-older-than-run", "card-worktree-file"])
def test_mixed_version_import_error_books_by_root_and_run_start(
    kanban_home, install_root, tmp_path, where, offset, outcome, failures,
):
    """D3/D4a (§3.4). The run started 600 s ago, ``tasks.started_at`` says 120 s ago and the
    install file was written 300 s ago: only the RUN's start makes it a mixed import."""
    with kbc.connect() as conn:
        tid = _running(conn)
        run_start = int(time.time()) - 600
        conn.execute(
            "UPDATE task_runs SET started_at=? WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
            (run_start, tid),
        )
        conn.commit()
        base = install_root if where == "install" else tmp_path / "worktree"
        _append_log(tid, _import_error(tid, _module_file(base, run_start + offset)))

        kbd.detect_crashed_workers(conn)

        assert _outcomes(conn, tid) == [outcome]
        assert kb.get_task(conn, tid).consecutive_failures == failures


_DEATHS = {
    "d": (1 << 8, DRAIN_OUTPUT),
    "c": (1 << 8, PLAIN_CRASH),
    "rl": (kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8, ""),
}


@pytest.mark.parametrize("deaths, outcomes", [
    (["d", "d", "d", "d"], ["update_drain"] * 3 + ["crashed"]),
    (["d", "d", "c", "d", "d", "d"], ["update_drain", "update_drain", "crashed"] + ["update_drain"] * 3),
    (["d", "rl", "d", "d", "d"],
     ["update_drain", "rate_limited", "update_drain", "update_drain", "crashed"]),
], ids=["fourth-drain-counts", "a-crash-breaks-the-streak", "rate-limit-is-neutral"])
def test_drain_streak_is_capped(kanban_home, deaths, outcomes):
    """D5 (§3.5): three update deaths in a row are free, the 4th counts; rate_limited is neutral,
    any other outcome restarts the streak."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="coder")
        for n, death in enumerate(deaths):
            raw_status, output = _DEATHS[death]
            _claim_dead(conn, tid, 72000 + n, raw_status=raw_status)
            _append_log(tid, output.format(tid=tid))
            kbd.detect_crashed_workers(conn)

        assert _outcomes(conn, tid) == outcomes
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 1)


def test_a_drained_card_whose_output_mentions_auth_is_not_parked(kanban_home):
    """D4a: an update_drain run's error carries the worker's own output (like a crash, #117097),
    so the blocker_auth regex must not park the card on a benign ``claude auth status``."""
    with kbc.connect() as conn:
        tid = _running(conn)
        _append_log(tid, f"Query: work kanban task {tid}\n$ claude auth status\nlogged in\n"
                         "Kanban owner refused: Gateway draining: update_paused\n")

        kbd.detect_crashed_workers(conn)

        assert _outcomes(conn, tid) == ["update_drain"]
        assert "claude auth status" in (kb.get_task(conn, tid).last_failure_error or "")
        assert kbd.check_respawn_guard(conn, tid) is None
