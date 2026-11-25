"""Block-loop hold: a card the unblock-loop breaker sent to ``triage`` waits for a reply.

``kanban_db._route_block`` routes a card that re-blocks for the same kind
``BLOCK_RECURRENCE_LIMIT`` times to ``triage`` with a ``block_loop_detected``
event. ``triage`` is also where raw ideas wait for the auto-decomposer, which
rewrote such cards with the aux LLM and promoted them straight back into the
same unanswered question (t_72559b11: seven rewrites, zero human decisions).
This module tells the two apart and releases a held card unchanged once it got
a reply (``kanban_db_loop_breaker.first_reply_author``, the one definition).

Events are ordered by ``task_events.id``, never ``created_at``: a comment and an
unblock written in the same second share a timestamp.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli.kanban_db_loop_breaker import AUTO_DECOMPOSER_AUTHOR, first_reply_author

HOLD_NONE = "none"
HOLD_AWAITING_REPLY = "awaiting_reply"
HOLD_REPLIED = "replied"

# The gateway tick writes the release audit; its own comments never count as a reply.
RELEASE_AUTHOR = AUTO_DECOMPOSER_AUTHOR

# Any of these after the loop means the card already left (or was dragged through) triage,
# so its current triage stay has another cause. ``decomposed`` is the fan-out exit,
# ``status`` the dashboard drag.
_EXIT_KINDS = frozenset({"specified", "promoted", "decomposed", "status"})


def _hold(
    conn: sqlite3.Connection, task_id: str, *, release_authors: Optional[tuple[str, ...]] = None,
) -> tuple[str, Optional[str]]:
    """``(state, first_reply_author)``; the author is None unless the state is replied.

    ``release_authors=None`` reads the calling profile's ``kanban.loop_breaker_release_authors``
    (CLI sweeps); the gateway passes its tick's tuple. Unreadable config: still awaiting a reply.
    """
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "triage":
        return HOLD_NONE, None
    loop_id = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected'",
        (task_id,),
    ).fetchone()[0]
    if loop_id is None:
        return HOLD_NONE, None
    later = conn.execute(
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
        (task_id, loop_id),
    ).fetchall()
    if any(r["kind"] in _EXIT_KINDS for r in later):
        return HOLD_NONE, None
    comments = [r for r in later if r["kind"] == "commented"]
    reply_author = first_reply_author(conn, task_id, comments, release_authors=release_authors)
    return (HOLD_REPLIED, reply_author) if reply_author else (HOLD_AWAITING_REPLY, None)


def block_loop_hold_state(
    conn: sqlite3.Connection, task_id: str, *, release_authors: Optional[tuple[str, ...]] = None,
) -> str:
    """``"none"``, ``"awaiting_reply"`` or ``"replied"`` for *task_id* (see module doc)."""
    return _hold(conn, task_id, release_authors=release_authors)[0]


def release_block_loop_hold(
    conn: sqlite3.Connection, task_id: str, *, release_authors: Optional[tuple[str, ...]] = None,
) -> Optional[str]:
    """Promote a replied block-loop card ``triage -> todo`` as is; the reply's author, else None.

    No LLM and no rewrite: the body is already a spec and the answer lives in the comment
    thread every worker receives. If the reply did not settle it, the next identical block
    lands back in triage (``block_recurrences`` survives) and is held again.
    """
    from hermes_cli import kanban_db as kb

    state, reply_author = _hold(conn, task_id, release_authors=release_authors)
    if state != HOLD_REPLIED:
        return None
    if not kb.specify_triage_task(conn, task_id, author=RELEASE_AUTHOR):
        return None
    kb.add_comment(
        conn, task_id, RELEASE_AUTHOR,
        f"Released from block-loop hold after reply by {reply_author}; "
        "title and body unchanged, promoted to todo.",
    )
    return reply_author
