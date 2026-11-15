# One Reply Predicate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The block-loop hold (BLH), the loop-parked release (LB release) and the reply reset (LB reset) answer "did the commissioner answer this card?" with ONE function in `hermes_cli/kanban_db_loop_breaker.py`. It compares names case-insensitively, honours `kanban.loop_breaker_release_authors`, and never counts CLI audit notes.

**Architecture:** `_is_reply_author` keeps the name checks (a)-(c) and folds names with `strip().casefold()`. A new public `first_reply_author(conn, task_id, comments, *, release_authors=None)` adds the audit-note check (d) and the lazy config read. It replaces `_reply_author`. `releasing_comment_author`, `block_loop_inputs` and `kanban_db_block_loop._hold` all call it. `_hold` and its two public readers get a `release_authors` keyword. The gateway passes the tick's tuple through `_release_one` → `_release_block_loop_hold` → `release_block_loop_hold`.

**Tech Stack:** Python 3, sqlite3, pytest via `scripts/run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-11-15-one-reply-predicate-design.md` (commit 93bcc5a210d2). D1-D6 are final.

## Global Constraints

- Base: `wt/t_3af61c7b` @ d1adb1bdc85a. Spec commit 93bcc5a210d2 sits on top. Work on branch `wt/t_52540fe8`. The branch stays LOCAL: never push. Probe outputs name private cards.
- D1: one predicate. A `commented` event is a reply iff (a) author ≠ assignee, (b) author ≠ `AUTO_DECOMPOSER_AUTHOR`, (c) with non-empty `release_authors` the author is in it, (d) the comment is not a CLI audit note (`_carries_reply_body`, `CLI_AUDIT_PREFIXES`, 1-second window).
- D2: names are compared as `str(x or "").strip().casefold()` on both sides. The stripped original spelling is reported, logged, and used in the `task_comments` SQL match.
- D3: the audit filter applies on all three paths.
- D4: `_hold(conn, task_id, *, release_authors=None)`. An explicit tuple is used as given. `None` reads `_configured_release_authors()`, but only after a candidate passed (a), (b), (d). An unreadable config means no reply, so the state stays `awaiting_reply`.
- D5: the FIRST qualifying reply after the anchor event is reported on all paths.
- D6: no change to `_EXIT_KINDS`, `is_loop_parked`, `list_triage_ids`, the reset rule, `force`.
- Spec acceptance 4: `grep -n "_norm\|not_replies" hermes_cli/kanban_db_block_loop.py` prints nothing. `grep -n "in release_authors" hermes_cli/kanban_db_loop_breaker.py` only hits inside `_is_reply_author`.
- Size: new functions CC ≤ 20, ≤ 300 lines. `tests/hermes_cli/test_kanban_block_kinds.py` is 600 lines (cap 2000).
- Language: English for code, comments, commits, docs.
- Tests: always `scripts/run_tests.sh`, never bare `pytest`. One command per shell call. No `cd`.

## Measured baselines (2026-11-15, HEAD 93bcc5a210d2)

- **TESTCMD** (create the TMPDIR first: `mkdir -p /home/birk/scratch/t_52540fe8/tmp`):

  ```
  env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/hermes_cli/test_kanban_block_classify.py tests/hermes_cli/test_kanban_decompose.py tests/gateway/test_kanban_auto_decompose_live.py tests/hermes_cli/test_kanban_specify.py tests/hermes_cli/test_kanban_specify_db.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/gateway/test_kanban_auto_decompose_block_loop.py
  ```

  Baseline: `=== Summary: 9 files, 91 tests passed, 0 failed` (block_kinds 37, auto_decompose_live 14, auto_decompose_block_loop 6, specify 12, decompose 11, specify_db 4, secret_scope 2, blocked_sticky 3, block_classify 2).
  After this plan: **103 passed, 0 failed** (12 new test ids: 9 in block_kinds, 1 in live, 2 in block_loop).

- **`/home/birk/.hermes/hermes-agent/venv/bin/python scripts/check`**: exactly **2 blocking, 0 advisory**, both pre-existing on d1adb1b:
  - `gateway/kanban_watchers_dispatcher.py:297 BLE001`
  - `tests/hermes_cli/test_kanban_db.py:1 FILE_LINES 2085 > 2084`

  (Architect correction after the plan run: at 93bcc5a the probe scripts were committed as `.py` and added 5 blocking + 7 advisory findings. Commit 7463f9567f8b renamed them to `*.py.txt`. Measured on the fully replayed plan: `code health: 25 files vs 6905478e5df0: 2 blocking, 0 advisory`.)

  This plan adds NONE. Keeping the BLE001 at `kanban_watchers_dispatcher.py:297` on line 297 is why Task 3 changes only existing lines above it and adds no lines.

## Single source of truth

