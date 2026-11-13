# Auto-decomposer must not un-park loop-breaker triage cards: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card that the unblock-loop breaker moved to `triage` (`block_loop_detected`) is never auto-specified or
auto-decomposed. It leaves triage only through a comment from someone other than its own worker (promoted as-is) or
an explicit human unblock/specify/decompose.

**Architecture:** One new DB sibling `hermes_cli/kanban_db_loop_breaker.py` owns the loop breaker: `route_block`
(moved out of `kanban_db.py`), the event-log marker `is_loop_parked`, and the comment-release predicate and action.
The triage listings used by automation filter the marker out. The gateway tick releases commented cards first, with no LLM call.
`unblock_task` accepts a loop-parked triage card.

**Tech Stack:** Python 3, sqlite3 (kanban.db `tasks` / `task_events`), pytest via `scripts/run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-11-13-auto-decompose-loop-breaker-design.md`.
**Evidence:** `docs/superpowers/specs/2026-11-13-auto-decompose-loop-breaker-measurements.md`.
**Behaviour source of truth:** decisions D1-D5 (below). Every test names the D-number it pins.

## Global Constraints

- Base: `origin/main` 547739e4, branch `wt/t_95a7e463`, worktree `/mnt/HC_Volume_106183673/hermes/hermes-agent/.worktrees/t_95a7e463`. Never touch `~/.hermes/hermes-agent`. No merge, no push.
- Bash rules for this run (cc-run.py): one command per Bash call; no `cd`, `&&`, `;`, `$VAR`, loops or `$( )`; paths inside the worktree only; long commands with timeout 900000 ms, never in the background; commit after every finished task.
- Test runner prefix for every verification (run `mkdir -p /home/birk/scratch/t_95a7e463/tmp` once first):
  `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh <files>`
  (never bare `pytest`; never the full suite, which cc-run.py runs once afterwards).
- `hermes_cli/kanban_db.py` is 4231 lines. The size ratchet (`scripts/code_health/config.py`: `FILE_LINES` 2000, over-limit
  units may only shrink) forbids net growth. New logic goes in the sibling, and the `route_block` move offsets the
  `unblock_task` growth. New functions: CC ≤ 20, ≤ 300 lines, nesting ≤ 6.
- Siblings late-import the facade inside functions (no module-level cycle). No re-export shim for `_route_block`.
- Do NOT edit any model tool schema (`kanban_unblock` description etc.): that changes `tests/fixtures/prompt_surface/`
  and needs a core approval. Handler error strings are fine.
- No new `HERMES_*` env var. The new knob is the config key `kanban.loop_breaker_release_authors` with a docs row
  (`tests/ci/test_docs_drift.py`).
- No user content in INFO+ logs (task ids only).
- English for code, comments, commits, docs.

## Decisions (from the card brief: do not re-open)

- **D1** Marker: status `triage` and the newest *triage-entering* event is `block_loop_detected`. Triage-entering events:
  `created`; `block_loop_detected`; `status` with `payload.status == "triage"` (dashboard drag/PATCH); `imported` with
  `payload.parked == true` (`hermes_cli/kanban_transfer.py:271`). Other events of those kinds are skipped. No new
  column: the event log answers the question and can't go stale. A column would need clearing on every exit path
  (specify, decompose, drag, unblock, archive).
- **D2** The gateway tick and `hermes kanban decompose --all` / `specify --all` skip loop-parked cards. Single-id
  `specify <id>` / `decompose <id>`, the dashboard buttons and drag are unchanged (the human way out).
