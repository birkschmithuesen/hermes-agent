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


def releasing_comment_author(
    conn: sqlite3.Connection, task_id: str, *, release_authors: tuple[str, ...] = (),
) -> Optional[str]:
    """Author of the first comment newer than the parking event that may wake the card, else None.

    The card's own worker (its assignee) and the auto-decomposer never count. With
    ``release_authors`` set (``kanban.loop_breaker_release_authors``) only those authors
    count, so an automation comment (a factory parking note) cannot wake it.
    """
    from hermes_cli.kanban_db import _json_dict

    parking_id = _parking_event_id(conn, task_id)
    if parking_id is None:
        return None
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    own = {AUTO_DECOMPOSER_AUTHOR, (row["assignee"] or "") if row else ""}
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented' AND id > ? "
        "ORDER BY id", (task_id, parking_id),
    ).fetchall()
    for event in events:
        author = str(_json_dict(event["payload"]).get("author") or "").strip()
        if author and author not in own and (not release_authors or author in release_authors):
            return author
    return None


def release_commented(conn: sqlite3.Connection, *, release_authors: tuple[str, ...] = ()) -> list[str]:
    """Promote every loop-parked triage card that a releasing comment woke, AS-IS:
    ``specify_triage_task`` with no title/body, so the commissioner's body is never
    LLM-rewritten. Returns the promoted ids. Call outside any write txn."""
    from hermes_cli.kanban_db import list_tasks, specify_triage_task

    return [
        task.id for task in list_tasks(conn, status="triage")
        if releasing_comment_author(conn, task.id, release_authors=release_authors)
        and specify_triage_task(conn, task.id)
    ]