`hermes_cli.kanban_db_loop_breaker.first_reply_author` owns the reply definition. Its parts are `_is_reply_author` (names, casefold, release authors) and `_carries_reply_body` (audit notes). Three call sites use it:
1. `block_loop_inputs` (reset path, `release_authors=None` → worker profile config);
2. `releasing_comment_author` (loop-parked release, explicit tick tuple);
3. `kanban_db_block_loop._hold` (BLH; explicit tick tuple from the gateway, `None` from `kanban_specify._held` / `_unheld_ids`).

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `hermes_cli/kanban_db_loop_breaker.py` | modify | `_fold`, casefolding `_is_reply_author`, `first_reply_author` (replaces `_reply_author`), release path through it, warning text |
| `hermes_cli/kanban_db_block_loop.py` | modify | `_hold` and readers on `first_reply_author`, `release_authors` keyword, `RELEASE_AUTHOR` alias; drop `_norm`, `not_replies`, `json` |
| `gateway/kanban_watchers_dispatcher.py` | modify | `_release_one` / `_release_block_loop_hold` take and pass the tick's `release_authors` |
| `hermes_cli/kanban_specify.py` | **no change** | `_held` / `_unheld_ids` call `block_loop_hold_state(conn, id)`; the default `release_authors=None` is exactly D4's CLI behaviour |
| `tests/hermes_cli/test_kanban_block_kinds.py` | append | predicate contracts on all three DB paths (9 test ids) |
| `tests/gateway/test_kanban_auto_decompose_live.py` | append | tick E2E: an audit note does not wake a loop-parked card (1) |
| `tests/gateway/test_kanban_auto_decompose_block_loop.py` | append | `_release_one` honours the tick's authors; the tick forwards them (2) |
| `website/docs/user-guide/features/kanban.md` | modify ~433-469 | one "what counts as a reply" rule, never-count audit notes, case-insensitive |

**Why no new test file:** TESTCMD is fixed by the card ("unchanged from the 2026-11-14 plan"). A new file would fall outside it, so the RED/GREEN proof would not cover it. The three files above already hold the fixtures the new tests need (`_block_loop_card`, `_loop_parked`, `_with_release_authors`, `_loop_park`, `_dispatcher`, `_snapshot`). `test_kanban_block_kinds.py` grows from 600 to about 690 lines, far below 2000.

## Existing assertions changed

**None.** I checked each existing test that the new rule touches:
- `test_kanban_block_kinds.py:104-109` (`" Worker "` is the assignee `worker`, `auto-decomposer` ignored): holds under casefold. This test is a mutant witness for M1.
- `test_hold_state_replied_after_foreign_comment`, `test_release_promotes_replied_card_unchanged` ("after reply by birk"): each has a single reply `birk`, so first == latest (D5) and no `FRAGE` is needed. With no config patch, `_hold` now reads the temp home's config. I probed it: `load_config_readonly()` in a fresh `HERMES_HOME` returns `kanban.loop_breaker_release_authors = None` → `()` → anyone.
- The gateway tick tests (`test_kanban_auto_decompose_block_loop.py`, `..._live.py`) use `birk`/`worker`/`factory-lead` with plain bodies. No audit-prefixed body, no mixed case.
- `test_no_candidate_reply_reads_no_config`: `first_reply_author` keeps the "config only after a candidate" order.
- No test asserts the log text `"kanban: reply reset skipped, ..."` (grep of `tests/`), so Task 1 may reword it.

---

### Task 1: One predicate in `kanban_db_loop_breaker` (casefold, audit filter on the release path)

**Files:**
- Modify: `hermes_cli/kanban_db_loop_breaker.py:34-115` (`_is_reply_author`, `_configured_release_authors`, `_reply_author` → `first_reply_author`), `:156-158` (`block_loop_inputs` call), `:249-271` (`releasing_comment_author`)
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (append), `tests/gateway/test_kanban_auto_decompose_live.py` (append)

**Interfaces:**
- Produces:
  - `_fold(name: object) -> str`: `str(name or "").strip().casefold()`
  - `_is_reply_author(author: str, *, assignee: Optional[str], release_authors: tuple[str, ...]) -> bool` (signature unchanged, now casefolds)
  - `first_reply_author(conn: sqlite3.Connection, task_id: str, comments: Iterable[sqlite3.Row], *, release_authors: Optional[tuple[str, ...]] = None) -> Optional[str]`. `comments` rows need the columns `payload` and `created_at`, in event-id order. It returns the stripped original author.
  - `releasing_comment_author(conn, task_id, *, release_authors: tuple[str, ...] = ()) -> Optional[str]` (signature unchanged)