- **D3** Release by comment: a `commented` event newer than the parking event, author ∉ {task assignee,
  `auto-decomposer`}. The tick then calls `specify_triage_task(conn, id)` with no title/body/assignee/author: no LLM, no
  body rewrite (t_a096323e's binding body was LLM-rewritten 13 times). Rationale: upstream has no machine-readable
  commissioner field, and "not the card's own worker" is the generic, upstreamable form of "Auftraggeber". Narrowing:
  `kanban.loop_breaker_release_authors` (list or comma-separated string). When it's set, only those authors release; unset or empty
  means the generic rule. It's read in `_resolve_auto_decompose_settings`, live each tick. Evidence: the `factory-lead` parking note on
  t_a096323e (17:30) is an automation comment that must not wake the card.
- **D4** `unblock_task` accepts a loop-parked triage card and restores it like a blocked card (resume status from
  events, parent re-gate). A fresh triage card is refused. `block_recurrences` survives (existing invariant).
- **D5** `website/docs/user-guide/features/kanban.md` "Where an unblocked task lands" note says that the
  auto-decomposer never touches loop-breaker cards and how they leave triage.

## Facts verified on 547739e4 (Stand 2026-11-13)

| Fact | Location |
|---|---|
| `BLOCK_RECURRENCE_LIMIT = 2` | `hermes_cli/kanban_db.py:115` |
| `_route_block` (to move), loop -> `("triage", "block_loop_detected", ...)` | `hermes_cli/kanban_db.py:3215-3241` |
| its only caller | `hermes_cli/kanban_db.py:3183` (`block_task`) |
| `unblock_task`, guard `status IN ('blocked', 'scheduled')` | `hermes_cli/kanban_db.py:3557-3598` |
| `_resume_status_from_events` already reads `block_loop_detected.source_status` | `hermes_cli/kanban_db.py:2018-2033` |
| `specify_triage_task` (title/body None -> no change, no comment; `specified` event; `recompute_ready`) | `hermes_cli/kanban_db.py:3732-3786` |
| `add_comment` -> `commented` event `{"author": author, "len": n}` (author unstripped) | `hermes_cli/kanban_db.py:1671-1686` |
| `_json_dict`, `_row_get` helpers | `hermes_cli/kanban_db.py:72`, `:55` |
| worker comment author = its profile name (= assignee), `"worker"` only as a fallback | `tools/kanban_tools.py:139-150, 970` |
| `list_triage_ids` (decompose) | `hermes_cli/kanban_decompose.py:339-343` |
| `list_triage_ids` (specify) | `hermes_cli/kanban_specify.py:237-241` |
| `--all` sweep driver | `hermes_cli/kanban.py:1271-1308` |
| CLI unblock failure text | `hermes_cli/kanban.py:1032` |
| tool unblock failure text | `tools/kanban_tools.py:1270` |
| tick, author literal `"auto-decomposer"` | `gateway/kanban_watchers_dispatcher.py:246-297` |
| settings resolver | `gateway/kanban_watchers_common.py:66-87` |
| tick caller | `gateway/kanban_watchers.py:304-307` (loader: `hermes_cli.config.load_config`) |
| config defaults (`auto_decompose`, `auto_decompose_per_tick`) | `hermes_cli/config_defaults.py:1917-1922` |
| docs note / config table | `website/docs/user-guide/features/kanban.md:414-432`, `:826-836` |
| CLI reference rows | `website/docs/reference/cli-commands.md:849, 854, 855` |
| Baseline: the 5 target test files pass, 16 tests | run 2026-11-13 on 547739e4 |

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `hermes_cli/kanban_db_loop_breaker.py` | the unblock-loop breaker: routing, marker, comment release | **create** |
| `hermes_cli/kanban_db.py` | facade: `block_task` calls the sibling; `unblock_task` accepts loop-parked triage | modify (net shrink) |
| `hermes_cli/kanban_decompose.py` | `list_triage_ids` filters; `release_loop_parked` wrapper | modify |
| `hermes_cli/kanban_specify.py` | `list_triage_ids` filters | modify |
| `gateway/kanban_watchers_common.py` | settings resolver returns release authors | modify |
| `gateway/kanban_watchers_dispatcher.py` | tick releases first, then decomposes the filtered list | modify |
| `gateway/kanban_watchers.py` | pass release authors to the tick | modify |
| `hermes_cli/config_defaults.py` | `kanban.loop_breaker_release_authors: None` | modify |
| `hermes_cli/kanban.py`, `tools/kanban_tools.py` | unblock failure strings mention loop-parked | modify (strings only) |
| `website/docs/user-guide/features/kanban.md`, `website/docs/reference/cli-commands.md` | D5 docs | modify |
| `tests/gateway/test_kanban_auto_decompose_live.py` | tick E2E with real DB + stub aux (RED), D3, settings | extend |
| `tests/gateway/test_kanban_auto_decompose_secret_scope.py` | fakes gain `release_loop_parked` | extend |
| `tests/hermes_cli/test_kanban_block_kinds.py` | D1 marker, D4 unblock | extend |
| `tests/hermes_cli/test_kanban_decompose.py` | D2 sweep skip, explicit single id still works | extend |
| `tests/hermes_cli/test_kanban_specify_db.py` | D2 specify listing skip | extend |

**Testkommando (coder card, one line):**
`HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify_db.py`

---

### Task 1: RED characterization: the real tick re-promotes a loop-parked card

**Files:**
- Test: `tests/gateway/test_kanban_auto_decompose_live.py` (extend; keep `test_disabled_when_flag_false`)

**Interfaces:**
- Consumes: `kb.create_task`, `kb.claim_task`, `kb.block_task`, `kb.unblock_task`, `kb.BLOCK_RECURRENCE_LIMIT`,
  `kwd._KanbanDispatcher`, `kwd._DispatcherSettings`, `kwd._board_slugs` (module function, patched),
  `agent.auxiliary_client.call_llm` (patched).
- Produces (test helpers reused in Task 4 in the same file): fixture `board`, `_dispatcher()`, `_loop_park(conn, ...)`,
  `_llm(content)`.

- [ ] **Step 1: Write the characterization test and the green control**

Replace the module's imports block (lines 10-15) and append the new tests. The final top of the file reads:

```python
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from gateway.kanban_watchers_common import _resolve_auto_decompose_settings
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Real default board under a temp HERMES_HOME; the tick sees only that board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: [kb.DEFAULT_BOARD])
    return home


def _dispatcher():
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD=kb.DEFAULT_BOARD), settings)


def _llm(content: str):
    """Stub the aux LLM the decomposer calls; the mock records whether it was billed."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return patch("agent.auxiliary_client.call_llm", return_value=resp)


_REWRITE = json.dumps({"fanout": False, "rationale": "one unit", "title": "LLM title", "body": "LLM rewritten body"})


def _loop_park(conn, *, body: str = "binding body from the commissioner") -> str:
    """Drive a card into triage the way production does: block needs_input, unblock, re-block."""
    tid = kb.create_task(conn, title="needs the owner's answer", body=body, assignee="worker")
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid
```

Tests to append:

```python
def test_tick_leaves_a_loop_parked_card_in_triage(board):
    """D1/D2 (t_95a7e463): the unblock-loop breaker parked the card in triage; the auto-decompose tick
    must neither bill the LLM for it nor promote it (it re-spawned a worker into the same wall 13x)."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.body) == ("triage", "binding body from the commissioner")
    assert llm.call_count == 0


def test_tick_still_decomposes_a_fresh_triage_card(board):
    """Regression control (D2): a card created in triage is still auto-specified by the same tick."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough idea", assignee="worker", triage=True)
    with _llm(_REWRITE) as llm:
        assert _dispatcher().auto_decompose_tick(5) == 1
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.body) == ("ready", "LLM rewritten body")
    assert llm.call_count == 1
```

Also change the existing test's unpack only after Task 4. In Task 1 it still reads `enabled, _per_tick = ...`.

- [ ] **Step 2: Run, verify RED for the right reason**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_live.py -v`
Expected: `test_tick_leaves_a_loop_parked_card_in_triage` FAILS with `AssertionError` showing
`('ready', 'LLM rewritten body') == ('triage', 'binding body from the commissioner')`. That is the measured loop:
promoted and body rewritten. `test_tick_still_decomposes_a_fresh_triage_card` and `test_disabled_when_flag_false` PASS.
If the first test passes on base, the fixture is not reaching the board the tick reads (the control would fail too).
Fix the fixture before going on. ANNAHME: `scoped_current_board("default")` resolves to the DB `kb.init_db()` created; the
green control proves it.

- [ ] **Step 3: Commit the red test**

```bash
git add tests/gateway/test_kanban_auto_decompose_live.py
git commit -m "test(kanban): characterize auto-decompose re-promoting a loop-breaker triage card (red)"
```

---

### Task 2: Loop-breaker sibling: move `route_block`, add the D1 marker

**Files:**
- Create: `hermes_cli/kanban_db_loop_breaker.py`
- Modify: `hermes_cli/kanban_db.py:110` (comment), `:3129-3130` (docstring), `:3183` (call), delete `:3215-3242` (`_route_block` + its trailing blank lines)
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (extend)

**Interfaces:**
- Produces:
  - `route_block(kind: Optional[str], reason: Optional[str], source_status: str, *, prev_kind: Optional[str], prev_recurrences: int) -> tuple[str, str, str, tuple, dict]` (same contract as the old `_route_block`)
  - `AUTO_DECOMPOSER_AUTHOR: str = "auto-decomposer"`
  - `is_loop_parked(conn: sqlite3.Connection, task_id: str) -> bool`
  - `loop_parked_ids(conn: sqlite3.Connection, task_ids: Iterable[str]) -> set[str]`
  - private `_parking_event_id(conn, task_id) -> Optional[int]` (used by Task 4)

- [ ] **Step 1: Write the failing marker tests**

Append to `tests/hermes_cli/test_kanban_block_kinds.py` (it already has `kanban_home`, `_running_task`, `_make_running_again`):

```python
# ---------------------------------------------------------------------------
# Loop-parked marker (t_95a7e463, D1)
# ---------------------------------------------------------------------------


def _loop_parked(conn, title="t"):
    tid = _running_task(conn, title=title)
    kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_loop_breaker_triage_card_is_marked_parked(kanban_home: Path) -> None:
    from hermes_cli.kanban_db_loop_breaker import is_loop_parked, loop_parked_ids

    with kbc.connect_closing() as conn:
        parked = _loop_parked(conn)
        fresh = kb.create_task(conn, title="idea", triage=True)
        assert is_loop_parked(conn, parked) is True
        assert is_loop_parked(conn, fresh) is False
        assert loop_parked_ids(conn, [parked, fresh]) == {parked}


def test_marker_follows_the_newest_triage_entry(kanban_home: Path) -> None:
    """A human move into triage after the loop breaker makes it an ordinary triage card;
    leaving triage clears the marker; a new loop re-parks it."""
    from hermes_cli.kanban_db_loop_breaker import is_loop_parked

    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "todo"})
        assert is_loop_parked(conn, tid) is False  # not in triage
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "triage", "requested_status": "triage"})
        assert is_loop_parked(conn, tid) is False  # a human dragged it there
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still which variant?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        assert is_loop_parked(conn, tid) is True
```

- [ ] **Step 2: Run, verify they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py -v`
Expected: the two new tests FAIL with `ModuleNotFoundError: No module named 'hermes_cli.kanban_db_loop_breaker'`. The 4 existing ones PASS.

