"""Memory-pressure warning is throttled, the guard's behaviour is not.

The memory-pressure guard in ``_tick_spawn_budget`` runs on every dispatch
tick, and the dispatcher ticks several times a minute. On a host under
sustained pressure this reprinted the identical warning on every tick —
dozens of duplicate journal lines per hour carrying no new information.

These tests pin the throttle:

* a persisting state warns once, not once per tick,
* every state transition (ok -> elevated -> critical -> elevated -> ok)
  warns immediately,
* after the cooldown a persisting state warns again,

and — the part that must NOT change — that the spawn budget the guard
computes is byte-for-byte what it was before the throttle existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


GIB = 1024 * 1024  # KiB per GiB


def _pressure_sample(level: str) -> dict:
    """Memory sample that ``classify_pressure`` maps to ``level``."""
    total = 1 * GIB
    if level == "critical":
        return {"mem_available_kib": 32 * 1024, "mem_total_kib": total}
    if level == "elevated":
        return {"mem_available_kib": 100 * 1024, "mem_total_kib": total}
    return {"mem_available_kib": total // 2, "mem_total_kib": total}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _reset_throttle_state(monkeypatch):
    """Module-level throttle state must not leak between tests."""
    monkeypatch.setattr(kbd, "_last_pressure_log", None, raising=False)
    yield
    kbd._last_pressure_log = None


class _WarningRecorder:
    """Stand-in for ``kanban_db._log`` that records warning call sites."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(str(msg))

    def __getattr__(self, name):  # info/debug/error are no-ops here
        return lambda *a, **k: None


@pytest.fixture
def warnings_log(monkeypatch):
    rec = _WarningRecorder()
    monkeypatch.setattr(kb, "_log", rec)
    return rec


@pytest.fixture
def fake_clock(monkeypatch):
    """Controllable ``time.monotonic`` for the cooldown window."""

    class _Clock:
        def __init__(self):
            self.now = 1000.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    clock = _Clock()
    monkeypatch.setattr(kbd.time, "monotonic", lambda: clock.now)
    return clock


def _pressure_warnings(rec: _WarningRecorder) -> list[str]:
    return [m for m in rec.warnings if "system memory pressure" in m]


def _tick(conn, level: str, monkeypatch, **kwargs):
    """One ``_tick_spawn_budget`` call at the given pressure level."""
    monkeypatch.setattr(
        kbd, "_memory_pressure_level", lambda sample=None: level
    )
    result = kbd.DispatchResult()
    may_spawn, budget = kbd._tick_spawn_budget(
        conn,
        result,
        max_spawn=kwargs.get("max_spawn"),
        max_in_progress=kwargs.get("max_in_progress"),
        board=None,
    )
    return may_spawn, budget, result


# ---------------------------------------------------------------------------
# (a) a persisting state warns ONCE, not once per tick
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["elevated", "critical"])
def test_sustained_pressure_warns_once_across_many_ticks(
    kanban_home, warnings_log, fake_clock, monkeypatch, level,
):
    with kb.connect() as conn:
        for _ in range(10):
            fake_clock.advance(15.0)  # ticks well inside the cooldown
            _tick(conn, level, monkeypatch)

    assert len(_pressure_warnings(warnings_log)) == 1