- Removed: `_reply_author` (private, only caller `block_loop_inputs`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_block_kinds.py` (after line 600):

```python


# ---------------------------------------------------------------------------
# One reply predicate for the hold, the loop-parked release and the reset (t_52540fe8)
# ---------------------------------------------------------------------------

from hermes_cli.kanban_db_loop_breaker import releasing_comment_author


def test_reset_matches_release_authors_case_insensitively(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2: `--author Birk` is the commissioner `birk`; the reported name keeps its spelling."""
    _with_release_authors(monkeypatch, "birk")
    with kbc.connect_closing() as conn:
        tid = _running_task(conn, assignee="architekt")
        kb.block_task(conn, tid, reason="go on plan v1", kind="needs_input")
        assert _reblock_after(conn, tid, [("Birk", "amend step 3")])
        task = kb.get_task(conn, tid)
        assert (task.status, task.block_recurrences) == ("blocked", 1)
        assert _events(conn, tid, "blocked")[-1].payload["reset_by_reply"] == "Birk"


def test_loop_parked_release_matches_release_authors_case_insensitively(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        kb.add_comment(conn, tid, "Birk", "variant B")
        assert releasing_comment_author(conn, tid, release_authors=("birk",)) == "Birk"


def test_loop_parked_release_ignores_a_cli_audit_note(kanban_home: Path) -> None:
    """D3: `hermes kanban schedule --reason` writes its note as the operator profile, even when the
    op then fails; that annotation is not an answer and must not wake the card."""
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        kb.add_comment(conn, tid, "birk", "SCHEDULED: retry tomorrow")
        assert releasing_comment_author(conn, tid, release_authors=("birk",)) is None
        kb.add_comment(conn, tid, "birk", "variant B")
        assert releasing_comment_author(conn, tid, release_authors=("birk",)) == "birk"
```

Append to `tests/gateway/test_kanban_auto_decompose_live.py` (after line 195):

```python


def test_a_cli_audit_note_does_not_release_a_loop_parked_card(board):
    """t_52540fe8 D3: a `SCHEDULED:` note the CLI wrote as a release author is an annotation of an
    operation, not an answer, so the tick leaves the card parked."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="birk", body="SCHEDULED: retry tomorrow")
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5, ("birk",))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
    assert llm.call_count == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run:
`env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/gateway/test_kanban_auto_decompose_live.py -rf`

Expected: `4 failed`. The failures are:
- `test_reset_matches_release_authors_case_insensitively` (status `triage`);
- `test_loop_parked_release_matches_release_authors_case_insensitively` (None);
- `test_loop_parked_release_ignores_a_cli_audit_note` (`'birk'` is not None);
- `test_a_cli_audit_note_does_not_release_a_loop_parked_card` (status `ready`).

The other 51 pass.

- [ ] **Step 3: Implement**

In `hermes_cli/kanban_db_loop_breaker.py`, replace lines 41-50 (`_is_reply_author`) with:

```python
def _fold(name: object) -> str:
    """Comparison key of an author name: stripped and case-insensitive. Profile ids are
    lowercase-only, so folding never merges two agents; only human free text has case."""
    return str(name or "").strip().casefold()


def _is_reply_author(author: str, *, assignee: Optional[str], release_authors: tuple[str, ...]) -> bool:
    """The name checks of a reply (see :func:`first_reply_author`).

    Never the card's own worker (its assignee) nor the auto-decomposer; with
    ``release_authors`` set (``kanban.loop_breaker_release_authors``) only those names.
    Names compare via :func:`_fold`.
    """
    key = _fold(author)
    if not key or key in (_fold(AUTO_DECOMPOSER_AUTHOR), _fold(assignee)):
        return False
    return not release_authors or key in {_fold(a) for a in release_authors}
```

Replace `_configured_release_authors` (lines 82-94) with this. Only the docstring and the warning text change: the hold now fails closed through it too.

```python
def _configured_release_authors() -> Optional[tuple[str, ...]]:
    """``kanban.loop_breaker_release_authors`` of the calling profile; None when the
    config cannot be read (no reply then counts: fail closed)."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        cfg = load_config_readonly() or {}
    except (Exception, ConfigBackendUnavailable):  # health: allow BLE001 -- fail closed: any unreadable config means no reply, never a crash in route_block or a sweep
        logger.warning("kanban: no reply counted, loop-breaker config could not be read", exc_info=True)
        return None
    kcfg = cfg.get("kanban") if isinstance(cfg, dict) else None
    return parse_release_authors(kcfg.get("loop_breaker_release_authors") if isinstance(kcfg, dict) else None)
```

Replace `_reply_author` (lines 97-115) with:

```python
def first_reply_author(
    conn: sqlite3.Connection, task_id: str, comments: Iterable[sqlite3.Row], *,
    release_authors: Optional[tuple[str, ...]] = None,
) -> Optional[str]:
    """The one definition of a reply. Shared by the reply reset (:func:`block_loop_inputs`),
    the loop-parked release (:func:`releasing_comment_author`) and the block-loop hold
    (``kanban_db_block_loop._hold``).

    ``comments`` are ``commented`` event rows (``payload``, ``created_at``) after the anchor
    event, in id order. Returns the stripped author of the first one that passes
    :func:`_is_reply_author` and is not a CLI audit note (:func:`_carries_reply_body`), else
    None. ``release_authors=None`` reads the calling profile's config, and only once a
    candidate passed the other checks; an unreadable config means no reply."""
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assignee = row["assignee"] if row else None
    known = release_authors or ()
    candidates = [
        author for author, created_at in ((_event_author(e["payload"]), int(e["created_at"])) for e in comments)
        if _is_reply_author(author, assignee=assignee, release_authors=known)
        and _carries_reply_body(conn, task_id, author, created_at)
    ]
    if release_authors is not None or not candidates:
        return candidates[0] if candidates else None
    configured = _configured_release_authors()
    if configured is None:
        return None
    return next((a for a in candidates if _is_reply_author(a, assignee=assignee, release_authors=configured)), None)
```

In `block_loop_inputs`, change line 158 from `inputs["replied_by"] = _reply_author(conn, task_id, comments)` to:

```python
        inputs["replied_by"] = first_reply_author(conn, task_id, comments)
```

Replace `releasing_comment_author` (lines 249-271) with:

```python
def releasing_comment_author(
    conn: sqlite3.Connection, task_id: str, *, release_authors: tuple[str, ...] = (),
) -> Optional[str]:
    """Author of the first reply (:func:`first_reply_author`) newer than the parking event, else None.

    The card's own worker, the auto-decomposer and the CLI's own audit notes never count.
    With ``release_authors`` set (``kanban.loop_breaker_release_authors``) only those
    authors count, so an automation comment (a factory parking note) cannot wake it.
    """
    parking_id = _parking_event_id(conn, task_id)
    if parking_id is None:
        return None
    events = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'commented' AND id > ? "
        "ORDER BY id", (task_id, parking_id),
    ).fetchall()
    return first_reply_author(conn, task_id, events, release_authors=release_authors)
```

Also update the `_is_reply_author` sentence in the module docstring (lines 8-10) to: "... or a reply from the commissioner (:func:`first_reply_author`), which the tick honours by promoting the card as-is."

- [ ] **Step 4: Run them to verify they pass**

Run the Step 2 command.
Expected: `=== Summary: 2 files, 55 tests passed, 0 failed`.

Then check the grep:
`grep -n "in release_authors" hermes_cli/kanban_db_loop_breaker.py`
Expected: exactly one hit, the `return not release_authors or key in {_fold(a) for a in release_authors}` line inside `_is_reply_author`.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db_loop_breaker.py tests/hermes_cli/test_kanban_block_kinds.py tests/gateway/test_kanban_auto_decompose_live.py
git commit -m "fix(kanban): one case-insensitive reply predicate; audit notes never wake a parked card"
```

(Each commit message ends with the `Co-Authored-By` attribution line.)

---

### Task 2: BLH `_hold` on the shared predicate

**Files:**
- Modify: `hermes_cli/kanban_db_block_loop.py` (whole module body; lines 1-91)
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (append after Task 1's block)

**Interfaces:**
- Consumes: `first_reply_author(conn, task_id, comments, *, release_authors=None)`, `AUTO_DECOMPOSER_AUTHOR` (Task 1).
- Produces:
  - `_hold(conn: sqlite3.Connection, task_id: str, *, release_authors: Optional[tuple[str, ...]] = None) -> tuple[str, Optional[str]]`
  - `block_loop_hold_state(conn, task_id, *, release_authors: Optional[tuple[str, ...]] = None) -> str`
  - `release_block_loop_hold(conn, task_id, *, release_authors: Optional[tuple[str, ...]] = None) -> Optional[str]`
  - `RELEASE_AUTHOR = AUTO_DECOMPOSER_AUTHOR`

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_block_kinds.py`:

```python


def test_hold_ignores_a_comment_by_someone_outside_the_release_authors(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1: the hold honours `kanban.loop_breaker_release_authors` like the release path does."""
    _with_release_authors(monkeypatch, "birk")
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "ops", "B1.")
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"
        assert kbl.release_block_loop_hold(conn, tid) is None
        kb.add_comment(conn, tid, "birk", "B1.")
        assert kbl.block_loop_hold_state(conn, tid) == "replied"


@pytest.mark.parametrize("body", ["SCHEDULED: retry tomorrow", "BLOCKED: waiting on triage", "UNBLOCK: go ahead"])
def test_hold_ignores_a_cli_audit_note(kanban_home: Path, body: str) -> None:
    """D3: a park sweep's CLI note written as the commissioner is not an answer."""
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", body)
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_matches_release_authors_case_insensitively(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "Birk", "B1.")
        assert kbl.block_loop_hold_state(conn, tid, release_authors=("birk",)) == "replied"
        assert kbl.release_block_loop_hold(conn, tid, release_authors=("birk",)) == "Birk"


def test_hold_stays_awaiting_reply_when_the_config_is_unreadable(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4: with no explicit release authors the hold reads the config and fails closed;
    a sweep then skips the card and a human can still name it with `force`."""
    def boom():
        raise OSError("config unreadable")

    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", "B1.")
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"
        assert kbl.release_block_loop_hold(conn, tid) is None
        # An explicit tuple (the gateway tick's) is used as given and reads no config.
        assert kbl.block_loop_hold_state(conn, tid, release_authors=()) == "replied"
```

- [ ] **Step 2: Run them to verify they fail**

Run:
`env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py -rf`

Expected: `6 failed`, 40 passed. The failures are:
- `test_hold_ignores_a_comment_by_someone_outside_the_release_authors`;
- `test_hold_ignores_a_cli_audit_note[SCHEDULED: retry tomorrow]`, `[BLOCKED: waiting on triage]`, `[UNBLOCK: go ahead]`. Each says `'replied' == 'awaiting_reply'`;
- `test_hold_matches_release_authors_case_insensitively` (`TypeError: ... unexpected keyword argument 'release_authors'`);
- `test_hold_stays_awaiting_reply_when_the_config_is_unreadable`.

- [ ] **Step 3: Implement**

Replace `hermes_cli/kanban_db_block_loop.py` lines 1-91 with:

```python
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
```

The module-level import is cycle-free: `kanban_db_loop_breaker` imports only `logging`/`sqlite3`/`typing` at module level, and late-imports `kanban_db` inside functions. `json` goes because `_event_author` (through `kanban_db._json_dict`) now parses the payload.

- [ ] **Step 4: Run them to verify they pass**

Run the Step 2 command.
Expected: `=== Summary: 1 files, 46 tests passed, 0 failed`.

Then check the grep:
`grep -n "_norm\|not_replies" hermes_cli/kanban_db_block_loop.py`
Expected: no output (exit 1).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db_block_loop.py tests/hermes_cli/test_kanban_block_kinds.py
git commit -m "fix(kanban): the block-loop hold uses the shared reply predicate"
```

---

### Task 3: Gateway passes the tick's release authors to the hold release

**Files:**
- Modify: `gateway/kanban_watchers_dispatcher.py:282`, `:293`, `:296`, `:343-353`
- Test: `tests/gateway/test_kanban_auto_decompose_block_loop.py` (append after line 157)

**Interfaces:**
- Consumes: `release_block_loop_hold(conn, task_id, *, release_authors=None)` (Task 2).
- Produces:
  - `_KanbanDispatcher._release_one(slug: str, tid: str, release_authors: tuple[str, ...]) -> Optional[int]` (staticmethod)
  - `_release_block_loop_hold(tid: str, release_authors: tuple[str, ...]) -> Optional[str]` (module function)

- [ ] **Step 1: Write the failing tests**

Append to `tests/gateway/test_kanban_auto_decompose_block_loop.py`:

```python


def test_release_one_honours_the_ticks_release_authors(kanban_home):
    """t_52540fe8 D4: the hold release reads the same release authors as the loop-parked release."""
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "ops", "B1.")
    assert kwd._KanbanDispatcher._release_one("default", tid, ("birk",)) is None
    assert _snapshot(tid)[0].status == "triage"
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "Birk", "B1.")
    assert kwd._KanbanDispatcher._release_one("default", tid, ("birk",)) == 1
    assert _snapshot(tid)[0].status in {"todo", "ready"}


