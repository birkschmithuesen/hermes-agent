"""Sovereign-session persistence guard for the Honcho memory provider.

A session latched *sovereign* by the ``sovereign_sessions`` PROFILE plugin is
pinned to a LOCAL model. Honcho stores conversation messages and its backend
pipeline makes cloud LLM calls over them, so a sovereign session's content MUST
NOT be persisted to Honcho until Honcho's own egress is verified local (that
verification is a separate task). This module lets the Honcho provider skip all
its write paths for such sessions.

Why a duplicated check and not an import: the core must stay independent of any
profile plugin, so the 5-line flag-file test from
``plugins/sovereign_sessions/state.py`` is copied here verbatim in spirit — same
env var (``HERMES_SOVEREIGN_FLAG_DIR``), same literal default path, same
sanitize regex. No ``sovereign_sessions`` import.

Cost / config flag: when the flag directory does not exist (the profile plugin
was never used) the check short-circuits to ``False`` after a single ``stat``,
so behaviour for non-sovereign deployments is byte-identical and effectively
free. The module depends only on the stdlib, so it imports without pulling in
the Honcho SDK (keeps the pure function unit-testable in isolation).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# Same safe set as plugins/sovereign_sessions/state.py: session ids come from
# untrusted platform routing (Telegram chat/thread ids, CLI uuids), so a crafted
# id must never let the flag path escape the flag dir. Everything outside this
# set collapses to "_".
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Literal default mirrors sovereign_sessions/state.py; overridable for tests and
# alternate profiles via HERMES_SOVEREIGN_FLAG_DIR.
_DEFAULT_FLAG_DIR = "/home/birk/.hermes/profiles/birk/var/sovereign-sessions"


def flag_dir() -> Path:
    """Resolve the sovereign flag directory (env override, else literal default)."""
    return Path(
        os.environ.get("HERMES_SOVEREIGN_FLAG_DIR") or _DEFAULT_FLAG_DIR
    ).expanduser()


def is_sovereign_session(session_id: Optional[str]) -> bool:
    """Return True iff *session_id* is latched sovereign → suppress persistence.

    Falsy ids are never sovereign. When the flag directory is absent the check
    returns immediately (feature never used), keeping the hot path at one stat.

    FAIL-CLOSED: if the stat itself errors (EACCES on the dir, an unreadable
    mount, …) we cannot prove the session is NOT sovereign, so return True and
    suppress — a lost memory write is recoverable, leaked sovereign content is
    not. ENOENT is not an error here: ``Path.exists()`` maps it to False.
    """
    if not session_id:
        return False
    try:
        directory = flag_dir()
        if not directory.exists():
            return False
        safe = _SAFE_RE.sub("_", session_id)
        return (directory / f"{safe}.json").exists()
    except Exception:
        return True