- [ ] **Step 3: Create the sibling**

`hermes_cli/kanban_db_loop_breaker.py`:

```python
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
```

- [ ] **Step 4: Switch the facade to the sibling and delete `_route_block`**

In `hermes_cli/kanban_db.py`:

- line 110: `# Typed block reasons (routing in ``_route_block``); ``None`` = legacy un-typed.` →
  `# Typed block reasons (routing in ``kanban_db_loop_breaker.route_block``); ``None`` = legacy un-typed.`
- `block_task` docstring lines 3129-3130: `(or ``todo`` / ``triage``, see\n    :func:`_route_block`)` →
  `(or ``todo`` / ``triage``, see\n    :func:`kanban_db_loop_breaker.route_block`)`
- at the call (line 3183):

```python
        from hermes_cli.kanban_db_loop_breaker import route_block

        new_status, event_kind, set_sql, params, payload = route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=int(_row_get(cur_row, "block_recurrences") or 0),
        )
```

- delete the whole `def _route_block(...)` block (lines 3215-3241) and one of the two blank lines that follow it, so
  `redact_review_value` keeps exactly two blank lines above it.

- [ ] **Step 5: Run, verify green (marker + unchanged loop routing)**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/gateway/test_kanban_notifier.py`
Expected: all PASS (6 in `test_kanban_block_kinds.py`). `wc -l hermes_cli/kanban_db.py` prints a number < 4231.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/kanban_db_loop_breaker.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_block_kinds.py
git commit -m "refactor(kanban): loop-breaker sibling owns route_block and the loop-parked marker"
```