def test_tick_hands_its_release_authors_to_the_hold_release(kanban_home, call_llm, monkeypatch):
    seen = []

    def record(tid, release_authors):
        seen.append((tid, release_authors))
        return "birk"

    monkeypatch.setattr(kwd, "_release_block_loop_hold", record)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="raw idea", triage=True, assignee="worker")
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings).auto_decompose_tick(5, ("birk",))
    assert seen == [(tid, ("birk",))]
    assert call_llm.call_count == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run:
`env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_block_loop.py -rf`

Expected: `2 failed`, 6 passed. The failures are:
- `test_release_one_honours_the_ticks_release_authors` (`TypeError: _release_one() takes 2 positional arguments but 3 were given`);
- `test_tick_hands_its_release_authors_to_the_hold_release`. Its `record` gets one argument, `_release_one` swallows the TypeError, and `seen == []`.

- [ ] **Step 3: Implement**

Edit `gateway/kanban_watchers_dispatcher.py`. Every edit replaces one line with one line, so the pre-existing `BLE001` stays at line 297:

Line 282:
```python
                        released = self._release_one(slug, tid, release_authors)
```

Line 293:
```python
    def _release_one(slug: str, tid: str, release_authors: tuple[str, ...]) -> Optional[int]:
```

Line 296:
```python
            released_by = _release_block_loop_hold(tid, release_authors)
```

