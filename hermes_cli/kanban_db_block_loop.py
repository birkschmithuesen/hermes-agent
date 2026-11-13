"""Block-loop hold: a card the unblock-loop breaker sent to ``triage`` waits for a reply.

``kanban_db._route_block`` routes a card that re-blocks for the same kind
``BLOCK_RECURRENCE_LIMIT`` times to ``triage`` with a ``block_loop_detected``
event. ``triage`` is also where raw ideas wait for the auto-decomposer, which
rewrote such cards with the aux LLM and promoted them straight back into the
same unanswered question (t_72559b11: seven rewrites, zero human decisions).
This module tells the two apart and releases a held card unchanged once
someone other than the blocker has commented.

Events are ordered by ``task_events.id``, never ``created_at``: a comment and an
unblock written in the same second share a timestamp.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

HOLD_NONE = "none"
HOLD_AWAITING_REPLY = "awaiting_reply"
HOLD_REPLIED = "replied"

RELEASE_AUTHOR = "auto-decomposer"

# Any of these after the loop means the card already left (or was dragged through) triage,
# so its current triage stay has another cause. ``decomposed`` is the fan-out exit,
# ``status`` the dashboard drag.
_EXIT_KINDS = frozenset({"specified", "promoted", "decomposed", "status"})


def _norm(name: object) -> str:
    return str(name or "").strip().lower()


def _hold(conn: sqlite3.Connection, task_id: str) -> tuple[str, Optional[str]]:
    """``(state, latest_reply_author)``; the author is None unless the state is replied."""
    row = conn.execute("SELECT status, assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "triage":
        return HOLD_NONE, None
    loop_id = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected'",
        (task_id,),
    ).fetchone()[0]
    if loop_id is None:
        return HOLD_NONE, None
    later = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
        (task_id, loop_id),
    ).fetchall()
    if any(r["kind"] in _EXIT_KINDS for r in later):
        return HOLD_NONE, None
    # The blocker cannot answer its own question; anyone else who comments has taken a position.
    not_replies = {_norm(row["assignee"]), RELEASE_AUTHOR}
    reply_author: Optional[str] = None
    for r in later:
        if r["kind"] != "commented":
            continue
        author = (json.loads(r["payload"] or "null") or {}).get("author")
        if _norm(author) and _norm(author) not in not_replies:
            reply_author = str(author).strip()
    return (HOLD_REPLIED, reply_author) if reply_author else (HOLD_AWAITING_REPLY, None)


def block_loop_hold_state(conn: sqlite3.Connection, task_id: str) -> str:
    """``"none"``, ``"awaiting_reply"`` or ``"replied"`` for *task_id* (see module doc)."""
    return _hold(conn, task_id)[0]
