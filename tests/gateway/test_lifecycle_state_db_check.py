"""An unclean gateway death must trigger a state.db integrity check.

Regression for the 2026-08-31 incident. ``state.db`` was corrupt from
2026-08-26 evening (a SIGKILL landed on a gateway mid-WAL-checkpoint during a
``--replace`` restart storm), but nothing checked the file. The damage sat in
old, rarely-read session rows for 3.5 days until a Desktop read tripped over
it on 2026-08-30 17:15 and surfaced as "Session not found".

``record_startup`` already detects the unclean exit and logs "SIGKILL / OOM /
VM death" — it just never looked at the database that death may have torn.
The check is gated on the unclean exit precisely because it costs ~2s on a
500MB store; a clean boot must not pay it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from gateway.lifecycle_ledger import (
    check_state_db_integrity,
    get_lifecycle_sentinel_path,
    record_startup,
)

_DEAD_PID = 2 ** 22 + 12345  # beyond default pid_max; never alive


def _write_sentinel(home: Path, phase: str = "running") -> None:
    path = get_lifecycle_sentinel_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "phase": phase,
            "pid": _DEAD_PID,
            "start_time": 1000.0,
            "started_at": "2026-08-26T23:56:45+00:00",
        }),
        encoding="utf-8",
    )


def _make_state_db(home: Path, *, corrupt: bool) -> Path:
    """Build a real SQLite file, optionally with a genuinely torn b-tree page."""
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(
        "INSERT INTO sessions (v) VALUES (?)", [(f"row-{i}" * 40,) for i in range(4000)]
    )
    conn.commit()
    conn.close()
    if corrupt:
        with open(path, "r+b") as handle:
            handle.seek(4096 * 6)
            handle.write(b"\xEF" * 4096)
    return path


def _exit_diag_records(home: Path) -> list:
    log = home / "logs" / "gateway-exit-diag.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ── the checker itself ──────────────────────────────────────────────────────


def test_checker_passes_a_healthy_store(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    assert check_state_db_integrity(home=tmp_path) == "ok"


def test_checker_reports_a_torn_btree_page(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    verdict = check_state_db_integrity(home=tmp_path)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_checker_tolerates_a_missing_store(tmp_path: Path) -> None:
    assert check_state_db_integrity(home=tmp_path) == "absent"


# ── wiring into the unclean-exit path ───────────────────────────────────────


def test_unclean_exit_records_the_corruption_verdict(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] != "ok"
    record = _exit_diag_records(tmp_path)[0]
    assert record["state_db_integrity"] != "ok"


def test_unclean_exit_on_a_healthy_store_records_ok(tmp_path: Path) -> None:
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    evidence = record_startup(home=tmp_path)

    assert evidence is not None
    assert evidence["state_db_integrity"] == "ok"


def test_clean_exit_does_not_pay_for_the_check(tmp_path: Path, monkeypatch) -> None:
    """A clean boot must not scan the store — that is the whole cost gate."""
    _make_state_db(tmp_path, corrupt=True)
    _write_sentinel(tmp_path, phase="exited")

    called = []
    import gateway.lifecycle_ledger as ledger

    monkeypatch.setattr(
        ledger, "check_state_db_integrity", lambda **kw: called.append(1) or "ok"
    )
    record_startup(home=tmp_path)

    assert not called, "integrity check ran on a clean boot"


# ── the startup budget ──────────────────────────────────────────────────────
#
# Regression for the 2026-09-15 incident, the mirror image of the one above.
# The check runs synchronously before any platform adapter connects, and
# ``quick_check`` is O(database) on a HEALTHY store: it reads every b-tree page
# before it can say "ok". On a 2.7 GB state.db on network-backed storage that was
# 15 minutes of cold reads (8.4 GB at ~19 MB/s) with Telegram dark throughout —
# the gateway looked hung, and the verdict was "ok". Unbounded forensics must not
# outweigh the outage they diagnose.


def test_check_finishes_within_its_budget_on_a_slow_store(tmp_path: Path) -> None:
    """A store too slow to walk must not hold the startup path open indefinitely."""
    _make_state_db(tmp_path, corrupt=False)

    started = time.monotonic()
    verdict = check_state_db_integrity(home=tmp_path, budget_seconds=0.001)
    elapsed = time.monotonic() - started

    # 0.001s is below any real walk, so the deadline fires on the first handler call.
    assert elapsed < 10, f"budget ignored: check ran {elapsed:.1f}s"
    assert verdict.startswith("check-inconclusive"), verdict


def test_budget_verdict_is_not_reported_as_healthy(tmp_path: Path) -> None:
    """An unfinished walk proves nothing — it must never read as a pass."""
    _make_state_db(tmp_path, corrupt=False)

    verdict = check_state_db_integrity(home=tmp_path, budget_seconds=0.001)

    assert verdict != "ok"
    assert verdict != "absent"
    assert "check-failed" not in verdict, "a budget stop is not a corruption finding"


def test_generous_budget_still_returns_the_real_verdict(tmp_path: Path) -> None:
    """The budget is a ceiling, not a shortcut: a store under it is fully checked."""
    healthy = tmp_path / "healthy"
    torn = tmp_path / "torn"
    healthy.mkdir()
    torn.mkdir()

    _make_state_db(healthy, corrupt=False)
    assert check_state_db_integrity(home=healthy, budget_seconds=60) == "ok"

    _make_state_db(torn, corrupt=True)
    verdict = check_state_db_integrity(home=torn, budget_seconds=60)
    assert verdict != "ok"
    assert "btreeInitPage" in verdict or "malformed" in verdict.lower()


def test_budget_is_on_by_default(tmp_path: Path) -> None:
    """The default call path — the one the gateway uses — carries a finite budget."""
    from gateway.lifecycle_ledger import DEFAULT_INTEGRITY_CHECK_BUDGET_SECONDS

    assert 0 < DEFAULT_INTEGRITY_CHECK_BUDGET_SECONDS <= 120

    calls = {}
    real = check_state_db_integrity

    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    import gateway.lifecycle_ledger as ledger

    def _spy(*args, **kwargs):
        calls.update(kwargs)
        return real(*args, **kwargs)

    original = ledger.check_state_db_integrity
    ledger.check_state_db_integrity = _spy
    try:
        record_startup(home=tmp_path)
    finally:
        ledger.check_state_db_integrity = original

    # The gateway never passes a budget itself; the default must supply the ceiling.
    assert "budget_seconds" not in calls or calls["budget_seconds"] > 0


def test_inconclusive_verdict_is_logged_as_unverified(tmp_path: Path, caplog) -> None:
    """The operator must be told the store is unproven, not left with silence."""
    _make_state_db(tmp_path, corrupt=False)
    _write_sentinel(tmp_path)

    import gateway.lifecycle_ledger as ledger

    original = ledger.check_state_db_integrity
    ledger.check_state_db_integrity = lambda **kw: "check-inconclusive: exceeded budget"
    try:
        with caplog.at_level(logging.WARNING, logger="gateway.lifecycle_ledger"):
            evidence = record_startup(home=tmp_path)
    finally:
        ledger.check_state_db_integrity = original

    assert evidence is not None
    assert evidence["state_db_integrity"].startswith("check-inconclusive")
    assert any("UNVERIFIED" in r.message or "UNVERIFIED" in r.getMessage()
               for r in caplog.records), caplog.text