Lines 343-353 (below the finding, so a line count change is harmless):
```python
def _release_block_loop_hold(tid: str, release_authors: tuple[str, ...]) -> Optional[str]:
    """Promote a replied block-loop card unchanged (no aux call); the reply's author, else None.

    Its body is already a spec and the answer sits in the comment thread every worker
    receives, so an LLM rewrite only re-asks the question (t_72559b11). The caller has
    pinned the board, so the connection names none. ``release_authors`` is the tick's
    tuple, the same one ``_release_loop_parked`` gets.
    """
    from hermes_cli import kanban_db_block_loop as kbl

    with _kbc().connect_closing() as conn:
        return kbl.release_block_loop_hold(conn, tid, release_authors=release_authors)
```

- [ ] **Step 4: Run them to verify they pass**

Run the Step 2 command.
Expected: `=== Summary: 1 files, 8 tests passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
git add gateway/kanban_watchers_dispatcher.py tests/gateway/test_kanban_auto_decompose_block_loop.py
git commit -m "fix(kanban): the gateway hold release reads the tick's release authors"
```

---

### Task 4: User docs: one reply rule

**Files:**
- Modify: `website/docs/user-guide/features/kanban.md:433-440`, `:444-453` (two sentences), `:462-467` (one bullet)

- [ ] **Step 1: Replace lines 433-440** (from "An answer resets the count." through the "set the key in every worker profile ..." sentence) with:

