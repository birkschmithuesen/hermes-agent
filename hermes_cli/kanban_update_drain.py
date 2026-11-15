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
    if row is None:
        return None
    # The board's own reader: ISO text or garbage in the column (#97455) must never abort the sweep.
    from hermes_cli.kanban_db import _to_epoch

    started = _to_epoch(row["started_at"])
    return float(started) if started is not None else None


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
