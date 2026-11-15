# Update drain is a run-state event, not a card failure — Implementation Plan (card t_bb79440f)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A worker that dies because an update drained/fenced the gateway (or imported a
half-updated tree) goes back to `ready`/`review` WITHOUT consuming the retry budget, and the
dispatcher claims no new work while an update holds the install. A normal crash still counts.

**Architecture:** A new module `hermes_cli/kanban_update_drain.py` owns all behaviour: the fence
probe (D1), the two output matchers (D2/D3), the free-requeue streak (D5) and one classifier hook
that marks a `_DeadWorker`. `hermes_cli/kanban_db_dispatch.py` gets only thin wiring: one flag on
`_DeadWorker`, one hook call in `_reclaim_dead_workers`, a new sweep/result bucket that bypasses
`_account_crashes`, the `check_respawn_guard` exemption (D4a) and the fence gate in
`_dispatch_once_locked`.

**Tech Stack:** Python 3.11, sqlite3, pytest via `scripts/run_tests.sh`; existing kanban fixtures
(style of `tests/hermes_cli/test_kanban_preload_failure_block.py`).

**Spec (binding, final):** `docs/superpowers/specs/2026-11-15-update-drain-requeue-design.md`;
D0..D9 are decided and not reopened here. Probes: `docs/superpowers/specs/2026-11-15-update-drain-probes/`.

## Global Constraints

- Base (D0): `feat/kanban-review-crash-loop` @ c350c529a491. Never rebase onto origin/main; no
  push, no merge from the coder card.
- Behaviour lives in `hermes_cli/kanban_update_drain.py`; `kanban_db_dispatch.py` gets wiring only
  (that file is far over the 2,000-line ratchet, see "Measured baseline").
- Do NOT touch `describe_suppression`, the memory-pressure lines in `_tick_spawn_budget`, the
  per-profile pool lines in `_dispatch_once_locked`, or any `DispatchResult` field after
  `memory_pressure` (sibling feats `feat/kanban-gateway-live-caps` & co. edit them).
- `DispatchResult.update_drained: list[str]` goes directly after `rate_limited`, before
  `skipped_locked` (D4).
- Drain regex, verbatim (D2):
  `r"Kanban owner refused: (?:Kanban owner silent for \d+s: )?Gateway draining: update_paused\s*$"`
- Mixed-import regex, verbatim (D3):
  `r"ImportError: cannot import name '\w+' from '[\w.]+' \(([^()]+\.py)\)\s*$"`
- Fence warning text, verbatim (D1):
  `kanban dispatch: an update holds the install; spawning no new workers this tick (deferred, not dropped)`
- Fence probe = `gateway_runtime._update_fenced({get_default_hermes_root(), get_hermes_home()})`;
  any exception → `False` + debug log with `exc_info=True` (fail-open, D1).
- Run outcome `update_drain`; event kind stays `crashed` with payload `update_drain: "drain" | "mixed_import"` (D4).
- `UPDATE_DRAIN_FREE_LIMIT = 3` consecutive closed `update_drain` runs; `rate_limited` neutral;
  anything else breaks the streak (D5).
- D3 run start = `task_runs.started_at` of the run being closed, read before `_end_run` — never
  `tasks.started_at` (D4a).
- Tests: `scripts/run_tests.sh` only, never bare pytest; behaviour contracts, no source-reading.
- English for code, identifiers, comments, commits and repo docs.

## Measured baseline (Stand 2026-11-15, branch head df940cbd0f8b = c350c529a491 + spec docs)

**Targeted tests, the 5 existing files** (command below without the new file):
`=== Summary: 5 files, 48 tests passed, 1 failed; FAILED: 1 of 5 files ===`.
The one red is PRE-EXISTING and unrelated:
`tests/hermes_cli/test_kanban_managed_worker_loss.py::test_the_owner_records_the_loss_on_the_board_and_one_tick_disposes_of_it`
— `hermes_bootstrap._settle_interrupted_update` stats
`<install>/.git/worktrees/<wt>/hermes-update-pull` and `tests/home_io_guard.py` refuses it
("TEST BUG: file I/O against the REAL hermes home"). It depends on the worktree living under the
live install; it is not this card's to fix. Every "expected" line below counts it as 1 failed.

**Code-health ratchet** (`/home/birk/.hermes/hermes-agent/venv/bin/python -m scripts.code_health --head HEAD`,
base = merge-base d3d4ee4c7c0e) already reports 4 blocking findings on the branch head, all
inherited from the stacked base feats, none from this card:
- `hermes_cli/kanban_db.py:1 FILE_LINES lines 4252 > 4231`
- `hermes_cli/kanban_db_dispatch.py:1 FILE_LINES lines 2993 > 2905`
- `hermes_cli/kanban_db_dispatch.py:292 BLE001 _current_spawn_log`
- `hermes_cli/main.py:1 FILE_LINES lines 3382 > 3372`

This card adds no finding of its own: the new module meets every target. The wiring grows
`kanban_db_dispatch.py` by about +18 lines (2993 → ~3011; the existing FILE_LINES finding's
number changes, nothing new). We do NOT offset that by moving code out of
`kanban_db_dispatch.py`: every candidate function (`_worker_final_output`,
`_classify_dead_worker*`, `_account_crashes`) is rewritten by `feat/kanban-review-crash-loop` or
its siblings, and moving it would collide at every `hermes-up` (D0). The fork's feats tolerate the
ratchet locally; an upstream PR would need the offset. The merge card records that.