---

### Task 3: D2: automation listings skip loop-parked cards

**Files:**
- Modify: `hermes_cli/kanban_decompose.py:339-343`, `hermes_cli/kanban_specify.py:237-241`
- Test: `tests/hermes_cli/test_kanban_decompose.py`, `tests/hermes_cli/test_kanban_specify_db.py` (extend)

**Interfaces:**
- Consumes: `kanban_db_loop_breaker.loop_parked_ids(conn, ids) -> set[str]` (Task 2).
- Produces: `kanban_decompose.list_triage_ids(*, tenant=None) -> list[str]` and
  `kanban_specify.list_triage_ids(*, tenant=None) -> list[str]`, same signatures, now without loop-parked ids.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_decompose.py`:

```python
def _loop_park(conn, body="binding body"):
    """block needs_input -> unblock -> re-block: the unblock-loop breaker parks it in triage."""
    tid = kb.create_task(conn, title="needs answer", body=body, assignee="worker")
    for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
        if attempt:
            assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_all_sweep_skips_loop_parked_triage_cards(kanban_home):
    """D2: `decompose --all` / the gateway tick list only cards that are not loop-parked."""
    with kbc.connect() as conn:
        parked = _loop_park(conn)
        fresh = kb.create_task(conn, title="idea", triage=True)
    assert decomp.list_triage_ids() == [fresh]
    assert parked not in decomp.list_triage_ids()


def test_explicit_single_id_still_decomposes_a_loop_parked_card(kanban_home):
    """D2: `hermes kanban decompose <id>` is the human way out and keeps working."""
    with kbc.connect() as conn:
        tid = _loop_park(conn)
    payload = jsonlib.dumps({"fanout": False, "rationale": "r", "title": "Clarified", "body": "new spec"})
    with _patch_aux_client(payload):
        outcome = decomp.decompose_task(tid, author="birk")
    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
```

Append to `tests/hermes_cli/test_kanban_specify_db.py`:

```python
def test_specify_all_listing_skips_loop_parked_cards(kanban_home):
    """D2: `hermes kanban specify --all` never sweeps a card the unblock-loop breaker parked."""
    from hermes_cli import kanban_specify as spec

    with kbc.connect() as conn:
        parked = kb.create_task(conn, title="needs answer", assignee="worker")
        for attempt in range(kb.BLOCK_RECURRENCE_LIMIT):
            if attempt:
                assert kb.unblock_task(conn, parked)
            assert kb.claim_task(conn, parked, claimer="worker") is not None
            assert kb.block_task(conn, parked, reason="?", kind="needs_input")
        fresh = _create_triage(conn, title="idea")
    assert spec.list_triage_ids() == [fresh]
```

- [ ] **Step 2: Run, verify they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify_db.py -v`
Expected: `test_all_sweep_skips_loop_parked_triage_cards` and `test_specify_all_listing_skips_loop_parked_cards` FAIL
(the parked id is in the list). `test_explicit_single_id_still_decomposes_a_loop_parked_card` PASSES (behaviour that must not change).

- [ ] **Step 3: Implement the filter**

`hermes_cli/kanban_decompose.py`, replace `list_triage_ids`:

```python
def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Triage ids automation may decompose: a card the unblock-loop breaker parked
    is skipped (promoting it respawns a worker into the same wall)."""
    from hermes_cli.kanban_db_loop_breaker import loop_parked_ids

    with kbc.connect_closing() as conn:
        ids = [row.id for row in kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)]
        parked = loop_parked_ids(conn, ids)
    return [tid for tid in ids if tid not in parked]
```

`hermes_cli/kanban_specify.py`, replace `list_triage_ids`:

```python
def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Task ids in the triage column for a ``--all`` sweep; ``tenant`` narrows it.
    Cards the unblock-loop breaker parked are skipped (see ``kanban_db_loop_breaker``)."""
    from hermes_cli.kanban_db_loop_breaker import loop_parked_ids

    with kbc.connect_closing() as conn:
        ids = [t.id for t in kb.list_tasks(conn, status="triage", tenant=tenant, include_archived=False)]
        parked = loop_parked_ids(conn, ids)
    return [tid for tid in ids if tid not in parked]
```

