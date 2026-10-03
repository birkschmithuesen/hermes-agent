"""ONE operator alert per auth outage episode per assignee profile.

A logged-out credential makes every worker of one profile exit ``KANBAN_AUTH_FAILED_EXIT_CODE``
(77); the dispatcher leaves each card ``ready`` and retries on the auth cooldown. Without this
module the outage is silent — the night of 24./25.09.2026 produced 24 runs of one card in four
hours and no message to the operator. Sending per card per respawn would have been ~24 messages,
which is the same failure with the opposite sign.

Exactly one message per EPISODE per profile. The claim is an ``INSERT OR IGNORE`` into
``kanban_auth_alerts`` (one row per profile), so it is atomic against a second board tick and
survives a dispatcher or gateway restart. The episode ends when any run of that profile ends with
an outcome other than ``auth_failed``; that is detected lazily at claim time, so no write hook is
needed on every completion path.

The target comes from config (``kanban.auth_alert.target``, e.g. ``telegram:<chat>:<thread>``);
unset means no send, so behaviour is unchanged for anyone who has not opted in. Nothing here may
raise into a dispatcher tick.

Delivery is at-most-once per episode: the slot is claimed before the send, so a crash between
claim and send loses that episode's alert. The send runs inline in the dispatcher tick — it blocks
the tick for the transport's timeout, but holds no DB transaction while it waits.
"""

from __future__ import annotations

import getpass
import json
import logging
import sqlite3
import time
from typing import Callable, Optional

import hermes_cli.kanban_db as _kb

logger = logging.getLogger(__name__)

_TARGET_KEY_PATH = ("kanban", "auth_alert", "target")


def configured_alert_target() -> str:
    """``kanban.auth_alert.target`` or ``""`` when unset/unreadable.

    Read per call, never cached: an operator sets the key without restarting the dispatcher. Any
    config fault yields "" — a broken config must not crash a tick or a board. Read from the
    DISPATCHER's own profile (its ``HERMES_HOME``), never the logged-out assignee's — this is the
    operator's alert channel, not the failing profile's.
    """
    try:
        from hermes_cli.config import load_config_readonly
        node: object = load_config_readonly() or {}
        for key in _TARGET_KEY_PATH:
            node = node.get(key, "") if isinstance(node, dict) else ""
        return node.strip() if isinstance(node, str) else ""
    except Exception:
        logger.debug("kanban auth alert: target unreadable", exc_info=True)
        return ""


def format_alert(profile: str, waiting: int, cooldown_seconds: int) -> str:
    """The German plain-text alert.

    Contains the profile name, the classified provider error, the number of waiting cards and the
    fix command — deliberately NO credential, token, file content or provider body. Exit 77 covers
    every provider's auth refusal, so the remedy is provider-neutral with Claude as the example.
    """
    minutes = max(1, int(cooldown_seconds) // 60)
    try:
        username = getpass.getuser()
        user = f"Benutzer {username}" if username else "Hermes-Benutzer"
    except Exception:
        # getpass.getuser() raises OSError/KeyError when USER/LOGNAME are unset and the uid has
        # no passwd entry — routine in containers/systemd running under an arbitrary uid.
        user = "Hermes-Benutzer"
    return (
        f"🔐 Kanban: Anmeldung fehlt — Profil {profile}\n"
        f"Provider-Fehler: Anmeldung abgelehnt (auth, Worker-Exit 77). Die Zugangsdaten werden "
        f"abgelehnt; ein erneuter Versuch heilt das nicht.\n"
        f"Wartende Karten: {waiting} — sie bleiben in ready und werden alle {minutes} min "
        f"erneut versucht.\n"
        f"Fix: Provider-Anmeldung bzw. API-Key des Profils erneuern — für anthropic_plan/Claude "
        f"auf dem Host als {user}: claude /login"
    )


def _claim_alert_slot(conn: sqlite3.Connection, profile: str) -> bool:
    """True when THIS call owns the alert for ``profile``'s current auth episode.

    A row whose ``sent_at`` predates a non-``auth_failed`` ended run of the same profile belongs
    to a finished episode: it is deleted and the new outage alerts again. A recovery run that
    ended in the same second as ``sent_at`` does not count — fail closed, never double-send.
    """
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT sent_at FROM kanban_auth_alerts WHERE profile = ?", (profile,),
        ).fetchone()
        if row is not None:
            recovered = conn.execute(
                "SELECT 1 FROM task_runs WHERE profile = ? AND ended_at IS NOT NULL "
                "AND ended_at > ? AND COALESCE(outcome, '') != 'auth_failed' LIMIT 1",
                (profile, int(row["sent_at"] or 0)),
            ).fetchone()
            if recovered is None:
                return False  # same episode — already announced
            conn.execute("DELETE FROM kanban_auth_alerts WHERE profile = ?", (profile,))
        conn.execute(
            "INSERT OR IGNORE INTO kanban_auth_alerts (profile, sent_at) VALUES (?, ?)",
            (profile, int(time.time())),
        )
        return True


