"""The unblock-loop breaker: route a repeated same-cause block to ``triage`` and
recognise the cards it parked there.

A card is *loop-parked* when it sits in ``triage`` and the newest event that put
it there is ``block_loop_detected``. Automation (the gateway auto-decompose
tick, ``specify --all`` / ``decompose --all``) leaves such a card alone:
promoting it respawned a worker into the same wall every tick (t_95a7e463).
It leaves triage through an explicit human action (``unblock``, single-id
``specify`` / ``decompose``, the dashboard) or a comment from someone other
than its own worker, which the tick honours by promoting the card as-is.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

# Audit author of the gateway tick; its own writes never count as a human reply.
AUTO_DECOMPOSER_AUTHOR = "auto-decomposer"


def route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for :func:`kanban_db.block_task`.

    ``dependency`` never enters the human ``blocked`` bucket: it waits in
    ``todo`` for ``recompute_ready``, so a cron never sees a dependency-wait
    as something to "unblock". Callers that pass ``dependency`` with no
    incomplete parent are re-kinded to ``needs_input`` before this runs
    (see :func:`kanban_db.block_task`). Every other kind counts unblock-loop
    recurrences: block_task only fires from running/ready (AFTER an unblock
    returned the task to the pool), so a stored ``block_kind`` equal to the
    incoming one means blocked -> unblocked -> re-block for the same cause
    (un-typed None compares equal to a prior un-typed block). At
    ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def _entered_triage(kind: str, payload: dict) -> bool:
    """Whether an event of a triage-entering kind actually moved the card INTO triage:
    creation (a card in triage that was never moved was created there), the loop
    breaker, a dashboard drag/PATCH to triage, or a board import that re-parked it."""
    if kind in ("created", "block_loop_detected"):
        return True
    if kind == "status":
        return payload.get("status") == "triage"
    return kind == "imported" and bool(payload.get("parked"))


def _parking_event_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """Id of the ``block_loop_detected`` event that put ``task_id`` into triage, else
    None (the newest triage entry was something else). Does not check the status."""
    from hermes_cli.kanban_db import _json_dict

    rows = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('created', 'block_loop_detected', 'status', 'imported') "
        "ORDER BY id DESC", (task_id,),
    ).fetchall()
    for row in rows:
        if _entered_triage(row["kind"], _json_dict(row["payload"])):
            return int(row["id"]) if row["kind"] == "block_loop_detected" else None
    return None


def is_loop_parked(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when ``task_id`` is in ``triage`` because the unblock-loop breaker put it there."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row) and row["status"] == "triage" and _parking_event_id(conn, task_id) is not None


def loop_parked_ids(conn: sqlite3.Connection, task_ids: Iterable[str]) -> set[str]:
    """The subset of ``task_ids`` that are loop-parked (automation must skip them)."""
    return {tid for tid in task_ids if is_loop_parked(conn, tid)}