- [ ] **Step 4: Run, verify green, including Task 1's RED test**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify_db.py tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_secret_scope.py`
Expected: all PASS. `test_tick_leaves_a_loop_parked_card_in_triage` is now green.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_decompose.py hermes_cli/kanban_specify.py tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify_db.py
git commit -m "fix(kanban): auto-decompose and --all sweeps skip loop-breaker triage cards"
```

---

### Task 4: D3: release by a foreign comment, promoted as-is, plus the config knob

**Files:**
- Modify: `hermes_cli/kanban_db_loop_breaker.py` (append), `hermes_cli/kanban_decompose.py` (append wrapper),
  `gateway/kanban_watchers_common.py:66-87`, `gateway/kanban_watchers_dispatcher.py:246-297`,
  `gateway/kanban_watchers.py:304-307`, `hermes_cli/config_defaults.py:1922` (after `auto_decompose_per_tick`)
- Test: `tests/gateway/test_kanban_auto_decompose_live.py`, `tests/gateway/test_kanban_auto_decompose_secret_scope.py`

**Interfaces:**
- Consumes: `_parking_event_id`, `AUTO_DECOMPOSER_AUTHOR` (Task 2); `kb.list_tasks`, `kb.specify_triage_task`.
- Produces:
  - `kanban_db_loop_breaker.releasing_comment_author(conn, task_id: str, *, release_authors: tuple[str, ...] = ()) -> Optional[str]`
  - `kanban_db_loop_breaker.release_commented(conn, *, release_authors: tuple[str, ...] = ()) -> list[str]`
  - `kanban_decompose.release_loop_parked(*, release_authors: tuple[str, ...] = ()) -> list[str]`
  - `_resolve_auto_decompose_settings(load_config) -> tuple[bool, int, tuple[str, ...]]`
  - `_KanbanDispatcher.auto_decompose_tick(auto_decompose_per_tick: int, release_authors: tuple[str, ...] = ()) -> int`
  - config key `kanban.loop_breaker_release_authors` (default `None`)

- [ ] **Step 1: Write the failing tests**

In `tests/gateway/test_kanban_auto_decompose_live.py`, change the existing test's unpack to three values:

```python
def test_disabled_when_flag_false():
    enabled, _per_tick, _authors = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False
```

and append:

```python
def test_worker_comment_does_not_release_a_loop_parked_card(board):
    """D3: the card's own worker (assignee) commenting is not an answer from the commissioner."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="worker", body="still waiting for the variant")
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
    assert llm.call_count == 0


def test_foreign_comment_releases_the_card_as_is(board):
    """D3: a comment by someone else promotes the card on the next tick WITHOUT an LLM rewrite."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="birk", body="variant B")
    with _llm(_REWRITE) as llm:
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert (task.status, task.title, task.body) == (
        "ready", "needs the owner's answer", "binding body from the commissioner")
    assert llm.call_count == 0


def test_comment_before_the_loop_detection_does_not_release(board):
    """D3: only comments newer than the parking event count."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs the owner's answer", body="b", assignee="worker")
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="?", kind="needs_input")
        kb.add_comment(conn, tid, author="birk", body="first answer")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_release_authors_narrow_who_may_wake_the_card(board):
    """D3 narrowing: with kanban.loop_breaker_release_authors set, an automation comment does not release."""
    with kbc.connect_closing() as conn:
        tid = _loop_park(conn)
        kb.add_comment(conn, tid, author="factory-lead", body="parking note")
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5, ("birk",))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        kb.add_comment(conn, tid, author="birk", body="variant B")
    with _llm(_REWRITE):
        _dispatcher().auto_decompose_tick(5, ("birk",))
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize(("raw", "expected"), [
    (None, ()), ([], ()), ("", ()), (["birk", " ", 3], ("birk",)), ("birk, robo", ("birk", "robo")),
])
def test_release_authors_setting_is_parsed(raw, expected):
    _enabled, _per_tick, authors = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"loop_breaker_release_authors": raw}}
    )
    assert authors == expected


def test_unreadable_config_fails_safe_with_no_release_authors():
    def boom():
        raise OSError("unreadable")

    assert _resolve_auto_decompose_settings(boom) == (False, 3, ())
```

In `tests/gateway/test_kanban_auto_decompose_secret_scope.py`, both fakes (lines 36 and 76) become:

```python
    fake = SimpleNamespace(list_triage_ids=lambda: ["t1"], decompose_task=fake_decompose,
                           release_loop_parked=lambda release_authors=(): [])
```

- [ ] **Step 2: Run, verify they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_live.py -v`
Expected FAIL: `test_disabled_when_flag_false`, `test_release_authors_setting_is_parsed[*]`,
`test_unreadable_config_fails_safe_with_no_release_authors` (`ValueError: not enough values to unpack`, or 2-tuple
≠ 3-tuple); `test_foreign_comment_releases_the_card_as_is` and the second half of
`test_release_authors_narrow_who_may_wake_the_card` (status stays `triage`, and the tick takes no positional
`release_authors` → `TypeError`). PASS: the worker-comment and before-loop tests (already parked by Task 3).

- [ ] **Step 3: Implement the release predicate and action in the sibling**

Append to `hermes_cli/kanban_db_loop_breaker.py`:

```python
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
```

- [ ] **Step 4: Decompose-module wrapper**

Append to `hermes_cli/kanban_decompose.py`:

```python
def release_loop_parked(*, release_authors: tuple[str, ...] = ()) -> list[str]:
    """Promote, as-is, the loop-parked triage cards a non-worker comment answered
    (see ``kanban_db_loop_breaker.release_commented``); returns their ids."""
    from hermes_cli.kanban_db_loop_breaker import release_commented

    with kbc.connect_closing() as conn:
        return release_commented(conn, release_authors=release_authors)
```

- [ ] **Step 5: Settings resolver returns the release authors**

`gateway/kanban_watchers_common.py`: add above `_resolve_auto_decompose_settings`:

```python
def _release_authors(raw: Any) -> tuple[str, ...]:
    """``kanban.loop_breaker_release_authors`` (list or comma-separated string) -> names; unset/blank -> ()."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(a.strip() for a in raw if isinstance(a, str) and a.strip())
