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
from typing import Any, Iterable, Optional

# Audit author of the gateway tick; its own writes never count as a human reply.
AUTO_DECOMPOSER_AUTHOR = "auto-decomposer"


def parse_release_authors(raw: Any) -> tuple[str, ...]:
    """``kanban.loop_breaker_release_authors`` (list or comma-separated string) -> names; unset/blank -> ()."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(a.strip() for a in raw if isinstance(a, str) and a.strip())


def _event_author(payload: Any) -> str:
    """Stripped ``author`` of a ``commented`` event payload ("" when absent)."""
    from hermes_cli.kanban_db import _json_dict

    return str(_json_dict(payload).get("author") or "").strip()


def _is_reply_author(author: str, *, assignee: Optional[str], release_authors: tuple[str, ...]) -> bool:
    """Whether a comment by ``author`` can be the commissioner answering the card.

    Never the card's own worker (its assignee) nor the auto-decomposer; with
    ``release_authors`` set (``kanban.loop_breaker_release_authors``) only those names.
    Shared by the loop-parked release path and the reply reset of :func:`route_block`.
    """
    if not author or author in (AUTO_DECOMPOSER_AUTHOR, assignee or ""):
        return False
    return not release_authors or author in release_authors


# Comments the ``hermes kanban`` CLI writes itself around block/schedule/unblock/reopen
# (``hermes_cli/kanban.py`` ``_commented`` and ``_cmd_reopen_review``): an operation's
# annotation, never an answer. ``UNBLOCK: ...`` counting would make every annotated
# unblock reset the loop count -- the amnesia ``unblock_task`` refuses.
CLI_AUDIT_PREFIXES = ("BLOCKED: ", "SCHEDULED: ", "UNBLOCK: ", "CHANGES REQUESTED: ")

# Same-kind blocks per completion cycle (this one included) after which even an answered
# re-block is a loop. Measured: 44 cards had 1 answered re-block, 11 had 2, 6 had 3+.
BLOCK_REPLY_CEILING = 5

_BLOCK_EVENT_KINDS = ("blocked", "block_loop_detected")


def _carries_reply_body(conn: sqlite3.Connection, task_id: str, author: str, created_at: int) -> bool:
    """Whether the comment behind a ``commented`` event is more than a CLI audit note.

    The event payload carries no comment id, so the row is matched on task and author
    within one second before the event: ``add_comment`` reads its timestamp just before
    opening the txn that writes both, so the row can be a second-boundary older. Any
    matching non-audit body counts; no matching row (a longer lock wait) does not (fail
    closed)."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = ? "
        "AND created_at BETWEEN ? AND ?",
        (task_id, author, created_at - 1, created_at),
    ).fetchall()
    return any(not str(row["body"]).startswith(CLI_AUDIT_PREFIXES) for row in rows)


def _configured_release_authors() -> Optional[tuple[str, ...]]:
    """``kanban.loop_breaker_release_authors`` of the calling profile; None when the
    config cannot be read (the reset then fails closed)."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        cfg = load_config_readonly() or {}
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- fail closed: any unreadable config means no reset, never a crash in route_block
        return None
    kcfg = cfg.get("kanban") if isinstance(cfg, dict) else None
    return parse_release_authors(kcfg.get("loop_breaker_release_authors") if isinstance(kcfg, dict) else None)


def _reply_author(conn: sqlite3.Connection, task_id: str, comments: list) -> Optional[str]:
    """Author of the first reply among ``comments`` (``commented`` event rows since the
    latest block, in id order), else None. Reads the config only once a candidate passed
    the author and audit-note checks."""
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assignee = row["assignee"] if row else None
    candidates = []
    for event in comments:
        author = _event_author(event["payload"])
        if _is_reply_author(author, assignee=assignee, release_authors=()) and _carries_reply_body(
            conn, task_id, author, int(event["created_at"]),
        ):
            candidates.append(author)
    if not candidates:
        return None
    release_authors = _configured_release_authors()
    if release_authors is None:
        return None
    return next((a for a in candidates if not release_authors or a in release_authors), None)


def _same_reason(block_event, reason: Optional[str]) -> bool:
    """Whether ``reason`` repeats the reason of ``block_event`` verbatim (stripped; None == "")."""
    from hermes_cli.kanban_db import _json_dict

    previous = _json_dict(block_event["payload"]).get("reason")
    return str(previous or "").strip() == str(reason or "").strip()


def block_loop_inputs(
    conn: sqlite3.Connection, task_id: str, kind: Optional[str], reason: Optional[str],
) -> dict[str, Any]:
    """The event-log inputs of :func:`route_block` for a new block of ``kind``, read inside
    :func:`kanban_db.block_task`'s write txn: ``replied_by`` (the reply between the latest
    ``blocked``/``block_loop_detected`` event and now) and ``prior_same_kind_blocks`` (same-kind
    block events since the latest ``completed``). Everything is ordered by event id.

    ``replied_by`` stays None when ``reason`` repeats the latest block's reason verbatim:
    a worker re-asking word for word after an answer is an automated re-ask, still a loop.
    Real new questions differ in text (436 of 437 measured loop events)."""
    from hermes_cli.kanban_db import _json_dict

    inputs: dict[str, Any] = {"replied_by": None, "prior_same_kind_blocks": 0}
    if kind == "dependency":
        return inputs
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'block_loop_detected', 'commented', 'completed') ORDER BY id",
        (task_id,),
    ).fetchall()
    blocks = [r for r in rows if r["kind"] in _BLOCK_EVENT_KINDS]
    if not blocks:
        return inputs
    last_done = max((r["id"] for r in rows if r["kind"] == "completed"), default=0)
    inputs["prior_same_kind_blocks"] = sum(
        1 for r in blocks if r["id"] > last_done and _json_dict(r["payload"]).get("kind") == kind
    )
    if _same_reason(blocks[-1], reason):
        return inputs
    comments = [r for r in rows if r["kind"] == "commented" and r["id"] > blocks[-1]["id"]]
    if comments:
        inputs["replied_by"] = _reply_author(conn, task_id, comments)
    return inputs


def route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
    replied_by: Optional[str] = None, prior_same_kind_blocks: int = 0,
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

    A reply (``replied_by``, see :func:`block_loop_inputs`) since the previous
    block, with a reason that is not a verbatim repeat of that block's, makes a
    same-kind re-block a new question, not a loop: the count restarts
    at 1 and the payload names the author (``reset_by_reply``). An unblock alone
    never resets. ``prior_same_kind_blocks`` caps that: the
    ``BLOCK_REPLY_CEILING``-th same-kind block since the last completion routes
    to ``triage`` even when answered, so a commenting bot cannot loop forever.
    Pure: every input comes from the caller.
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    reset = replied_by is not None and prev_kind == kind
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    if reset and prior_same_kind_blocks + 1 >= BLOCK_REPLY_CEILING:
        recurrences = prior_same_kind_blocks + 1
        payload = {"reason": reason, "kind": kind, "recurrences": recurrences,
                   "source_status": source_status, "ceiling": BLOCK_REPLY_CEILING}
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    recurrences = 1 if reset or prev_kind != kind else prev_recurrences + 1
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if reset:
        payload["reset_by_reply"] = replied_by
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
    parking_id = _parking_event_id(conn, task_id)
    if parking_id is None:
        return None
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assignee = row["assignee"] if row else None
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented' AND id > ? "
        "ORDER BY id", (task_id, parking_id),
    ).fetchall()
    for event in events:
        author = _event_author(event["payload"])
        if _is_reply_author(author, assignee=assignee, release_authors=release_authors):
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