def test_sustained_pressure_still_reports_via_structured_channel(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    """Suppressing the log must not make the state invisible."""
    levels = []
    with kb.connect() as conn:
        for _ in range(5):
            fake_clock.advance(15.0)
            _, _, result = _tick(conn, "elevated", monkeypatch)
            levels.append(result.memory_pressure)

    assert levels == ["elevated"] * 5
    assert len(_pressure_warnings(warnings_log)) == 1


# ---------------------------------------------------------------------------
# (b) state transitions warn immediately
# ---------------------------------------------------------------------------


def test_state_change_logs_immediately(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    with kb.connect() as conn:
        for level in ("elevated", "critical", "elevated"):
            fake_clock.advance(1.0)  # far inside the cooldown
            _tick(conn, level, monkeypatch)

    msgs = _pressure_warnings(warnings_log)
    assert len(msgs) == 3
    assert "elevated" in msgs[0]
    assert "critical" in msgs[1]
    assert "elevated" in msgs[2]


def test_recovery_resets_cooldown_so_reentry_warns_at_once(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    """ok -> elevated -> ok -> elevated warns twice, inside one cooldown."""
    with kb.connect() as conn:
        _tick(conn, "elevated", monkeypatch)
        fake_clock.advance(1.0)
        _tick(conn, "ok", monkeypatch)
        fake_clock.advance(1.0)
        _tick(conn, "elevated", monkeypatch)

    assert len(_pressure_warnings(warnings_log)) == 2


def test_ok_and_unknown_never_warn(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    with kb.connect() as conn:
        for level in ("ok", "unknown", "ok"):
            fake_clock.advance(600.0)
            _tick(conn, level, monkeypatch)

    assert _pressure_warnings(warnings_log) == []


# ---------------------------------------------------------------------------
# (c) after the cooldown, a persisting state warns again
# ---------------------------------------------------------------------------


def test_warns_again_after_cooldown_elapses(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    with kb.connect() as conn:
        _tick(conn, "elevated", monkeypatch)
        # One second short of the cooldown: still suppressed.
        fake_clock.advance(kbd._PRESSURE_LOG_COOLDOWN_SECONDS - 1.0)
        _tick(conn, "elevated", monkeypatch)
        assert len(_pressure_warnings(warnings_log)) == 1
        # Crossing the boundary re-arms the warning.
        fake_clock.advance(2.0)
        _tick(conn, "elevated", monkeypatch)

    assert len(_pressure_warnings(warnings_log)) == 2


def test_cooldown_is_a_module_constant_not_an_env_var():
    """Upstream rubric: no new HERMES_* env vars for non-secrets."""
    assert isinstance(kbd._PRESSURE_LOG_COOLDOWN_SECONDS, (int, float))
    assert kbd._PRESSURE_LOG_COOLDOWN_SECONDS > 0


# ---------------------------------------------------------------------------
# (d) DISPATCH BEHAVIOUR is unchanged by the throttle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tick_index", [0, 1, 2, 5])
def test_budget_identical_on_suppressed_ticks_elevated(
    kanban_home, warnings_log, fake_clock, monkeypatch, tick_index,
):
    """The tick whose warning is suppressed still caps the budget at 1."""
    with kb.connect() as conn:
        for _ in range(tick_index):
            fake_clock.advance(5.0)
            _tick(conn, "elevated", monkeypatch)
        fake_clock.advance(5.0)
        may_spawn, budget, result = _tick(conn, "elevated", monkeypatch)

    assert may_spawn is True
    assert budget == 1
    assert result.memory_pressure == "elevated"


@pytest.mark.parametrize("tick_index", [0, 1, 2, 5])
def test_budget_identical_on_suppressed_ticks_critical(
    kanban_home, warnings_log, fake_clock, monkeypatch, tick_index,
):
    """Critical still refuses to spawn, logged or not."""
    with kb.connect() as conn:
        for _ in range(tick_index):
            fake_clock.advance(5.0)
            _tick(conn, "critical", monkeypatch)
        fake_clock.advance(5.0)
        may_spawn, budget, result = _tick(conn, "critical", monkeypatch)

    assert may_spawn is False
    assert budget is None
    assert result.memory_pressure == "critical"


def test_elevated_does_not_widen_a_tighter_caller_budget(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    """A caller cap of 0 remaining must not be widened to 1 by the guard."""
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        may_spawn, budget, _ = _tick(
            conn, "elevated", monkeypatch, max_in_progress=1
        )

    assert may_spawn is False
    assert budget is None


def test_unknown_pressure_leaves_budget_uncapped(
    kanban_home, warnings_log, fake_clock, monkeypatch,
):
    with kb.connect() as conn:
        may_spawn, budget, result = _tick(conn, "unknown", monkeypatch)

    assert may_spawn is True
    assert budget is None
    assert result.memory_pressure is None