```

and change `_resolve_auto_decompose_settings`. The signature becomes
`-> tuple[bool, int, tuple[str, ...]]`, append this paragraph to the docstring:
`Also returns kanban.loop_breaker_release_authors (who may wake a loop-breaker triage card by commenting; () = anyone but its worker).`.
`return False, 3` becomes `return False, 3, ()`, and the last line becomes:

```python
    return (
        bool(kcfg.get("auto_decompose", True)), max(per_tick, 1),
        _release_authors(kcfg.get("loop_breaker_release_authors")),
    )
```

- [ ] **Step 6: Tick: release first, then decompose the filtered list**

`gateway/kanban_watchers_dispatcher.py`, replace `auto_decompose_tick` and add `_release_loop_parked` right after it
(keep `_decompose_one`, but swap its literal for the constant):

```python
    def auto_decompose_tick(self, auto_decompose_per_tick: int, release_authors: tuple[str, ...] = ()) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        Cards the unblock-loop breaker parked are never decomposed; one answered
        by a comment from someone other than its worker is promoted as-is first
        (no LLM, so outside the cap).
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
            from hermes_cli.kanban_db import scoped_current_board
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        from hermes_cli import kanban_db as _kb
        with _default_profile_secret_scope(), _kb.pin_first_board_resolution():
            for slug in self._board_slugs():
                # The decomposer connects with no board kwarg. Pin the board for this context only: an
                # env pin is process-wide, so for the whole aux-LLM call every gateway turn, kanban tool
                # and spawned subprocess that names no board would resolve this one instead.
                with scoped_current_board(slug):
                    self._release_loop_parked(_decomp, slug, release_authors)
                    if attempted >= auto_decompose_per_tick:
                        continue
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        successes += self._decompose_one(_decomp, slug, tid)
        return successes

    @staticmethod
    def _release_loop_parked(_decomp: Any, slug: str, release_authors: tuple[str, ...]) -> None:
        """Promote, as-is, this board's loop-parked cards a non-worker comment answered."""
        try:
            released = _decomp.release_loop_parked(release_authors=release_authors)
        except Exception as exc:
            logger.debug("kanban auto-decompose: loop-parked release failed on board %s (%s)", slug, exc)
            return
        for tid in released:
            logger.info("kanban auto-decompose [%s]: %s released from loop-breaker triage by a comment", slug, tid)
```

In `_decompose_one`: `outcome = _decomp.decompose_task(tid, author="auto-decomposer")` →

```python
        from hermes_cli.kanban_db_loop_breaker import AUTO_DECOMPOSER_AUTHOR

        try:
            outcome = _decomp.decompose_task(tid, author=AUTO_DECOMPOSER_AUTHOR)
```

(the import goes above the existing `try`; the rest of `_decompose_one` is unchanged).

- [ ] **Step 7: Caller and config default**

`gateway/kanban_watchers.py:304-307`:

```python
                    _ad_enabled, _ad_per_tick, _ad_release_authors = _resolve_auto_decompose_settings(_load_config)
                    # See #49638.
                    if _ad_enabled:
                        await _to_thread_process_service(
                            dispatcher.auto_decompose_tick, _ad_per_tick, _ad_release_authors)
```

`hermes_cli/config_defaults.py`, after `"auto_decompose_per_tick": 3,`:

```python
        # Who may wake a card the unblock-loop breaker parked in triage by commenting on it (the next
        # auto-decompose tick then promotes it as-is). None/empty = anyone but the card's own worker
        # (its assignee) and the auto-decomposer; set = only these authors (list or comma-separated).
        "loop_breaker_release_authors": None,
```

- [ ] **Step 8: Run, verify green**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/hermes_cli/test_kanban_decompose.py tests/gateway/test_internal_event_pin_wiring.py`
Expected: all PASS. (`test_internal_event_pin_wiring.py` is the only other gateway test that imports
`gateway.kanban_watchers`, which makes it the import smoke for the watcher loop. Verified 2026-11-13: no test file named
`test_kanban_watchers.py` exists.) Then run `grep -rn "_resolve_auto_decompose_settings\|auto_decompose_tick" tests gateway hermes_cli`
and confirm that every caller unpacks three values or passes the new arg. On base the only callers are
`gateway/kanban_watchers.py:304-307` and the two test files above.

- [ ] **Step 9: Commit**

```bash
git add hermes_cli/kanban_db_loop_breaker.py hermes_cli/kanban_decompose.py gateway/kanban_watchers_common.py gateway/kanban_watchers_dispatcher.py gateway/kanban_watchers.py hermes_cli/config_defaults.py tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_secret_scope.py
git commit -m "feat(kanban): a non-worker comment releases a loop-breaker triage card as-is"
```