**RED proof on the base** (throwaway replay of this plan's test blocks against the base API, file
deleted afterwards; `git status --short` clean):
- drain exit, ready lane: `STATUS ready FAILURES 0 -> 1 ['crashed']`; review lane:
  `STATUS review FAILURES 0 -> 1 ['review_requested', 'crashed']`
- three drain exits in one sweep: `AUTO_BLOCKED [3 ids]`, `STATUSES [('blocked', 1) x3]` (the
  systemic rule, i.e. the 15.11. incident shape)
- mixed import, file in install written after run start: `consecutive_failures == 1` (counts)
- streak replay: blocked after the 2nd drain death (`failure_limit` 2)
- the real `_worker_final_output` of the fixture logs ends with
  `... Kanban owner refused: Kanban owner silent for 181s: Gateway draining: update_paused` and the
  D2 regex matches it; for the ImportError log the D3 regex captures the exact fixture path.
- the real `gateway_runtime._update_fenced({get_default_hermes_root(), get_hermes_home()})` over a
  temp `HERMES_HOME` answers False with no marker and True with an empty
  `.hermes-update-in-progress` marker (exclusive publish in flight, 5 s grace).

## Plan-level decisions (within the spec, recorded so a reviewer can reject them)

- **P1 — hook placement.** The drain classification is ONE call,
  `kanban_update_drain.classify_update_drain(conn, task_id, dead)`, placed in
  `_reclaim_dead_workers` directly after `apply_attempt_note(...)` and before `_retry_status_for_run`
  / `_end_run`, rather than inside `_classify_dead_worker`. Why: (a) it needs `conn` (run start for
  D3, streak for D5) and `_classify_dead_worker` has none; (b) it runs after `apply_attempt_note`, so
  the protocol-violation flag is final (a lost managed worker loses that flag there); (c)
  `_classify_dead_worker` is the function `feat/kanban-review-crash-loop` rewrote, so leaving its
  body untouched removes a collision point; (d) its input is exactly D2's: the
  `dead.event_payload["worker_output"]` that `_classify_dead_worker` set.
- **P2 — streak helper lives in the new module** (`update_drain_streak`), not in
  `kanban_db_dispatch.py`. It is new behaviour, and that file is over its ratchet cap.
- **P3 — no edits to `kanban_db.py` (`_TICK_ACTIVITY_FIELDS`) or `kanban_ops.py` (`--json`).**
  The spec does not ask for them, and both are sibling-feat collision points. A drained card still
  shows up in `crashed` events and in `DispatchResult.update_drained`.

## File structure

| File | Chain | Responsibility |
|---|---|---|
| `hermes_cli/kanban_update_drain.py` (new) | A | fence probe, matchers, run-start read, streak, classifier hook |
| `hermes_cli/kanban_db_dispatch.py` (modify) | A | `DispatchResult.update_drained`, `_DeadWorker.update_drain` + `run_outcome`, hook call + booking in `_reclaim_dead_workers`, `_CrashSweep.update_drained`, side channel in `detect_crashed_workers`, `_run_reclaim_phase`, `check_respawn_guard` exemption, fence gate, module import |
| `tests/hermes_cli/test_kanban_update_drain.py` (new) | A | spec §3 items 1–6 + D4a, D6/D7 regressions |
| `website/docs/user-guide/features/kanban.md` (modify) | A | one paragraph after the exit-code booking paragraph |
| profile repo `docs/superpowers/specs/2026-10-19-porter-multi-human-design.md` | B | one §4.13 row (D8) |
| profile repo `docs/fabrik-changelog.md` | B | newest-on-top entry |
| profile repo `plugins/porter/tests/test_porter_card_states.py` | B | two contract tests |

## Commands