```markdown
**What counts as a reply.** The answer reset, the block-loop hold and the loop-parked release below share one
rule. A comment after the block (for the hold and the parked card: after the loop breaker's `block_loop_detected`
event) is a reply unless one of these holds:

- its author is the task's own worker (its assignee) or the `auto-decomposer`;
- `kanban.loop_breaker_release_authors` is set and its author is not in that list;
- it is a note the CLI writes itself around an operation (`BLOCKED:`, `SCHEDULED:`, `UNBLOCK:`,
  `CHANGES REQUESTED:`). These never count, on any path: `hermes kanban schedule --reason` leaves its note even
  when the operation then fails.

Names compare case-insensitively and ignore surrounding spaces, so `--author Birk` matches a release author
`birk`. If there are several replies, the first one is named.

An answer resets the count. If a reply arrives between a block and the next block of the same kind, and the new
block's reason is not a verbatim repeat of the previous one, the new block is treated as a new question:
it lands in `blocked` with `recurrences: 1` and its event names the author
(`reset_by_reply`). A worker that re-asks word for word after an answer is still looping. An unblock alone never
resets.
Each path reads `kanban.loop_breaker_release_authors` from a different profile. The reset reads the profile that blocks
the card (the worker's profile: a CLI worker, a gateway-hosted worker or the kanban tool's process). `decompose --all` /
`specify --all` read the profile that runs them to decide whether a card is still held. The dispatcher tick reads the
gateway's profile for both releases. Set the key in every profile where the narrowing should apply.
```

- [ ] **Step 2: In the "held" paragraph (old lines 444-453)**, make two edits:
  - Replace "until someone other than its assignee comments on it." with "until it gets a reply (see *What counts as a reply* above)."
  - Replace "A comment counts as a reply only if its author differs from the card's assignee:" with "A comment by the card's assignee never counts:"

  Leave the rest of the paragraph as it is.

- [ ] **Step 3: In the parked-card list (old lines 462-467)**, replace the second bullet with:

```markdown
- a reply (see *What counts as a reply* above): the next dispatcher tick promotes it **as-is**, without rewriting
  its title or body. Set `kanban.loop_breaker_release_authors` to restrict whose comments count, so an
  automation's note cannot wake it. With the key unset, any comment from someone other than the assignee
  (except the CLI's own notes) releases the card, including an orchestrator or creator session woken by the
  `block_loop_detected` notification; deployments where automation comments on parked cards should set the key
  to the human commissioners. This path needs `kanban.auto_decompose: true`;
```

- [ ] **Step 4: Verify**

Run: `grep -n "never count\|case-insensitively\|What counts as a reply" website/docs/user-guide/features/kanban.md`
Expected: 5 hits in the reply section (old lines ~433-470), plus the existing, unrelated hit `never counted` in the `respawn_guarded` table row (~line 1520):
- the bold heading;
- "These never count, on any path";
- "Names compare case-insensitively";
- "(see *What counts as a reply* above)" twice.

Run: `grep -n "do not count" website/docs/user-guide/features/kanban.md`
Expected: no hit from the old line-438/439 sentence. No config key or slash command changed, so `tests/ci/test_docs_drift.py` is unaffected.

- [ ] **Step 5: Commit**

```bash
git add website/docs/user-guide/features/kanban.md
git commit -m "docs(kanban): one reply rule for reset, hold and release; CLI notes never count"
```

---

### Task 5: Verification: GREEN suite, check, RED proof on the base, mutants