---

### Task 5: D4: `unblock` releases a loop-parked triage card

**Files:**
- Modify: `hermes_cli/kanban_db.py:3557-3598` (`unblock_task`), `hermes_cli/kanban.py:1032`, `tools/kanban_tools.py:1270` (strings only)
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (extend, reuses `_loop_parked` from Task 2)

**Interfaces:**
- Consumes: `kanban_db_loop_breaker.is_loop_parked` (Task 2).
- Produces: `unblock_task(conn, task_id) -> bool`, which now also succeeds for a loop-parked triage card.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_block_kinds.py`:

```python
def test_unblock_releases_a_loop_parked_triage_card(kanban_home: Path) -> None:
    """D4: `kanban_unblock` is the commissioner's explicit way out; recurrences survive."""
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        assert kb.unblock_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert [e for e in kb.list_events(conn, tid) if e.kind == "unblocked"]
        # The loop memory survives: the same block again goes straight back to triage.
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="which variant?", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"


def test_unblock_refuses_a_fresh_triage_card(kanban_home: Path) -> None:
    """D4: an idea card in triage is not "blocked"; unblock must not promote it past specify."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="idea", triage=True)
        assert kb.unblock_task(conn, tid) is False
        assert kb.get_task(conn, tid).status == "triage"


def test_unblock_of_a_loop_parked_card_regates_on_open_parents(kanban_home: Path) -> None:
    """D4: restored like a blocked card, so an open parent keeps it in todo."""
    with kbc.connect_closing() as conn:
        tid = _loop_parked(conn)
        parent = kb.create_task(conn, title="upstream", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "todo"
```

Verified 2026-11-13: `task_links` is `(parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY(...))`
(`hermes_cli/kanban_db.py:880-884`). The raw insert is needed because `link_tasks` refuses to gate a non-todo child.

- [ ] **Step 2: Run, verify they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py -v`
Expected: `test_unblock_releases_a_loop_parked_triage_card` and `test_unblock_of_a_loop_parked_card_regates_on_open_parents`
FAIL (`unblock_task` returns False). `test_unblock_refuses_a_fresh_triage_card` PASSES (it guards the refusal through the change).

- [ ] **Step 3: Implement**

Replace `unblock_task` in `hermes_cli/kanban_db.py` (keep the existing comment block about `block_kind`/`block_recurrences` verbatim):

```python
def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first. A ``triage`` card
    the unblock-loop breaker parked is restored the same way; a fresh one is refused."""
    from hermes_cli.kanban_db_loop_breaker import is_loop_parked

    now = int(time.time())
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status == "triage" and not is_loop_parked(conn, task_id):
            return False
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if status in ("blocked", "triage")
            else "ready"
        )
        _reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled", "triage"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled', 'triage')", (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
        return True
```

The `'triage'` in the UPDATE is only reachable after the marker check above (same txn).

Failure strings (no schema change):
- `hermes_cli/kanban.py:1032`: `f"cannot unblock {tid} (not blocked/scheduled?)"` → `f"cannot unblock {tid} (not blocked/scheduled/loop-parked in triage?)"`
- `tools/kanban_tools.py:1270`: `f"could not unblock {tid} (not blocked or unknown)"` → `f"could not unblock {tid} (not blocked, scheduled or loop-parked in triage, or unknown)"`

- [ ] **Step 4: Run, verify green and check the size ratchet**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_blocked_sticky.py tests/tools/test_kanban_tools.py`
Expected: all PASS (verified 2026-11-13: no test asserts either failure string). `wc -l hermes_cli/kanban_db.py` prints < 4231.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban.py tools/kanban_tools.py tests/hermes_cli/test_kanban_block_kinds.py
git commit -m "feat(kanban): unblock releases a card the loop breaker parked in triage"
```

---

### Task 6: D5 docs + lint gate

**Files:**
- Modify: `website/docs/user-guide/features/kanban.md:414-432` (note), `:831` (config table), `website/docs/reference/cli-commands.md:849, 854, 855`

- [ ] **Step 1: Update the note**

In `kanban.md`, insert this paragraph after the paragraph ending "or raise\n`BLOCK_RECURRENCE_LIMIT` if the loop is expected." and before `:::`:

```markdown
While a task sits in `triage` because of the loop breaker, no automation moves
it: the gateway auto-decomposer and the `specify --all` / `decompose --all`
sweeps skip it (a task you create in triage is still decomposed as before). It
leaves triage only through one of these:

- `hermes kanban unblock <id>` (or the `kanban_unblock` tool) restores it like a
  blocked task — same source phase, same parent gate;
- a comment from anyone other than the task's own worker (its assignee): the next
  dispatcher tick promotes it **as-is**, without rewriting its title or body. Set
  `kanban.loop_breaker_release_authors` to restrict whose comments count, so an
  automation's note cannot wake it. This path needs `kanban.auto_decompose: true`;
- an explicit `hermes kanban specify <id>` / `decompose <id>`, the dashboard's
  Specify/Decompose buttons, or dragging the card out of triage.
```

- [ ] **Step 2: Config table row** (after the `auto_decompose_per_tick` row, line 831):

```markdown
| `loop_breaker_release_authors` | unset | Whose comment wakes a task the unblock-loop breaker parked in triage (promoted as-is on the next tick). Unset/empty = anyone but the task's own worker (assignee) and the auto-decomposer; a list (or comma-separated string) = only those authors. |
```

- [ ] **Step 3: CLI reference rows** (`cli-commands.md`)

- line 849 → `| \`unblock <id>\` | Restore a blocked task, or one the unblock-loop breaker parked in triage, to its source phase (\`review\` or \`ready\`), or \`todo\` while dependencies remain open. A task you created in triage is refused. |`
- line 854: append before the closing `|`: ` \`--all\` skips tasks the unblock-loop breaker parked in triage.`
- line 855: after `Also runs automatically every dispatcher tick when \`kanban.auto_decompose: true\` (the default).` insert ` \`--all\` and the automatic run skip tasks the unblock-loop breaker parked in triage.`

- [ ] **Step 4: Docs drift + lint**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/ci/test_docs_drift.py`
Expected: PASS (the new config key is documented).
Run: `/home/birk/.hermes/hermes-agent/venv/bin/python scripts/check`
Expected: exit 0, no new `FILE_LINES` / `FUNC_*` finding for `hermes_cli/kanban_db.py`. ANNAHME: `scripts/check`
fetches pinned tools and may hang behind egress like `run_tests.sh`. If it can't run, record that in the coder card
report and run `/home/birk/.hermes/hermes-agent/venv/bin/python -m scripts.code_health --help` to find an
offline ratchet mode.

- [ ] **Step 5: Full targeted run (the Testkommando)**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_95a7e463/tmp scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify_db.py`
Expected: `5 files, 36 tests passed, 0 failed`: 16 baseline + 20 new (live 12, counting the parametrized settings
test as 5; block_kinds 5; decompose 2; specify 1).

- [ ] **Step 6: Commit**

```bash
git add website/docs/user-guide/features/kanban.md website/docs/reference/cli-commands.md
git commit -m "docs(kanban): loop-breaker triage cards are never auto-decomposed; how they leave triage"
```

---

### Task 7 (merge card / ops, NOT the coder card): roll out on this host

This task changes no code. It runs after the `feat/*` branch is merged into the fork and the line is added to
`~/.hermes/hermes-agent-features.txt`.

1. Set the narrowing on the config the live gateway's dispatcher reads. The tick reads config through
   `hermes_cli.config.load_config()` at tick time in the gateway process, outside any profile scope, so it reads the
   **launch profile's** `config.yaml`: `~/.hermes/config.yaml` (default profile).
   Measured by the architect 2026-11-13: `systemctl --user cat hermes-gateway` sets
   `HERMES_HOME=/mnt/HC_Volume_106183673/hermes`, and `readlink -f ~/.hermes` is that same path; its `config.yaml` has no
   `kanban:` section today (so `auto_decompose` runs with its default `true`). Re-check both lines before setting the key.
   Command: `hermes config set kanban.loop_breaker_release_authors birk` (the comma-separated string form avoids
   relying on list parsing in `config set`), then `hermes config get kanban.loop_breaker_release_authors` → `birk`.
   Value rationale: Birk's and Robo's comments on the boards carry author `birk` (measurements §1); the `factory-lead`
   automation note must not release a card.
2. No gateway restart is needed for the key (re-read every tick). The code itself needs the usual post-merge gateway restart.
3. Check on the board: an existing loop-parked card (e.g. hermes/t_72559b11 if it is still in triage) stays in triage for
   ≥ 3 dispatcher ticks (`hermes kanban show <id>` shows no new `specified` event), then a `birk` comment promotes it
   with an unchanged body.

---

## Self-review (done while writing)

- Spec coverage: D1 → Task 2; D2 → Task 3 (+ Task 1 RED); D3 → Task 4; D4 → Task 5; D5 → Task 6; config knob + ops → Tasks 4, 7.
  Regression "fresh triage still decomposed" → Task 1 control + Task 3 listing test.
- Type consistency: `release_authors: tuple[str, ...]` from `_resolve_auto_decompose_settings` → `auto_decompose_tick`
  → `release_loop_parked` → `release_commented` → `releasing_comment_author`. `is_loop_parked`, `loop_parked_ids` and
  `_parking_event_id` are defined in Task 2 and used in Tasks 3-5.
- Residual risks (spec § Residual risks): a worker comment authored with the generic fallback `"worker"` would release
  under the unset rule (only when no profile name resolves). The ops value `birk` closes it on this host. With
  `auto_decompose: false`, comments don't auto-release (documented).
- Residual risk added at architect review: on this host the kanban-chef LLM and Robo comment as profile `birk`, so a
  chef "[an Birk]" question comment also releases a parked card. That costs at most ONE extra worker run per such comment
  (the worker re-blocks, the breaker re-parks it). It is not the 1-2 min loop. Accepted; if it shows up in the event log,
  narrow it by giving the chef its own `--author`, not by changing this code.

## Architect review (t_95a7e463, 2026-11-13)
- Baseline re-run by the architect: the five target files on 547739e4 → `5 files, 16 tests passed, 0 failed`.
- Task 1 RED re-run by the architect (plan test block copied into a probe file, deleted again):
  `test_tick_leaves_a_loop_parked_card_in_triage` FAILS with `assert ('ready', 'LL...written body') == ('triage', ...)`,
  and the fresh-triage control PASSES. That reproduces the measured loop through the real tick.