def _release_alert_slot(conn: sqlite3.Connection, profile: str) -> None:
    """Undo a claim whose message never went out, so a later tick tries again."""
    try:
        with _kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_auth_alerts WHERE profile = ?", (profile,))
    except Exception:
        logger.debug("kanban auth alert: could not release the slot for %s", profile, exc_info=True)


def _waiting_card_count(conn: sqlite3.Connection, profile: str) -> int:
    """Cards of ``profile`` currently parked on this outage (latest ended run ``auth_failed``)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tasks t WHERE t.assignee = ? AND t.status IN ('ready', 'review') "
        "AND (SELECT r.outcome FROM task_runs r WHERE r.task_id = t.id "
        "     AND r.ended_at IS NOT NULL ORDER BY r.ended_at DESC, r.id DESC LIMIT 1) "
        "    = 'auth_failed'",
        (profile,),
    ).fetchone()
    return int(row[0]) if row else 0


def _default_sender(target: str, message: str) -> bool:
    """Deliver through the send_message tool (platform config and token resolution live there)."""
    from tools.send_message_tool import send_message_tool

    raw = send_message_tool({"action": "send", "target": target, "message": message})
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
    except (TypeError, ValueError):
        logger.warning("kanban auth alert: unparseable send result")
        return False
    if isinstance(payload, dict) and payload.get("error"):
        logger.warning("kanban auth alert: send failed: %s", payload.get("error"))
        return False
    return True


# Swapped out in tests (no network there); production always uses _default_sender.
_sender: Callable[[str, str], bool] = _default_sender


def maybe_alert_auth_failure(conn: sqlite3.Connection, *, profile: Optional[str]) -> bool:
    """Send THE alert for ``profile``'s auth outage; True when a message went out.

    Best-effort by contract: no profile, no configured target, an already-announced episode, a DB
    error or a transport error all return False. A dispatcher tick must never die because an alert
    could not be delivered.

    Everything AFTER a successful claim (building the message, sending it) runs under one
    try/except that releases the claimed slot on any failure — including exceptions raised
    while building the message itself (waiting-card count, cooldown lookup, ``format_alert``).
    Otherwise an error there would burn the episode: the claim row stays, and no later tick ever
    retries the alert.
    """
    try:
        if not profile:
            return False
        target = configured_alert_target()
        if not target:
            return False
        if not _claim_alert_slot(conn, profile):
            return False
    except Exception:
        logger.exception("kanban auth alert: failed to claim the slot for profile %s", profile)
        return False

    try:
        message = format_alert(
            profile, _waiting_card_count(conn, profile), _kb._resolve_auth_failed_cooldown_seconds(),
        )
        delivered = _sender(target, message)
    except Exception:
        logger.exception("kanban auth alert: failed to build/send the alert for profile %s", profile)
        delivered = False

    if delivered:
        logger.info("kanban auth alert sent for profile %s", profile)
        return True
    _release_alert_slot(conn, profile)
    return False