No production code changes. Only scratch worktrees under `/home/birk/scratch/t_52540fe8/`. Never switch, reset or stash the card worktree.

- [ ] **Step 1: GREEN, full TESTCMD**

Run TESTCMD (see *Measured baselines*).
Expected: `=== Summary: 9 files, 103 tests passed, 0 failed`.

- [ ] **Step 2: Lint**

Run: `/home/birk/.hermes/hermes-agent/venv/bin/python scripts/check`
Expected: the same findings as the baseline, and nothing else:
- `gateway/kanban_watchers_dispatcher.py:297 BLE001`, still on line 297;
- `tests/hermes_cli/test_kanban_db.py FILE_LINES`.

No finding may name `kanban_db_loop_breaker.py`, `kanban_db_block_loop.py` or the three test files. The summary line reads `2 blocking, 0 advisory`.

- [ ] **Step 3: Acceptance greps (spec item 4)**

Run: `grep -n "_norm\|not_replies" hermes_cli/kanban_db_block_loop.py`. Expected: no output.

Run: `grep -n "in release_authors" hermes_cli/kanban_db_loop_breaker.py`. Expected: one hit, inside `_is_reply_author`.

- [ ] **Step 4: RED proof on the unmodified base d1adb1bdc85a**

Create the scratch worktree:
`git worktree add --detach /home/birk/scratch/t_52540fe8/red d1adb1bdc85a`

Copy the three changed test files over the base copies (one command each):
- `cp tests/hermes_cli/test_kanban_block_kinds.py /home/birk/scratch/t_52540fe8/red/tests/hermes_cli/test_kanban_block_kinds.py`
- `cp tests/gateway/test_kanban_auto_decompose_live.py /home/birk/scratch/t_52540fe8/red/tests/gateway/test_kanban_auto_decompose_live.py`
- `cp tests/gateway/test_kanban_auto_decompose_block_loop.py /home/birk/scratch/t_52540fe8/red/tests/gateway/test_kanban_auto_decompose_block_loop.py`

Run the tests there. `run_tests.sh` changes into its own repo root, so the relative test paths resolve inside the scratch tree:
`env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp /home/birk/scratch/t_52540fe8/red/scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_block_loop.py -rf`

Expected: `3 files, 57 tests passed, 12 failed`. Exactly these tests fail:
- `tests/hermes_cli/test_kanban_block_kinds.py`:
  - `test_reset_matches_release_authors_case_insensitively`
  - `test_loop_parked_release_matches_release_authors_case_insensitively`
  - `test_loop_parked_release_ignores_a_cli_audit_note`
  - `test_hold_ignores_a_comment_by_someone_outside_the_release_authors`
  - `test_hold_ignores_a_cli_audit_note[SCHEDULED: retry tomorrow]`
  - `test_hold_ignores_a_cli_audit_note[BLOCKED: waiting on triage]`
  - `test_hold_ignores_a_cli_audit_note[UNBLOCK: go ahead]`
  - `test_hold_matches_release_authors_case_insensitively`
  - `test_hold_stays_awaiting_reply_when_the_config_is_unreadable`
- `tests/gateway/test_kanban_auto_decompose_live.py`:
  - `test_a_cli_audit_note_does_not_release_a_loop_parked_card`
- `tests/gateway/test_kanban_auto_decompose_block_loop.py`:
  - `test_release_one_honours_the_ticks_release_authors`
  - `test_tick_hands_its_release_authors_to_the_hold_release`

That makes 10 named functions and 12 test ids, because the audit-note test runs 3 parameters. If any OTHER test fails on the base, the copied file depends on something outside this plan. Stop and report it.

Clean up: `git worktree remove --force /home/birk/scratch/t_52540fe8/red`

- [ ] **Step 5: Mutants (spec acceptance 3)**

Get the implementation head with `git rev-parse HEAD` and write it into the commands below as `<IMPL>` literally (no shell substitution). For each mutant, run these steps:
1. `git worktree add --detach /home/birk/scratch/t_52540fe8/mut<N> <IMPL>`
2. Apply the edit with the Edit tool on the scratch copy.
3. Run:
   `env -u HERMES_HOME -u HERMES_PROFILE -u HERMES_KANBAN_BOARD -u HERMES_KANBAN_TASK HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_52540fe8/tmp /home/birk/scratch/t_52540fe8/mut<N>/scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_block_loop.py -rf`
4. `git worktree remove --force /home/birk/scratch/t_52540fe8/mut<N>`

**M1: drop the casefold.** In `hermes_cli/kanban_db_loop_breaker.py` `_fold`, `return str(name or "").strip().casefold()` → `return str(name or "").strip()`.
Must turn red (5):
- `test_reset_matches_release_authors_case_insensitively`
- `test_loop_parked_release_matches_release_authors_case_insensitively`
- `test_hold_matches_release_authors_case_insensitively`
- `test_hold_state_ignores_assignee_and_auto_decomposer_comments` (existing, `" Worker "` vs `worker`)
- `test_release_one_honours_the_ticks_release_authors` (the `Birk` arm)