**TEST (chain A, full targeted set)** — run `mkdir -p /home/birk/scratch/t_bb79440f/tmp` once first
(any scratch dir works; use your card's id in the path):

```
env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_RUN_ID -u HERMES_KANBAN_WORKSPACE HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_bb79440f/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_update_drain.py tests/hermes_cli/test_kanban_preload_failure_block.py tests/hermes_cli/test_kanban_worker_spawn_segment.py tests/hermes_cli/test_kanban_worker_exit_trailer.py tests/hermes_cli/test_kanban_managed_worker_loss.py tests/hermes_cli/test_kanban_owner_receipt_reclaim.py
```

**TEST-NEW (chain A, the new file only)** — same prefix, only the first path:

```
env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK -u HERMES_KANBAN_DB -u HERMES_KANBAN_RUN_ID -u HERMES_KANBAN_WORKSPACE HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_bb79440f/tmp HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/hermes_cli/test_kanban_update_drain.py
```

(`HERMES_TEST_FILE_RETRIES=0` so an expected RED is not retried.)

**TEST-B (chain B):** `cd /mnt/HC_Volume_106183673/hermes/profiles/birk && python3 -m pytest plugins/porter/tests/test_porter_card_states.py -q`
— ANNAHME: baseline `21 passed` (11 `test_classify` params + 10 other tests, counted from the
file; this run's Bash rules forbid paths outside the hermes-agent worktree, so it was not executed).
The chain-B coder checks this baseline first, in their own worktree.

---

# Chain A — core (hermes-agent, one coder card)

Coder worktree: branch `wt/<coder card>` whose base is this plan's branch code
(`feat/kanban-review-crash-loop` @ c350c529a491). Commit after each task.

### Task A0: Re-measure the anchors (no code, no commit)

The line numbers in this plan are from c350c529a491 / df940cbd0f8b.

- [ ] **Step 1: Locate every anchor**

Run: `grep -n -e "class DispatchResult" -e "    rate_limited: list" -e "class _DeadWorker" -e "def run_outcome" -e "def _classify_dead_worker(" -e "class _CrashSweep" -e "def _reclaim_dead_workers" -e "apply_attempt_note(conn" -e "def _account_crashes" -e "is_systemic = fp_counts" -e "def detect_crashed_workers" -e "_last_rate_limited = sweep" -e "latest_outcome !=" -e "def _run_reclaim_phase" -e "result.rate_limited.extend" -e "def _dispatch_once_locked" -e "    if not may_spawn:" -e "^from hermes_cli import kanban_db_workspace" hermes_cli/kanban_db_dispatch.py`

Expected (at c350c529a491): `90 class DispatchResult`, `142 rate_limited`, `1084 class _DeadWorker`,
`1102 def run_outcome`, `1108 def _classify_dead_worker(`, `1220 class _CrashSweep`,
`1224 rate_limited` (the `_CrashSweep` field),
`1233 def _reclaim_dead_workers`, `1267 apply_attempt_note(conn`, `1314 def _account_crashes`,
`1388 is_systemic = fp_counts...`, `1407 def detect_crashed_workers`, `1424 ..._last_rate_limited = sweep...`,
`1707 if err and latest_outcome != "crashed" ...`, `2263 def _run_reclaim_phase`, `2283 result.rate_limited.extend(...)`,
`2408 def _dispatch_once_locked`, `2436 if not may_spawn:`, `2993 from hermes_cli import kanban_db_workspace as _kbw`.
Shifted numbers are fine; the edits below anchor on code text, not on numbers.

- [ ] **Step 2: Check the shapes the spec quotes**

Read `hermes_cli/kanban_db_dispatch.py` around `_account_crashes`, `_classify_dead_worker` and
`_reclaim_dead_workers`. Stop with `FRAGE: <what differs>` if ANY of these no longer holds:
1. `_account_crashes` computes `is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3`
   and passes `failure_limit=1 if is_systemic else None` with `extra["sticky"] = True`.
2. `_classify_dead_worker` sets `dead.event_payload["worker_output"] = worker_output` from
   `_worker_final_output(task_id, board=board)` (current spawn segment only), skipping rate-limited.
3. `_reclaim_dead_workers` calls `_classify_dead_worker(...)`, then `apply_attempt_note(...)`, then
   `_kb._retry_status_for_run(...)`, the `UPDATE tasks ...`, and `_kb._end_run(...)` in that order;
   it appends non-rate-limited deaths to `sweep.crash_details`.
4. `_DeadWorker` has the flags `protocol_violation`, `rate_limited`, `terminal_provider`, `preload_failed`.

- [ ] **Step 3: Baseline the targeted tests**

Run: TEST (the full targeted set; the new file does not exist yet, the runner reports only the 5).
Expected: `=== Summary: 5 files, 48 tests passed, 1 failed; FAILED: 1 of 5 files` (the pre-existing
`test_kanban_managed_worker_loss.py` red, see "Measured baseline"). A different count → note it in
the card and use the measured numbers as "expected" from here on.

---

### Task A1: `kanban_update_drain` — fence probe and matchers (D1, D2, D3)

**Files:**
- Create: `hermes_cli/kanban_update_drain.py`
- Create: `tests/hermes_cli/test_kanban_update_drain.py`

**Interfaces:**
- Consumes: `hermes_cli.gateway_runtime._update_fenced(homes) -> bool`,
  `hermes_constants.get_default_hermes_root() -> Path`, `hermes_constants.get_hermes_home() -> Path`.
- Produces (used by A2/A3):
  - `UPDATE_DRAIN_RE: re.Pattern`, `MIXED_IMPORT_RE: re.Pattern`, `UPDATE_DRAIN_FREE_LIMIT: int = 3`
  - `_install_root() -> Path` (tests patch it)
  - `install_update_fenced() -> bool`
  - `update_drain_kind(worker_output: str, run_started_at: Optional[float]) -> Optional[str]` → `"drain" | "mixed_import" | None`
  - `update_drain_streak(conn: sqlite3.Connection, task_id: str) -> int`
  - `classify_update_drain(conn: sqlite3.Connection, task_id: str, dead: _DeadWorker) -> None` (sets
    `dead.update_drain` — the field A2 adds; A1 tests do not call it)

- [ ] **Step 1: Write the failing tests** — create `tests/hermes_cli/test_kanban_update_drain.py`:

```python
"""An update drain is a run-state event, not a card failure (card t_bb79440f).

On 2026-11-15 two core updates killed 40 workers with their own final text
``Kanban owner refused: ... Gateway draining: update_paused`` (or an ImportError from a
half-updated install); 31 cards were given up after ONE failure because identical fingerprints
tripped the systemic same-error rule. Spec: docs/superpowers/specs/2026-11-15-update-drain-requeue-design.md
(each test names its D-number).
"""

from __future__ import annotations

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
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST-NEW
Expected: the file errors at collection: `ModuleNotFoundError: No module named 'hermes_cli.kanban_update_drain'`
(summary line ends `FAILED: 1 of 1 file`).

- [ ] **Step 3: Write the module** — create `hermes_cli/kanban_update_drain.py`:

```python
"""Update drains and mixed-version imports: a host event, never a card failure.

``hermes update`` fences the install and drains the gateway. A worker spawned into the fence, or
running when the drain hits, dies with our own owner-refused text
(``gateway_runtime._fenced_verdict`` -> ``gateway_client`` -> ``kanban_worker_client``); one that
imported half the old and half the new tree dies on an ImportError. On 2026-11-15 two updates
killed 40 workers that way and 31 cards were given up after one failure (t_bb79440f). The
dispatcher requeues such deaths without counting (bounded by a streak) and claims nothing while
the fence is up.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import _DeadWorker

_log = logging.getLogger(__name__)

# Anchored at the end of the worker's final output: the drain must be what killed the worker,
# not a line it printed earlier in the run.
UPDATE_DRAIN_RE = re.compile(
    r"Kanban owner refused: (?:Kanban owner silent for \d+s: )?Gateway draining: update_paused\s*$"
)
MIXED_IMPORT_RE = re.compile(
    r"ImportError: cannot import name '\w+' from '[\w.]+' \(([^()]+\.py)\)\s*$"
)
# Free requeues in a row before an update death counts again. Each costs >= OWNER_WAIT_S (180 s),
# so a stuck fence wastes at most ~12 min of spawns per card.
UPDATE_DRAIN_FREE_LIMIT = 3
_STREAK_SCAN_LIMIT = 50


def _install_root() -> Path:
    """The checkout this process runs from; a card's own worktree is never under it."""
    import hermes_cli

    return Path(hermes_cli.__file__).resolve().parents[1]


def install_update_fenced() -> bool:
    """True while an update holds the install (marker in the install root or this profile's home).

    Fail-open: a broken probe must not stop the factory, so any error reads as "not fenced".
    """
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.gateway_runtime import _update_fenced

        return _update_fenced({get_default_hermes_root(), get_hermes_home()})
    except Exception:
        _log.debug("update-fence probe failed; dispatching as if unfenced", exc_info=True)
        return False


def update_drain_kind(worker_output: str, run_started_at: Optional[float]) -> Optional[str]:
    """``"drain"`` / ``"mixed_import"`` when the worker's final output says an update killed it.

    A mixed import also needs the named file inside the install root AND written after the run
    started: a card's worktree code never qualifies, and a genuinely broken new tree fails again
    after its mtime and then counts.
    """
    if UPDATE_DRAIN_RE.search(worker_output):
        return "drain"
    match = MIXED_IMPORT_RE.search(worker_output)
    if match is None or run_started_at is None:
        return None
    path = Path(match.group(1)).resolve()
    if not path.is_relative_to(_install_root().resolve()):
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return "mixed_import" if mtime > run_started_at else None


def _open_run_started_at(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    """``task_runs.started_at`` of the task's still-open run (D3 compares against the run,
    not ``tasks.started_at``)."""
    row = conn.execute(
        "SELECT r.started_at FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    return float(row["started_at"]) if row is not None else None


def update_drain_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Trailing closed runs booked ``update_drain``, newest first. ``rate_limited`` runs are
    neutral (as in ``_protocol_violation_streak``); any other closed run ends the streak."""
    streak = 0
    rows = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _STREAK_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        if row["outcome"] == "rate_limited":
            continue
        if row["outcome"] != "update_drain":
            break
        streak += 1
    return streak


def classify_update_drain(conn: sqlite3.Connection, task_id: str, dead: _DeadWorker) -> None:
    """Mark ``dead`` as an update death unless a more specific booking already owns it.

    Call before ``_end_run``: the run start is the still-open run's. Past
    ``UPDATE_DRAIN_FREE_LIMIT`` free requeues in a row the death stays a crash and counts.
    """
    if dead.rate_limited or dead.protocol_violation or dead.terminal_provider or dead.preload_failed:
        return
    output = dead.event_payload.get("worker_output") or ""
    kind = update_drain_kind(output, _open_run_started_at(conn, task_id))
    if kind is None:
        return
    if update_drain_streak(conn, task_id) >= UPDATE_DRAIN_FREE_LIMIT:
        dead.event_payload["update_drain_exhausted"] = kind
        return
    dead.update_drain = dead.event_payload["update_drain"] = kind
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: TEST-NEW
Expected: `=== Summary: 1 files, 12 tests passed, 0 failed`

- [ ] **Step 5: Commit**

```
git add hermes_cli/kanban_update_drain.py tests/hermes_cli/test_kanban_update_drain.py
git commit -m "feat(kanban): update-drain matchers and install-fence probe (t_bb79440f)"
```

---

### Task A2: Book update deaths as a free requeue (D4, D4a, D5, D6)

**Files:**
- Modify: `hermes_cli/kanban_db_dispatch.py` — `DispatchResult` (after `rate_limited`, ~l. 142-145),
  `_DeadWorker` (~l. 1084-1105), `_CrashSweep` (~l. 1220-1230), `_reclaim_dead_workers` (~l. 1263-1310),
  `detect_crashed_workers` (~l. 1424), `check_respawn_guard` (~l. 1701-1708), `_run_reclaim_phase`
  (~l. 2283), the module-bottom imports (~l. 2990-2993)
- Test: `tests/hermes_cli/test_kanban_update_drain.py` (append)

**Interfaces:**
- Consumes (A1): `kud.classify_update_drain(conn, task_id, dead) -> None`.
- Produces: `DispatchResult.update_drained: list[str]`, `_DeadWorker.update_drain: Optional[str]`,
  `_DeadWorker.run_outcome == "update_drain"` when set, `_CrashSweep.update_drained: list[str]`,
  `detect_crashed_workers._last_update_drained: list[str]`.

- [ ] **Step 1: Append the failing tests** to `tests/hermes_cli/test_kanban_update_drain.py`, and
add `import json` to its stdlib imports (above `import os`):

```python
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
```

- [ ] **Step 2: Run them to verify the right ones fail**

Run: TEST-NEW
Expected: `12 + 15 = 27` tests, `8 failed, 19 passed`. The 8 reds:
`test_drain_death_requeues_without_counting[ready-lane]`, `[review-lane]` (failures 0 → 1, outcome
`crashed`); `test_three_drain_deaths_in_one_sweep_block_none` (`AttributeError: ... 'update_drained'`);
`test_mixed_version_import_error_books_by_root_and_run_start[new-file-in-install]`;
`test_drain_streak_is_capped[fourth-drain-counts]`, `[a-crash-breaks-the-streak]`, `[rate-limit-is-neutral]`
(blocked at the 2nd drain death → `assert claimed is not None`);
`test_a_drained_card_whose_output_mentions_auth_is_not_parked` (outcome `crashed`, not `update_drain`;
its guard assertion is proven separately by mutant M5 in Task A5, because a `crashed` outcome is
already exempt today). Green already (regression contracts): the 4 `test_other_deaths_still_count`
cases, the systemic rule, and the two counting mixed-import cases.

- [ ] **Step 3: `DispatchResult.update_drained`** — in `hermes_cli/kanban_db_dispatch.py`, directly
after the `rate_limited` field and its docstring (before `skipped_locked`), insert:

```python
    update_drained: list[str] = field(default_factory=list)
    """Task ids whose worker an update drain or a mixed-version import killed (kanban_update_drain),
    released to their source phase WITHOUT counting a failure."""
```

- [ ] **Step 4: `_DeadWorker.update_drain` and `run_outcome`** — after the `preload_failed` field's
docstring insert:

```python
    update_drain: Optional[str] = None
    """``"drain"`` / ``"mixed_import"`` (kanban_update_drain): requeued without counting."""
```

and replace the `run_outcome` property body:

```python
    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall; an update death likewise.
        if self.rate_limited:
            return "rate_limited"
        return "update_drain" if self.update_drain else "crashed"
```

- [ ] **Step 5: `_CrashSweep.update_drained`** — after `rate_limited: list[str] = field(default_factory=list)`
in `_CrashSweep` insert:

```python
    update_drained: list[str] = field(default_factory=list)
```

- [ ] **Step 6: Hook and booking in `_reclaim_dead_workers`** — directly after
`apply_attempt_note(conn, row["id"], pid, dead)` insert:

```python
            _kud.classify_update_drain(conn, row["id"], dead)  # before _end_run: reads the open run (D3)
```

Then change the stamp condition and the bucket split at the end of the loop body from

```python
            if dead.rate_limited or dead.protocol_violation:
```

to

```python
            if dead.rate_limited or dead.protocol_violation or dead.update_drain:
```

and from

```python
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            else:
```

to

```python
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.update_drain:
                sweep.update_drained.append(row["id"])  # never _account_crashes: no systemic count
            else:
```

(The comment above the stamp stays as is; it already explains stamping without touching
`consecutive_failures`.)

- [ ] **Step 7: Side channel and result** — in `detect_crashed_workers`, after
`detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]` insert:

```python
    detect_crashed_workers._last_update_drained = sweep.update_drained  # type: ignore[attr-defined]
```

In `_run_reclaim_phase`, after `result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))` insert:

```python
    result.update_drained.extend(getattr(detect_crashed_workers, "_last_update_drained", []))
```

- [ ] **Step 8: Respawn guard (D4a)** — in `check_respawn_guard` replace

```python
    if err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(err):
```

with

```python
    if err and latest_outcome not in ("crashed", "update_drain") and _RESPAWN_BLOCKER_RE.search(err):
```

and extend the comment above it in place (no new line): `... A plain crash (or an update drain) is different: ...`.

- [ ] **Step 9: Import** — at the bottom of the module, after
`from hermes_cli import kanban_db_workspace as _kbw` add:

```python
from hermes_cli import kanban_update_drain as _kud
```

(Bottom-of-module like its neighbours; `kanban_update_drain` imports nothing from the dispatcher at
runtime, so there is no cycle.)

- [ ] **Step 10: Run the tests to verify they pass**

Run: TEST-NEW
Expected: `=== Summary: 1 files, 27 tests passed, 0 failed`

Run: TEST
Expected: `=== Summary: 6 files, 75 tests passed, 1 failed; FAILED: 1 of 6 files` — the 1 is the
pre-existing `test_kanban_managed_worker_loss.py` red; any other red is a regression and must be fixed.

- [ ] **Step 11: Commit**

```
git add hermes_cli/kanban_db_dispatch.py tests/hermes_cli/test_kanban_update_drain.py
git commit -m "fix(kanban): requeue update-drain deaths without counting a failure (t_bb79440f)"
```

---

### Task A3: Claim nothing while an update holds the install (D1)

**Files:**
- Modify: `hermes_cli/kanban_db_dispatch.py` — `_dispatch_once_locked` (~l. 2433-2437)
- Test: `tests/hermes_cli/test_kanban_update_drain.py` (append)

**Interfaces:**
- Consumes (A1): `kud.install_update_fenced() -> bool` (looked up on the module at call time, so
  `monkeypatch.setattr(kud, "install_update_fenced", ...)` reaches it).
- Produces: nothing new; `dispatch_once` returns early with reclaim/promotion done.

- [ ] **Step 1: Append the failing test**

```python
# --- D1: the dispatcher claims nothing while an update holds the install ----------------------


def test_no_claims_while_the_install_is_update_fenced(
    kanban_home, all_assignees_spawnable, monkeypatch, caplog,
):
    """D1 (§3.6): fence up -> nothing spawned, ready stays ready, one warning; fence down -> spawns."""
    fence = {"up": True}
    monkeypatch.setattr(kud, "install_update_fenced", lambda: fence["up"])
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    spawns: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="a", assignee="alice")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

        assert spawns == [] and res.spawned == []
        assert kb.get_task(conn, tid).status == "ready"
        assert caplog.text.count("an update holds the install") == 1

        fence["up"] = False
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [tid]
    assert [t for t, _, _ in res.spawned] == [tid]
```

- [ ] **Step 2: Run it to verify it fails**

Run: TEST-NEW
Expected: `1 failed, 27 passed`; the red is `test_no_claims_while_the_install_is_update_fenced`
(`assert [...] == []` — the fenced tick spawned).

- [ ] **Step 3: The gate** — in `_dispatch_once_locked`, directly after

```python
    if not may_spawn:
        return result
```

insert

```python
    if _kud.install_update_fenced():
        _kb._log.warning("kanban dispatch: an update holds the install; "
                         "spawning no new workers this tick (deferred, not dropped)")
        return result
```

(After the reclaim phase and `_tick_spawn_budget`, before any lane row is read or claimed; the
memory-pressure lines inside `_tick_spawn_budget` stay untouched.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: TEST-NEW → `=== Summary: 1 files, 28 tests passed, 0 failed`
Run: TEST → `=== Summary: 6 files, 76 tests passed, 1 failed; FAILED: 1 of 6 files` (pre-existing red only)

- [ ] **Step 5: Commit**

```
git add hermes_cli/kanban_db_dispatch.py tests/hermes_cli/test_kanban_update_drain.py
git commit -m "fix(kanban): claim no new work while an update holds the install (t_bb79440f)"
```

---

### Task A4: User docs

**Files:**
- Modify: `website/docs/user-guide/features/kanban.md` — new paragraph after the paragraph that ends
  ``... a worker killed before it reaches that line is a plain `crashed` (`pid <n> not alive`).`` (~l. 600),
  before `**Agent-side prevention:**`.

- [ ] **Step 1: Insert the paragraph** (blank line before and after):

```markdown
**Update drains are not failures.** While `hermes update` holds the install (its
`.hermes-update-in-progress` fence), the dispatcher claims and spawns nothing: it logs
`kanban dispatch: an update holds the install; spawning no new workers this tick (deferred, not
dropped)` and still runs reclaim and promotion. A worker that dies anyway because the update drained
its gateway (its last output ends with `Kanban owner refused: … Gateway draining: update_paused`), or
because it imported half of an old and half of a new install (an `ImportError` naming a file inside
the install that the update wrote after the run started), goes back to `ready` (or `review`) without
counting a failure. The run is recorded as `update_drain`, the `crashed` event carries
`update_drain: drain` or `mixed_import`, and such deaths never feed the same-error rule that blocks
a whole wave of cards after one attempt. Three update deaths in a row are free; the fourth counts
like any crash.
```

- [ ] **Step 2: Commit**

```
git add website/docs/user-guide/features/kanban.md
git commit -m "docs(kanban): update drains requeue without counting a failure (t_bb79440f)"
```

---

### Task A5: Mutants, ratchet delta, final run (no commit unless a fix is needed)

Each mutant: apply the ONE change, run TEST-NEW, confirm the named tests go RED, then restore with
`git checkout -- <file>` and confirm `git status --short` is clean. Record each result (red test
names) in the card's handoff.

| # | D | File: one-line change | Must go RED |
|---|---|---|---|
| M1 | D4 | `kanban_update_drain.py`: `dead.update_drain = dead.event_payload["update_drain"] = kind` → `dead.event_payload["update_drain"] = kind` (drain booked as crash, `failures` moves) | `test_drain_death_requeues_without_counting[ready-lane]`, `[review-lane]`, `test_three_drain_deaths_in_one_sweep_block_none`, `test_drain_streak_is_capped[*]`, `test_mixed_version_import_error_books_by_root_and_run_start[new-file-in-install]`, `test_a_drained_card_whose_output_mentions_auth_is_not_parked` |
| M2 | D4 | `kanban_db_dispatch.py` `_reclaim_dead_workers`: `sweep.update_drained.append(row["id"])` → `sweep.update_drained.append(row["id"]); sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))` (drained deaths reach `_account_crashes`, systemic trip at 3) | `test_three_drain_deaths_in_one_sweep_block_none` (auto_blocked / blocked), `test_drain_death_requeues_without_counting[*]` |
| M3 | D3 | `kanban_update_drain.py`: `return "mixed_import" if mtime > run_started_at else None` → `return "mixed_import"` (old-tree ImportError requeued) | `test_a_mixed_import_needs_a_new_file_inside_the_install[file-older-than-run]`, `test_mixed_version_import_error_books_by_root_and_run_start[file-older-than-run]` |
| M4 | D1 | `kanban_db_dispatch.py` `_dispatch_once_locked`: `if _kud.install_update_fenced():` → `if False:` (no fence gate) | `test_no_claims_while_the_install_is_update_fenced` |
| M5 | D4a | `kanban_db_dispatch.py`: `latest_outcome not in ("crashed", "update_drain")` → `latest_outcome != "crashed"` | `test_a_drained_card_whose_output_mentions_auth_is_not_parked` (`'blocker_auth' is None`) |
| M6 | D5 | `kanban_update_drain.py`: `>= UPDATE_DRAIN_FREE_LIMIT` → `> UPDATE_DRAIN_FREE_LIMIT` (a 4th free requeue) | `test_drain_streak_is_capped[fourth-drain-counts]`, `[rate-limit-is-neutral]` (`[a-crash-breaks-the-streak]` stays green: its final streak is only 2) |
| M7 | D3 | `kanban_update_drain.py`: `if not path.is_relative_to(_install_root().resolve()):` → `if False:` (card worktree code qualifies) | `test_a_mixed_import_needs_a_new_file_inside_the_install[card-worktree-file]`, `test_mixed_version_import_error_books_by_root_and_run_start[card-worktree-file]` |
| M8 | D4a | `kanban_update_drain.py` `_open_run_started_at`: SQL → `"SELECT started_at FROM tasks WHERE id = ?"` (task start instead of run start) | `test_mixed_version_import_error_books_by_root_and_run_start[new-file-in-install]` |
| M9 | D2 | `kanban_update_drain.py`: drop `\s*$` from `UPDATE_DRAIN_RE` | `test_the_drain_text_counts_only_as_the_final_output[drain-not-last]`, `test_other_deaths_still_count[drain-not-last]` |

Note on M9: dropping only the `$` and keeping `\s*` is NOT a valid mutant for a test whose drain text
is last (it still matches). It goes red here only because the `drain-not-last` case exists; keep
that case.

- [ ] **Step 1: Run M1..M9** as described; every "Must go RED" test is red, and the tree is clean
  after each restore. A mutant that stays green means a missing test: add it (in the test file),
  re-run, commit `test(kanban): pin <D-number> (t_bb79440f)`.

- [ ] **Step 2: Ratchet delta**

Run: `/home/birk/.hermes/hermes-agent/venv/bin/python -m scripts.code_health --head HEAD`
Expected: the same 4 pre-existing findings and no other finding. The `kanban_db_dispatch.py`
FILE_LINES number grows from 2993 to about 3011 (the wiring, +18 ±3). No finding may name
`hermes_cli/kanban_update_drain.py` or `tests/hermes_cli/test_kanban_update_drain.py`; if one does
(e.g. BLE001 because `exc_info=True` was dropped), fix it and commit.

- [ ] **Step 3: Final targeted run**

Run: TEST
Expected: `=== Summary: 6 files, 76 tests passed, 1 failed; FAILED: 1 of 6 files` (only the
pre-existing `test_kanban_managed_worker_loss.py` red). Do not run the full suite; cc-run.py does.

- [ ] **Step 4: Hand-off line for the merge card** — list the code commit SHAs of A1–A4 (and any
A5 test commit) in the card's completion summary. None of them touch `docs/superpowers/`.

---

### Merge step (chain A, separate merge card; not the coder card)

1. In the main clone `/mnt/HC_Volume_106183673/hermes/hermes-agent`, create
   `feat/kanban-update-drain-requeue` from `feat/kanban-review-crash-loop` and cherry-pick ONLY the
   code commits from the coder card's `wt/<coder card>` (A1–A4 and any A5 test commit). Never the
   `docs/superpowers/` commits (spec, probes, this plan). Check with `git show --stat <sha>` per
   commit that no path under `docs/superpowers/` is included.
2. `~/.hermes/hermes-agent-features.txt`: one block directly below the `feat/kanban-review-crash-loop`
   line (currently l. 1002), in the file's comment style:
   ```
   # ── feat/kanban-update-drain-requeue ────────────────────────────────────────
   #   Karte: t_bb79440f (Plan) + <coder card> (Code) + <review card> + <merge card>.
   #   Summary: an update drain / mixed-version ImportError requeues the card
   #   without counting a failure (outcome update_drain, max 3 in a row), and
   #   the dispatcher claims nothing while `hermes update` holds the install.
   #   Stacked on feat/kanban-review-crash-loop (D0).
   #   Upstream-PR: nein. Ratchet: kanban_db_dispatch.py FILE_LINES grows ~+18
   #   (pre-existing finding of the stack; an upstream PR needs an offset).
   #   Area: Kanban / dispatcher
   feat/kanban-update-drain-requeue
   ```
3. Acceptance 7 (spec §3): `python3 ~/.hermes/profiles/birk/scripts/fabrik-merge-check.py --repo /mnt/HC_Volume_106183673/hermes/hermes-agent --branch feat/kanban-update-drain-requeue --target birk-local`
   → clean (no conflict with any active sibling feat). A conflict → `FRAGE` with the conflicting paths.
4. `python3 ~/.hermes/profiles/birk/scripts/autoupdate-push-ready.py mark feat/kanban-update-drain-requeue`
   instead of a raw push.
5. Live effect needs `hermes-up` + a gateway restart. That is Birk's step; the merge card does not do it.

---

# Chain B — Pforte catalogue (profile repo, one coder card, independent of chain A)

Repo: `/mnt/HC_Volume_106183673/hermes/profiles/birk`, branch `wt/<coder card>` from `origin/main`.
No code dependency on chain A: `porter/card_states.py` already leaves `update_drain` out of
`FAILING`, so the change is catalogue + a contract test that pins it. It may land first.

### Task B1: §4.13 row, changelog entry, porter contract tests (D8) — ONE commit

**Files:**
- Modify: `docs/superpowers/specs/2026-10-19-porter-multi-human-design.md` §4.13 table (~l. 604-612)
- Modify: `docs/fabrik-changelog.md` (new entry on top)
- Test: `plugins/porter/tests/test_porter_card_states.py` (append)

**Interfaces:**
- Consumes: `card_states.classify(task_row, runs, decision_classes) -> CardState`,
  `card_states.scan(porter_home, humans, cfg, now) -> dict` and the file's own helpers `row`,
  `make_db`, `add_card`, `add_runs`, `cfg`, `notes`, `H`, `NOW`, `CLASSES`.
- Produces: nothing new.

- [ ] **Step 1: Baseline**

Run: TEST-B. Expected (ANNAHME, see Commands): `21 passed`. Record the real number.

- [ ] **Step 2: Append the tests** to `plugins/porter/tests/test_porter_card_states.py`:

```python
def test_update_drain_runs_neither_count_nor_reach_a_human():
    """hermes-agent t_bb79440f (D8): an `update_drain` run is the core's own self-fix (requeued
    without counting), so it never raises the failure count and never routes to a human."""
    runs = [{"id": i, "outcome": o} for i, o in enumerate(["crashed", "update_drain", "update_drain"], 1)]
    st = card_states.classify(row(), runs, CLASSES)
    assert st.failures == 1
    assert st.route != "human"


def test_update_drains_never_complete_a_failure_streak(tmp_path):
    """D8: two real failures plus three update drains stay below error_after_failures=3."""
    db = make_db(tmp_path / "kanban.db")
    add_card(db, "t_drain")
    add_runs(db, "t_drain", "crashed", "crashed", "update_drain", "update_drain", "update_drain")
    out = card_states.scan(tmp_path, H, cfg(db), NOW)
    assert out["errors"] == 0 and notes(tmp_path) == []
```

- [ ] **Step 3: Run them** — TEST-B. Expected: baseline + 2 passed (ANNAHME `23 passed`). They are
contract tests and are green on the current code. Prove them with the mutant: in
`plugins/porter/card_states.py` change
`SELF_FIX_OUTCOMES = ("crashed", "timed_out", "stale", "spawn_failed", "rate_limited")` to add
`"update_drain"` → both new tests RED (`failures == 3`, `errors == 1`); restore with
`git checkout -- plugins/porter/card_states.py`.

- [ ] **Step 4: §4.13 row** — in the §4.13 table, directly below the row that starts
`| crashed, timed_out, stale, spawn_failed, rate_limited, ...`, insert:

```markdown
| update drain / mixed-version crash (run outcome `update_drain`: the worker died on `Gateway draining: update_paused`, or on an ImportError from a half-updated install) | **self-fix by the core, never the human** | the dispatcher requeues the card without counting a failure (at most 3 in a row) and claims no new work while an update holds the install (hermes-agent `feat/kanban-update-drain-requeue`, t_bb79440f). Not in `card_states.FAILING`: it never counts toward the 3rd-failure `error` item |
```

- [ ] **Step 5: Changelog entry** — in `docs/fabrik-changelog.md`, insert as the FIRST `## ` entry
(directly above the current newest `## 2026-11-15 — cc-run run rule 7 allows pushing …`), with the
date of the commit day:

```markdown
## 2026-11-15 — Pforte catalogue: update drains are a core self-fix, never a failure

- **Reason:** on 2026-11-15 two core updates killed 40 workers with `Gateway draining: update_paused` (or a mixed-version ImportError); 31 cards were given up after ONE failure (t_bb79440f). The §4.13 catalogue had no row for this class.
- **Change:** §4.13 of `docs/superpowers/specs/2026-10-19-porter-multi-human-design.md` gains the row "update drain / mixed-version crash → self-fix by the core, never the human, does not count toward the 3rd-failure error item"; `plugins/porter/tests/test_porter_card_states.py` pins that an `update_drain` run neither raises the failure count nor reaches a human.
- **Tell:** a card with `update_drain` runs shows no "Fehler" item and no `card_error` routing record for them; the core side is hermes-agent `feat/kanban-update-drain-requeue`.
- **Commits/cards:** this commit, t_bb79440f
```

- [ ] **Step 6: Commit (one commit, D8)**

```
git add docs/superpowers/specs/2026-10-19-porter-multi-human-design.md docs/fabrik-changelog.md plugins/porter/tests/test_porter_card_states.py
git commit -m "docs(porter): update drains are a core self-fix in the 4.13 catalogue (t_bb79440f)"
```

(`docs/superpowers/` may be gitignored in the profile repo as well; if `git add` refuses the spec,
use `git add -f` for that path.)

---

## Spec coverage (self-review)

| Spec item | Task |
|---|---|
| D0 base | Global Constraints, Merge step 1 |
| D1 fence gate + wrapper + fail-open + one warning | A1 (wrapper, tests), A3 (gate, test), M4 |
| D2 drain regex on `worker_output`, anchored | A1 (`UPDATE_DRAIN_RE`, tests), A2 (hook reads the payload), M9 |
| D3 mixed import: root + mtime > run start | A1 (`update_drain_kind`), A2 integration test, M3, M7 |
| D4 booking: source phase, failures unchanged, `last_failure_error`, outcome, event payload, `update_drained` bucket, not `crash_details`, `DispatchResult` placement | A2 steps 3–7, M1, M2 |
| D4a respawn-guard exemption; run start from `task_runs` read before `_end_run`; `_install_root()` patchable | A2 step 8, A1 (`_open_run_started_at`, `_install_root`), M5, M8 |
| D5 streak cap 3, rate_limited neutral | A1 (`update_drain_streak`), A2 test, M6 |
| D6 regression contract, systemic rule unchanged | A2 (`test_other_deaths_still_count`, systemic test) |
| D7 out-of-scope shapes stay crashes | A1/A2 parametrized cases `one-shot-worker-text`, `service-identity` |
| D8 catalogue row + changelog + porter test | Chain B |
| D9 standard tools, kanban fixture style | Commands, test file fixtures |
| §3.1–§3.6 | A2 (1–5), A3 (6) |
| §3.7 merge-tree vs birk-local | Merge step 3 |