**M2: drop the audit filter on the release path.** In `releasing_comment_author`, replace `return first_reply_author(conn, task_id, events, release_authors=release_authors)` with:
```python
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    for event in events:
        author = _event_author(event["payload"])
        if _is_reply_author(author, assignee=row["assignee"], release_authors=release_authors):
            return author
    return None
```
Must turn red (2):
- `test_loop_parked_release_ignores_a_cli_audit_note`
- `test_a_cli_audit_note_does_not_release_a_loop_parked_card`

**M3: make `_hold` ignore release authors.** In `hermes_cli/kanban_db_block_loop.py` `_hold`, `first_reply_author(conn, task_id, comments, release_authors=release_authors)` → `first_reply_author(conn, task_id, comments, release_authors=())`.
Must turn red (3):
- `test_hold_ignores_a_comment_by_someone_outside_the_release_authors`
- `test_hold_stays_awaiting_reply_when_the_config_is_unreadable`
- `test_release_one_honours_the_ticks_release_authors` (the `ops` arm releases)

A mutant that leaves any listed test green, or leaves all tests green, means a test is too weak. Fix the test, re-run Step 4 and this step, and record it.

- [ ] **Step 6: Record and commit the evidence**

Append a `## Results` section to this plan with:
- the GREEN summary line;
- the `scripts/check` summary line;
- the RED summary line and its 12 failed ids;
- each mutant's failed ids.

Then commit:
```bash
git add -f docs/superpowers/plans/2026-11-15-one-reply-predicate.md
git commit -m "docs(kanban): record RED/GREEN and mutant evidence for the one reply predicate"
```

No push. No merge.

---

## Notes outside the scope (D6, not changed here)

- `kanban.md` old line 449 says that after a reply, `decompose --all` / `specify --all` "list the card again". `list_triage_ids` also drops loop-parked cards (t_30c69e47 D2), and `test_decompose_list_triage_ids_skips_held_card_even_after_reply` asserts that it does. That sentence was already inaccurate on the base. It is left alone because D6 forbids touching the composed listing, and its docs are a separate concern.
- In the gateway tick, `_release_one` only sees cards that `list_triage_ids` kept, so never loop-parked ones. Every card BLH parks is also loop-parked, so the tick releases replied cards through `_release_loop_parked` first. Task 3 still makes `_release_one` read the tick's tuple, so the two paths cannot disagree when a card is held but not parked (for example, after a board import with `parked`).

## Self-review

- Spec coverage:
  - D1 → Tasks 1+2.
  - D2 → `_fold`, plus the M1 tests.
  - D3 → Task 1 release + Task 2 hold, plus the M2 tests.
  - D4 → Task 2 (`None`/tuple/fail closed) + Task 3 (tick tuple).
  - D5 → `first_reply_author` returns the first. No existing test asserts the latest of several replies, so there is no `FRAGE`.
  - D6 → no change to `_EXIT_KINDS`, `is_loop_parked`, listing, `route_block`, `force`.
  - Acceptance 1 → Task 5.1. Acceptance 2 → Task 5.4. Acceptance 3 → Task 5.5. Acceptance 4 → Task 5.3. Acceptance 5 → Task 5.2. All six spec test bullets are covered.
- Names are the same in every task: `first_reply_author`, `_fold`, `_is_reply_author`, `release_authors`, `_release_one(slug, tid, release_authors)`, `_release_block_loop_hold(tid, release_authors)`.

## Architect replay (2026-11-15, before the build chain)

The architect applied every code block of Tasks 1-3 by fence order to a detached copy of 0fa595f84649 (`/home/birk/scratch/t_52540fe8/replay`). Results:
- Test blocks only, on the unmodified code: `=== Summary: 3 files, 57 tests passed, 12 failed`. The 12 failed ids are exactly the 12 listed in Task 5 Step 4.
- All blocks applied, full TESTCMD: `=== Summary: 9 files, 103 tests passed, 0 failed`.
- M1 (drop the casefold): `64 passed, 5 failed`. These are the 5 tests named for M1, including the existing `test_hold_state_ignores_assignee_and_auto_decomposer_comments`.
- M2 (drop the release-path audit filter): `67 passed, 2 failed`. These are the 2 named tests.
- M3 (`_hold` ignores release authors): `66 passed, 3 failed`. These are the 3 named tests.
- `scripts/check` (probes renamed to `.py.txt`): `2 blocking, 0 advisory`, both pre-existing.
- Acceptance greps: `_norm|not_replies` has no hit. `in release_authors` has exactly 1 hit, at `kanban_db_loop_breaker.py:57`, inside `_is_reply_author`.

Task 4 (docs) was not replayed. Its proof is the grep in Task 4 Step 4.
