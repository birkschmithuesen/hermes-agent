# Kanban block-loop hold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card that the unblock-loop breaker routed to `triage` (`block_loop_detected`) is no longer rewritten and
re-promoted by the auto-decomposer; it waits until someone other than the blocking assignee comments, and is then
promoted `triage -> todo` unchanged, without an aux LLM call.

**Architecture:** One new kernel sibling `hermes_cli/kanban_db_block_loop.py` answers the three-state question
(`none` / `awaiting_reply` / `replied`) from `task_events` ordered by `id`, and owns the no-rewrite release. The
specifier and decomposer filter held cards out of `list_triage_ids()` and refuse them in `specify_task()` /
`decompose_task()` unless `force=True`. The gateway tick releases `replied` cards through the sibling before it
would otherwise decompose; the CLI single-id path passes `force=True`.

**Tech Stack:** Python 3, SQLite (`task_events` autoincrement id), pytest via `scripts/run_tests.sh`.

**Spec:** `docs/superpowers/specs/2026-11-12-kanban-block-loop-hold-design.md` (commit 8405c029e4e9). Design is decided;
do not reopen it. Where the code contradicted the spec, see "Contradictions (FRAGE)" below; each resolution was
accepted by architect 2026-11-12 (t_a1deee87) and the spec is amended to match.

## Fork context

- Implementation branch: `feat/kanban-block-loop-hold`, branched from `origin/main` (72c880370ff2 at planning time):
  `git switch -c feat/kanban-block-loop-hold origin/main`. No push, no merge, no PR. Add the branch to
  `hermes-agent-features.txt` only at merge time (spec §8), not in this plan.
- Hotspots shared with active feats (spec §8): `hermes_cli/kanban_specify.py` (feat/kanban-specify-asis-actor-events adds
  `promote_as_is()` directly above `list_triage_ids()`) and `gateway/kanban_watchers_dispatcher.py`
  (feat/kanban-gateway-live-caps adds `refresh_caps()` above `tick_once`). Keep edits inside the function bodies named
  below; new helpers go where each task says (never directly above `list_triage_ids()` or `tick_once`).

## Global Constraints

- State strings, exact: `"none"`, `"awaiting_reply"`, `"replied"` (spec §3).
- Events are ordered by `task_events.id`, never by `created_at` (spec §3).
- A *reply* = `commented` event with `id > L` whose payload `author`, stripped and lower-cased, is neither the task's
  current `assignee` (stripped, lower-cased) nor `auto-decomposer` (spec §3).
- Refusal reason starts with `held:` (spec §4).
- Release audit comment, author `auto-decomposer`, exact text:
  `Released from block-loop hold after reply by <author>; title and body unchanged, promoted to todo.` (spec §4).
- Release uses `kb.specify_triage_task(conn, task_id, author=...)` with no title/body/assignee: no new status, column or
  migration (spec §4).
- `hermes_cli/kanban_db.py` is a facade at 4231 lines (cap 2000, ratcheted down): **no new code in it** (AGENTS.md
  "Behaviour goes in a sibling, never a facade"; `scripts/code_health/config.py` `FILE_LINES`). See FRAGE-3.
- New functions: CC ≤ 20, ≤ 300 lines, nesting ≤ 6. Siblings late-import the facade inside functions.
- Tests: always `scripts/run_tests.sh <file>`, never bare pytest. Behaviour contracts, no source reading.
- Commits: conventional message + a `Reason:` line + `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Language: English for code, identifiers, comments, commits.

## Verified code facts (2026-11-12, base 72c880370ff2)

| Fact from brief/spec | Status | Where |
|---|---|---|
| `_route_block()`, `BLOCK_RECURRENCE_LIMIT = 2` | confirmed | `hermes_cli/kanban_db.py:3215`, `:115` |
| `add_comment()` writes `commented` with `{"author", "len"}`; payload author is **unstripped** | confirmed | `kanban_db.py:1671-1686` (`_append_event(..., {"author": author, ...})`) |
| `specify_triage_task()` flips `triage -> todo`, writes `specified` (payload `None` when nothing changed), comment only when a field changed, then `recompute_ready()` | confirmed | `kanban_db.py:3732-3786` |
| `list_events()` exists | confirmed; orders `created_at ASC, id ASC`, so tests use `e.id` for order | `kanban_db.py:1831` |
| `Event` has `.id` | confirmed | `kanban_db.py:756` |
| `decompose_task()`, `list_triage_ids()`, `_apply_single()` | confirmed | `hermes_cli/kanban_decompose.py:303`, `:339`, `:221` |
| fan-out exit writes `decomposed`, **not** `specified` | contradiction, FRAGE-1 | `hermes_cli/kanban_db_graph.py:159-161` |
| dashboard drag writes a `status` event (also for moves into/out of triage) | contradiction, FRAGE-1 | `plugins/kanban/dashboard/plugin_api.py:742` `_set_status_direct` |
| `_load_triage_task()`, `specify_task()`, `list_triage_ids()` | confirmed | `hermes_cli/kanban_specify.py:126`, `:190`, `:237` |
| `auto_decompose_tick()` pins board via `scoped_current_board(slug)`; `_decompose_one()` is a staticmethod | confirmed | `gateway/kanban_watchers_dispatcher.py:246-297` |
| `_run_triage_sweep()` calls `run_one(tid, author=author)` for both single id and `--all` | confirmed → single id **needs** `force=True` (Task 5) | `hermes_cli/kanban.py:1271-1308` |
| dashboard `_run_aux()` calls `fn(task_id, author=...)` | would be refused by the new guard → FRAGE-2 | `plugins/kanban/dashboard/plugin_api.py:123-128` |
| `kanban_home` fixture in block-kinds tests; `_running_task()` / `_make_running_again()` drive blocks | confirmed | `tests/hermes_cli/test_kanban_block_kinds.py:33-58` |
| aux stub: `patch("agent.auxiliary_client.call_llm", ...)` (decompose: `return_value=`; specify: returns `(patcher, mock)`) | confirmed; tests count `mock.call_count` | `tests/hermes_cli/test_kanban_decompose.py:44`, `tests/hermes_cli/test_kanban_specify.py:51` |
| `tests/gateway/test_kanban_auto_decompose_live.py` tests only `_resolve_auto_decompose_settings` (pure, 24 lines) | tick tests go to a **new** file `tests/gateway/test_kanban_auto_decompose_block_loop.py`, built like `tests/gateway/test_kanban_auto_decompose_secret_scope.py` (`_dispatcher()` + patched `kwd._board_slugs`) | |

## Contradictions (FRAGE)

FRAGE-1: Spec §3 says state is `none` when "a `specified` or `promoted` event exists with `id > L`". The code has two
more ways a card leaves or re-enters triage after a loop: the fan-out path writes `decomposed`
(`kanban_db_graph.py:159`) and a dashboard drag writes `status` (`plugin_api.py:_set_status_direct`). With the spec's
list, a held card that a human drags `triage -> todo -> triage` stays held forever (no `specified`/`promoted`), which
contradicts the spec's own reasoning ("its current triage stay has another cause, e.g. a human dragged it back").
**Resolution, accepted by architect 2026-11-12 (t_a1deee87):** exit kinds = `specified`, `promoted`, `decomposed`,
`status`.

FRAGE-2: Spec §4 says the dashboard decompose/specify endpoints behave "as today" for `awaiting_reply` cards and are
out of scope. But they call `decompose_task(task_id, author=...)` / `specify_task(...)` without `force`, so the new
guard makes a dashboard click on a held card return `ok=False, reason="held: ..."` — not "as today".
**Resolution, accepted by architect 2026-11-12 (t_a1deee87):** Task 7 changes one line in `_run_aux()` to pass
`force=True` (an explicit click is a human decision, same rule as CLI single id), with one test in a new file.

FRAGE-3: Spec §5 puts `block_loop_hold_state()` and the state constants into `hermes_cli/kanban_db.py` next to
`_route_block()`. `kanban_db.py` is 4231 lines, over the 2000-line cap; the ratchet only lets it shrink, and root
AGENTS.md forbids new behaviour in a facade. **Resolution, accepted by architect 2026-11-12 (t_a1deee87):** new sibling
`hermes_cli/kanban_db_block_loop.py` (the `kanban_db_<topic>.py` pattern already used by `kanban_db_graph.py`).

FRAGE-4: Spec §5/§6.6 name `tests/gateway/test_kanban_auto_decompose_live.py` for the tick tests. That file only tests
the pure settings resolver; tick tests need a real board DB and a dispatcher. **Resolution, accepted by architect
2026-11-12 (t_a1deee87):** new file
`tests/gateway/test_kanban_auto_decompose_block_loop.py`; the final run (Task 8) runs both files plus
`tests/gateway/test_kanban_auto_decompose_secret_scope.py`, whose fake decomposer now also passes through the release
probe.

## File structure

| File | Change | Responsibility |
|---|---|---|
| `hermes_cli/kanban_db_block_loop.py` | create | state constants, `block_loop_hold_state()`, `release_block_loop_hold()` |
| `hermes_cli/kanban_specify.py` | modify | `_HELD_REASON`, `_held()`, `_unheld_ids()` after `_load_triage_task()`; guard in `specify_task()`; filter in `list_triage_ids()` |
| `hermes_cli/kanban_decompose.py` | modify | import the three helpers; guard in `decompose_task()`; filter in `list_triage_ids()` |
| `hermes_cli/kanban.py` | modify | `_run_triage_sweep()` passes `force=not all_flag` |
| `gateway/kanban_watchers_dispatcher.py` | modify | `_decompose_one()` releases first; module helper `_release_block_loop_hold()` after the class |
| `plugins/kanban/dashboard/plugin_api.py` | modify (FRAGE-2) | `_run_aux()` passes `force=True` |
| `website/docs/user-guide/features/kanban.md`, `website/docs/reference/cli-commands.md` | modify | describe the hold |
| `tests/hermes_cli/test_kanban_block_kinds.py` | extend | state function + release |
| `tests/hermes_cli/test_kanban_specify.py` | extend | specify guard, sweep filter, CLI single id |
| `tests/hermes_cli/test_kanban_decompose.py` | extend | decompose guard, sweep filter |
| `tests/gateway/test_kanban_auto_decompose_block_loop.py` | create | tick: acceptance 1-5 |
| `tests/plugins/test_kanban_dashboard_block_loop.py` | create (FRAGE-2) | dashboard click still rewrites |

## Acceptance map (spec §6)

| # | Spec §6 item | Test (file::name) | Task |
|---|---|---|---|
| 1 | held card survives the tick unchanged, aux 0 calls, no `specified` | `tests/gateway/test_kanban_auto_decompose_block_loop.py::test_tick_holds_block_loop_card_without_reply` (+ kernel `tests/hermes_cli/test_kanban_block_kinds.py::test_hold_state_awaiting_reply_after_block_loop`) | 6 (1) |
| 2 | after `add_comment(..., "birk", ...)` the tick promotes as is, aux 0, one `specified` without `changed_fields` | `...block_loop.py::test_tick_releases_replied_card_unchanged` (+ `test_kanban_block_kinds.py::test_release_promotes_replied_card_unchanged`) | 6 (2) |
| 3 | assignee's own comment does not release | `...block_loop.py::test_tick_ignores_assignee_comment` (+ `test_kanban_block_kinds.py::test_hold_state_ignores_assignee_and_auto_decomposer_comments`) | 6 (1) |
| 4 | plain triage card still decomposed | `...block_loop.py::test_tick_still_decomposes_plain_triage_card` | 6 |
| 5 | third identical block after release → triage, new loop event, held again | `...block_loop.py::test_tick_holds_again_after_third_identical_block` | 6 |
| 6 | four files green; 1-3 and 5 red on `origin/main` | Task 8 + "Base evidence" | 8 |

The card's own acceptance sentence (a `block_loop_detected` card with an unanswered question is NOT re-specified/promoted
until a new comment arrives) is acceptance item 1 plus item 3.

---

### Task 1: Hold-state function in a kernel sibling

**Files:**
- Create: `hermes_cli/kanban_db_block_loop.py`
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (append after `test_block_loop_detected_event_emitted`, line ~87)

**Interfaces:**
- Consumes: `tasks(id, status, assignee)`, `task_events(id, task_id, kind, payload)` rows; `conn.row_factory = sqlite3.Row`
  (set by `kanban_db_connect.connect`).
- Produces:
  - `HOLD_NONE = "none"`, `HOLD_AWAITING_REPLY = "awaiting_reply"`, `HOLD_REPLIED = "replied"`, `RELEASE_AUTHOR = "auto-decomposer"`
  - `block_loop_hold_state(conn: sqlite3.Connection, task_id: str) -> str` — one of the three constants
  - private `_hold(conn, task_id) -> tuple[str, Optional[str]]` — `(state, latest_reply_author)`; author is `None` unless `replied`

- [ ] **Step 1: Write the failing tests**

Add `from hermes_cli import kanban_db_block_loop as kbl` to the import block at the top of
`tests/hermes_cli/test_kanban_block_kinds.py` (after `from hermes_cli import kanban_db as kb`), then append (the file
already imports `kb`, `kbc`, `pytest`, `Path` and defines `kanban_home`, `_running_task`, `_make_running_again`):

```python
# ---------------------------------------------------------------------------
# Block-loop hold (t_a1deee87): a looped card waits for a reply
# ---------------------------------------------------------------------------


def _block_loop_card(conn, title="t"):
    """Two identical needs_input blocks -> triage with a block_loop_detected event."""
    tid = _running_task(conn, title=title)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    _make_running_again(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_hold_state_awaiting_reply_after_block_loop(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_ignores_assignee_and_auto_decomposer_comments(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, " Worker ", "still need B1 or B2")  # assignee, case/space-insensitive
        kb.add_comment(conn, tid, "auto-decomposer", "audit")
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_comment_before_the_loop_is_not_a_reply(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        kb.add_comment(conn, tid, "birk", "answered before the second block")
        assert kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        assert kbl.block_loop_hold_state(conn, tid) == "awaiting_reply"


def test_hold_state_replied_after_foreign_comment(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", "B1.")
        assert kbl.block_loop_hold_state(conn, tid) == "replied"


def test_hold_state_none_for_plain_triage_and_non_triage(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        raw = kb.create_task(conn, title="raw idea", triage=True)
        running = _running_task(conn)
        assert kbl.block_loop_hold_state(conn, raw) == "none"
        assert kbl.block_loop_hold_state(conn, running) == "none"
        assert kbl.block_loop_hold_state(conn, "t_missing") == "none"


@pytest.mark.parametrize("exit_kind", ["specified", "promoted", "decomposed", "status"])
def test_hold_state_none_once_card_left_triage_after_loop(kanban_home: Path, exit_kind: str) -> None:
    """A later exit event means the current triage stay has another cause (human drag-back)."""
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, exit_kind, None)
        assert kbl.block_loop_hold_state(conn, tid) == "none"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py`
Expected: collection error / FAIL with `ModuleNotFoundError: No module named 'hermes_cli.kanban_db_block_loop'`.

- [ ] **Step 3: Write the implementation**

Create `hermes_cli/kanban_db_block_loop.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py`
Expected: PASS (all tests in the file, including the 9 new cases: 5 plain + 4 parametrized).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db_block_loop.py tests/hermes_cli/test_kanban_block_kinds.py
git commit -m "feat(kanban): block-loop hold state for triage cards

Reason: the auto-decomposer cannot tell a block-loop card from a raw idea;
this answers none/awaiting_reply/replied from task_events ordered by id.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Release a replied card unchanged

**Files:**
- Modify: `hermes_cli/kanban_db_block_loop.py` (append after `block_loop_hold_state`)
- Test: `tests/hermes_cli/test_kanban_block_kinds.py` (append after Task 1's tests)

**Interfaces:**
- Consumes: Task 1 `_hold()`, `HOLD_REPLIED`, `RELEASE_AUTHOR`; facade `kb.specify_triage_task(conn, task_id, *, title=None, body=None, assignee=None, author=None) -> bool` (`kanban_db.py:3732`), `kb.add_comment(conn, task_id, author, body) -> int` (`kanban_db.py:1671`).
- Produces: `release_block_loop_hold(conn: sqlite3.Connection, task_id: str) -> Optional[str]` — the reply author when
  the card was promoted, `None` when the card is not `replied` (nothing written). Must be called outside an open write
  txn (`specify_triage_task` runs `recompute_ready` in its own txn).

- [ ] **Step 1: Write the failing tests**

```python
def test_release_promotes_replied_card_unchanged(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn, title="Roll out X")
        before = kb.get_task(conn, tid)
        kb.add_comment(conn, tid, "birk", "B1.")
        assert kbl.release_block_loop_hold(conn, tid) == "birk"
        after = kb.get_task(conn, tid)
        specified = [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
        comments = [(c.author, c.body) for c in kb.list_comments(conn, tid)]
    assert after.status in {"todo", "ready"}
    assert (after.title, after.body, after.assignee) == (before.title, before.body, before.assignee)
    assert len(specified) == 1
    assert not (specified[0].payload or {}).get("changed_fields")
    assert comments[-1] == (
        "auto-decomposer",
        "Released from block-loop hold after reply by birk; title and body unchanged, promoted to todo.",
    )


def test_release_is_a_noop_while_awaiting_reply(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        assert kbl.release_block_loop_hold(conn, tid) is None
        assert kb.get_task(conn, tid).status == "triage"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "specified"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py`
Expected: the two new tests FAIL with `AttributeError: module 'hermes_cli.kanban_db_block_loop' has no attribute 'release_block_loop_hold'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/kanban_db_block_loop.py`:

```python
def release_block_loop_hold(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Promote a replied block-loop card ``triage -> todo`` as is; the reply's author, else None.

    No LLM and no rewrite: the body is already a spec and the answer lives in the comment
    thread every worker receives. If the reply did not settle it, the next identical block
    lands back in triage (``block_recurrences`` survives) and is held again.
    """
    from hermes_cli import kanban_db as kb

    state, reply_author = _hold(conn, task_id)
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_db_block_loop.py tests/hermes_cli/test_kanban_block_kinds.py
git commit -m "feat(kanban): release a replied block-loop card without rewrite

Reason: once someone other than the blocker answered, the card goes back to
todo with title and body untouched; no aux LLM call, audit comment only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Specifier refuses held cards and skips them in sweeps

**Files:**
- Modify: `hermes_cli/kanban_specify.py` — import block (line ~22), new helpers directly after `_load_triage_task()`
  (line ~135, NOT above `list_triage_ids()`: hotspot), `specify_task()` (line 190), `list_triage_ids()` body (line 237)
- Test: `tests/hermes_cli/test_kanban_specify.py` (append after `test_specify_task_happy_path`, line ~94)

**Interfaces:**
- Consumes: Task 1 `kbl.block_loop_hold_state`, `kbl.HOLD_AWAITING_REPLY`.
- Produces (decompose imports these in Task 4):
  - `_HELD_REASON: str` — starts with `"held:"`
  - `_held(task_id: str) -> bool`
  - `_unheld_ids(conn: sqlite3.Connection, tasks: Iterable[kb.Task]) -> list[str]`
  - `specify_task(task_id, *, author=None, timeout=None, force: bool = False) -> SpecifyOutcome`

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_specify.py`:

```python
# ---------------------------------------------------------------------------
# Block-loop hold (t_a1deee87)
# ---------------------------------------------------------------------------

def _block_loop_card(conn, title="Roll out X", body="Planned body."):
    """Drive a fresh card through two identical needs_input blocks into triage."""
    tid = kb.create_task(conn, title=title, body=body, assignee="worker")
    for _ in range(2):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        if kb.get_task(conn, tid).status == "blocked":
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_specify_task_refuses_held_block_loop_card(kanban_home):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
        before = kb.get_task(conn, tid)
    p, mock_fn = _patch_aux_client(jsonlib.dumps({"title": "LLM", "body": "rewrite"}))
    with p:
        outcome = spec.specify_task(tid, author="ace")
    assert outcome.ok is False
    assert outcome.reason.startswith("held:")
    assert mock_fn.call_count == 0
    with kbc.connect() as conn:
        after = kb.get_task(conn, tid)
    assert (after.status, after.title, after.body) == ("triage", before.title, before.body)


def test_specify_task_force_rewrites_held_card(kanban_home):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
    p, mock_fn = _patch_aux_client(jsonlib.dumps({"title": "LLM", "body": "rewrite"}))
    with p:
        outcome = spec.specify_task(tid, author="ace", force=True)
    assert outcome.ok is True
    assert mock_fn.call_count == 1


def test_specify_list_triage_ids_skips_held_card_until_reply(kanban_home):
    with kbc.connect() as conn:
        raw = kb.create_task(conn, title="raw idea", triage=True)
        held = _block_loop_card(conn)
    assert spec.list_triage_ids() == [raw]
    with kbc.connect() as conn:
        kb.add_comment(conn, held, "birk", "B1.")
    assert set(spec.list_triage_ids()) == {raw, held}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_specify.py`
Expected: `test_specify_task_refuses_held_block_loop_card` FAILS (`outcome.ok is True`, `call_count == 1`),
`test_specify_task_force_rewrites_held_card` FAILS (`TypeError: specify_task() got an unexpected keyword argument 'force'`),
`test_specify_list_triage_ids_skips_held_card_until_reply` FAILS (held id is listed).

- [ ] **Step 3: Write the implementation**

In `hermes_cli/kanban_specify.py`:

Imports (after `from hermes_cli import kanban_db_connect as kbc`; add `Iterable` to the `typing` import):

```python
import sqlite3
from typing import Iterable, Optional

from hermes_cli import kanban_db_block_loop as kbl
```

Directly after `_load_triage_task()`:

```python
_HELD_REASON = (
    "held: block loop detected; waiting for a comment from someone other than the assignee "
    "(name the task explicitly to rewrite it anyway)"
)


def _held(task_id: str) -> bool:
    """True while a block-loop card waits for a reply: rewriting it would only re-ask the question."""
    with kbc.connect_closing() as conn:
        return kbl.block_loop_hold_state(conn, task_id) == kbl.HOLD_AWAITING_REPLY


def _unheld_ids(conn: sqlite3.Connection, tasks: Iterable[kb.Task]) -> list[str]:
    """Ids of *tasks* minus block-loop cards still awaiting a reply (sweeps and the gateway tick)."""
    ids = []
    for t in tasks:
        if kbl.block_loop_hold_state(conn, t.id) == kbl.HOLD_AWAITING_REPLY:
            logger.debug("triage sweep: %s held after block loop, awaiting reply", t.id)
            continue
        ids.append(t.id)
    return ids
```

`specify_task()` signature and guard (guard goes right after the `_load_triage_task` check, before `_call_aux`):

```python
def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
    force: bool = False,
) -> SpecifyOutcome:
    """Specify one triage task and promote it to ``todo``. Expected failures
    (not in triage, held after a block loop, no aux client, API error, malformed
    reply) surface as ``ok=False`` so an ``--all`` sweep continues. ``force``
    rewrites a held card anyway (a human named it)."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, reason)
    if not force and _held(task_id):
        return SpecifyOutcome(task_id, False, _HELD_REASON)
    # ... rest unchanged
```

`list_triage_ids()` body:

```python
def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Task ids in the triage column, minus held block-loop cards; ``tenant`` narrows the sweep."""
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn, status="triage", tenant=tenant, include_archived=False)
        return _unheld_ids(conn, tasks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_specify.py`
Expected: PASS (including the pre-existing `test_specify_task_happy_path` and `test_cli_specify_tenant_filter`).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_specify.py tests/hermes_cli/test_kanban_specify.py
git commit -m "feat(kanban): specify skips and refuses held block-loop cards

Reason: a block-loop card awaiting a reply must not reach the aux LLM via
specify --all; a direct call refuses with held: unless force=True.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Decomposer refuses held cards and skips them in sweeps

**Files:**
- Modify: `hermes_cli/kanban_decompose.py` — import from `kanban_specify` (lines 29-31), `decompose_task()` (line 303),
  `list_triage_ids()` (line 339)
- Test: `tests/hermes_cli/test_kanban_decompose.py` (append after `test_decompose_returns_false_when_task_not_triage`)

**Interfaces:**
- Consumes: Task 3 `_HELD_REASON`, `_held(task_id) -> bool`, `_unheld_ids(conn, tasks) -> list[str]`.
- Produces: `decompose_task(task_id, *, author=None, timeout=None, force: bool = False) -> DecomposeOutcome`;
  `list_triage_ids(*, tenant=None) -> list[str]` without held cards (the gateway tick relies on this in Task 6).

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_decompose.py`:

```python
# ---------------------------------------------------------------------------
# Block-loop hold (t_a1deee87)
# ---------------------------------------------------------------------------

def _block_loop_card(conn, title="Roll out X", body="Planned body."):
    """Drive a fresh card through two identical needs_input blocks into triage."""
    tid = kb.create_task(conn, title=title, body=body, assignee="worker")
    for _ in range(2):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        if kb.get_task(conn, tid).status == "blocked":
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_decompose_task_refuses_held_block_loop_card(kanban_home):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
        before = kb.get_task(conn, tid)
    payload = jsonlib.dumps({"fanout": False, "title": "LLM", "body": "rewrite"})
    with _patch_aux_client(payload) as call_llm:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")
    assert outcome.ok is False
    assert outcome.reason.startswith("held:")
    assert call_llm.call_count == 0
    with kbc.connect() as conn:
        after = kb.get_task(conn, tid)
    assert (after.status, after.title, after.body) == ("triage", before.title, before.body)


def test_decompose_task_force_rewrites_held_card(kanban_home):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
    payload = jsonlib.dumps({"fanout": False, "title": "LLM", "body": "rewrite"})
    with _patch_aux_client(payload) as call_llm:
        outcome = decomp.decompose_task(tid, author="me", force=True)
    assert outcome.ok, outcome.reason
    assert call_llm.call_count == 1


def test_decompose_list_triage_ids_skips_held_card_until_reply(kanban_home):
    with kbc.connect() as conn:
        raw = kb.create_task(conn, title="raw idea", triage=True)
        held = _block_loop_card(conn)
    assert decomp.list_triage_ids() == [raw]
    with kbc.connect() as conn:
        kb.add_comment(conn, held, "birk", "B1.")
    assert set(decomp.list_triage_ids()) == {raw, held}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py`
Expected: the refuse test FAILS (`ok is True`, `call_count == 1`), the force test FAILS (`TypeError ... 'force'`), the
list test FAILS (held id listed).

- [ ] **Step 3: Write the implementation**

`hermes_cli/kanban_decompose.py` import (replace lines 29-31):

```python
from hermes_cli.kanban_specify import (
    _HELD_REASON, _call_aux, _extract_json_blob, _held, _load_triage_task, _task_prompt_fields,
    _title_body, _unheld_ids,
)
```

`decompose_task()`:

```python
def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
    force: bool = False,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, held after a block loop, no aux client, API error,
    malformed/empty reply) surface as ``ok=False``. ``force`` decomposes a
    held card anyway (a human named it)."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)
    if not force and _held(task_id):
        return DecomposeOutcome(task_id, False, _HELD_REASON)
    # ... rest unchanged
```

`list_triage_ids()`:

```python
def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """Task ids currently in the triage column, minus held block-loop cards."""
    with kbc.connect_closing() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
        return _unheld_ids(conn, rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py`
Expected: PASS (pre-existing fan-out/assignee tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban_decompose.py tests/hermes_cli/test_kanban_decompose.py
git commit -m "feat(kanban): decompose skips and refuses held block-loop cards

Reason: the gateway tick and decompose --all list triage ids through this
module; a block-loop card awaiting a reply must not be rewritten by the LLM.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: CLI single id passes `force=True`

Decision (brief asked to decide from the code): **yes, single id needs `force=True`.** `_run_triage_sweep()`
(`hermes_cli/kanban.py:1295-1296`) calls the same `run_one(tid, author=author)` for one id and for `--all`, so after
Tasks 3/4 `hermes kanban specify <held-id>` would print `held: ...` and exit 1, contradicting spec §4 ("a human naming
one card has decided"). `--all` keeps `force=False`; it only sees unheld ids anyway.

**Files:**
- Modify: `hermes_cli/kanban.py:1294-1296` (`_run_triage_sweep`, the per-id loop)
- Test: `tests/hermes_cli/test_kanban_specify.py` (append after Task 3's tests; reuses `_block_loop_card`, `_run_cli`, `_patch_aux_client`)

**Interfaces:**
- Consumes: Task 3/4 `specify_task(..., force=)` / `decompose_task(..., force=)`.
- Produces: no new API.

- [ ] **Step 1: Write the failing tests**

```python
def test_cli_specify_single_id_rewrites_held_card(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
    p, mock_fn = _patch_aux_client(jsonlib.dumps({"title": "Human-asked", "body": "rewrite"}))
    with p:
        rc = _run_cli("specify", tid)
    assert rc == 0
    assert mock_fn.call_count == 1
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).title == "Human-asked"


def test_cli_specify_all_leaves_held_card(kanban_home, capsys):
    with kbc.connect() as conn:
        tid = _block_loop_card(conn)
    p, mock_fn = _patch_aux_client(jsonlib.dumps({"title": "x", "body": "y"}))
    with p:
        rc = _run_cli("specify", "--all")
    assert rc == 0
    assert mock_fn.call_count == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_specify.py`
Expected: `test_cli_specify_single_id_rewrites_held_card` FAILS (`rc == 1`, stderr `kanban: specify <id>: held: ...`).
`test_cli_specify_all_leaves_held_card` already PASSES after Task 3 (regression guard; it is red on origin/main).

- [ ] **Step 3: Write the implementation**

In `hermes_cli/kanban.py`, `_run_triage_sweep()`:

```python
    ok_count = 0
    for tid in ids:
        # A human naming one card has decided, so a held block-loop card is rewritten anyway;
        # --all only ever lists unheld cards.
        outcome = run_one(tid, author=author, force=not all_flag)
```

Update the docstring to: `Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
``run_one(tid, author=..., force=<single id>)`` per id, print JSON or human lines, exit code.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_specify.py`
Expected: PASS.
Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py`
Expected: PASS (the decompose CLI shares `_run_triage_sweep`).

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/kanban.py tests/hermes_cli/test_kanban_specify.py
git commit -m "feat(kanban): single-id specify/decompose overrides the block-loop hold

Reason: naming one card on the CLI is a human decision to rewrite it; only
the --all sweep and the gateway tick respect the hold.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Gateway tick releases replied cards as is (acceptance 1-5) + docs

**Files:**
- Modify: `gateway/kanban_watchers_dispatcher.py` — `_KanbanDispatcher._decompose_one()` (lines 281-297) and a new
  module-level `_release_block_loop_hold()` placed after the class, before `_default_profile_secret_scope()` (line 300).
  Do not touch `tick_once` or anything above it (hotspot).
- Modify: `website/docs/user-guide/features/kanban.md` (after the loop-breaker paragraph ending line 431),
  `website/docs/reference/cli-commands.md:855` (`decompose` row).
- Create: `tests/gateway/test_kanban_auto_decompose_block_loop.py`

**Interfaces:**
- Consumes: Task 2 `kbl.release_block_loop_hold(conn, task_id) -> Optional[str]`; Task 4 `list_triage_ids()` without
  held cards and `decompose_task()` refusing held cards.
- Produces: `_release_block_loop_hold(tid: str) -> Optional[str]` (module-private). The board is already pinned by
  `scoped_current_board(slug)` in `auto_decompose_tick`, so the helper connects without a board kwarg.

Behaviour per state in the tick: `awaiting_reply` never reaches `_decompose_one` (filtered by `list_triage_ids`, so it
also does not consume the per-tick budget; the filter logs at debug); `replied` → release, return 1, no aux call;
`none` → `decompose_task` as today.

- [ ] **Step 1: Write the failing tests**

Create `tests/gateway/test_kanban_auto_decompose_block_loop.py`:

```python
"""Auto-decompose tick vs. block-loop triage cards (t_a1deee87).

Regression for t_72559b11: a card the unblock-loop breaker routed to triage was
rewritten by the aux LLM and promoted seven times in 17 minutes while its
question stayed unanswered. The tick must hold such a card until someone other
than the blocker comments, then promote it unchanged.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb_mod: ["default"])
    kb.init_db()
    return home


@pytest.fixture
def call_llm():
    """Aux LLM stub; tests assert on ``call_count``."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = jsonlib.dumps(
        {"fanout": False, "title": "LLM rewrite", "body": "LLM rewritten body"})
    stub = MagicMock(return_value=resp)
    with patch("agent.auxiliary_client.call_llm", stub):
        yield stub


def _tick() -> int:
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings).auto_decompose_tick(5)


def _claim(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _block_loop_card(conn) -> str:
    """Two identical needs_input blocks with no other comment -> triage + block_loop_detected."""
    tid = kb.create_task(conn, title="Roll out X", body="Planned body.\n- step 1", assignee="worker")
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.unblock_task(conn, tid)
    _claim(conn, tid)
    assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _snapshot(tid):
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)
    return task, events


def test_tick_holds_block_loop_card_without_reply(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    before, _ = _snapshot(tid)
    _tick()
    after, events = _snapshot(tid)
    assert call_llm.call_count == 0
    assert after.status == "triage"
    assert (after.title, after.body) == (before.title, before.body)
    assert not [e for e in events if e.kind == "specified"]


def test_tick_ignores_assignee_comment(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "worker", "still need B1 or B2")
    _tick()
    after, events = _snapshot(tid)
    assert call_llm.call_count == 0
    assert after.status == "triage"
    assert not [e for e in events if e.kind == "specified"]


def test_tick_releases_replied_card_unchanged(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    before, _ = _snapshot(tid)
    _tick()  # held
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "birk", "B1.")
    assert _tick() == 1
    after, events = _snapshot(tid)
    specified = [e for e in events if e.kind == "specified"]
    assert call_llm.call_count == 0
    assert after.status in {"todo", "ready"}
    assert (after.title, after.body) == (before.title, before.body)
    assert len(specified) == 1
    assert not (specified[0].payload or {}).get("changed_fields")


def test_tick_holds_again_after_third_identical_block(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
        kb.add_comment(conn, tid, "birk", "B1.")
    assert _tick() == 1
    with kbc.connect_closing() as conn:
        first_loop = max(e.id for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected")
        _claim(conn, tid)
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
    after, events = _snapshot(tid)
    assert after.status == "triage"
    assert max(e.id for e in events if e.kind == "block_loop_detected") > first_loop
    _tick()
    held, events = _snapshot(tid)
    assert held.status == "triage"
    assert call_llm.call_count == 0
    assert len([e for e in events if e.kind == "specified"]) == 1


def test_tick_still_decomposes_plain_triage_card(kanban_home, call_llm):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="raw idea", triage=True, assignee="worker")
    assert _tick() == 1
    after, _ = _snapshot(tid)
    assert call_llm.call_count == 1
    assert after.status in {"todo", "ready"}
    assert after.title == "LLM rewrite"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_block_loop.py`
Expected after Tasks 1-5 (filter + guard already in place): `test_tick_releases_replied_card_unchanged` and
`test_tick_holds_again_after_third_identical_block` FAIL — the replied card is decomposed by the LLM
(`call_llm.call_count == 1`, title becomes `LLM rewrite`, `specified` has `changed_fields`). The hold, assignee and
plain-triage tests already PASS (they are red on origin/main, see "Base evidence").

- [ ] **Step 3: Write the implementation**

In `gateway/kanban_watchers_dispatcher.py`, replace `_decompose_one()`:

```python
    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Release a replied block-loop card as is, else decompose; 1 on success, 0 otherwise."""
        try:
            released_by = _release_block_loop_hold(tid)
            outcome = None if released_by else _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: triage handling crashed on %s", tid)
            return 0
        if released_by:
            logger.info("kanban auto-decompose [%s]: %s released from block-loop hold after reply by %s",
                        slug, tid, released_by)
            return 1
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1
```

After the class, before `_default_profile_secret_scope()`:

```python
def _release_block_loop_hold(tid: str) -> Optional[str]:
    """Promote a replied block-loop card unchanged (no aux call); the reply's author, else None.

    Its body is already a spec and the answer sits in the comment thread every worker
    receives, so an LLM rewrite only re-asks the question (t_72559b11). The caller has
    pinned the board, so the connection names none.
    """
    from hermes_cli import kanban_db_block_loop as kbl

    with _kbc().connect_closing() as conn:
        return kbl.release_block_loop_hold(conn, tid)
```

Docs — `website/docs/user-guide/features/kanban.md`, insert after line 431 (before `:::`):

```markdown

A card the loop breaker sent to `triage` is **held**: the auto-decomposer, `decompose --all` and `specify --all` skip it
(no LLM call, no rewrite) until someone other than its assignee comments on it. The next dispatcher tick then moves it
`triage -> todo` with title and body unchanged and an `auto-decomposer` audit comment; the answer reaches the worker
through the comment thread. If the answer does not settle it, the next same-cause block lands it in `triage` again and
it is held again. `hermes kanban decompose <id>` / `specify <id>` on a held card rewrites it anyway.
```

`website/docs/reference/cli-commands.md:855`, append to the `decompose` row's description (before the trailing ` |`):
` Cards held after an unblock loop are skipped by `--all` and the automatic tick until someone other than the assignee comments; naming the id rewrites them anyway.`

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_block_loop.py`
Expected: PASS (5 tests).
Run: `scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_secret_scope.py`
Expected: PASS (its fake decomposer's id `t1` does not exist on the temp board → state `none` → `decompose_task` as before).

- [ ] **Step 5: Commit**

```bash
git add gateway/kanban_watchers_dispatcher.py tests/gateway/test_kanban_auto_decompose_block_loop.py website/docs/user-guide/features/kanban.md website/docs/reference/cli-commands.md
git commit -m "fix(kanban): hold block-loop triage cards until a reply arrives

Reason: the auto-decomposer rewrote and re-promoted block-loop cards with the
question still open (t_72559b11: 7 LLM rewrites, 0 human decisions); now the
tick waits for a non-assignee comment and promotes the card unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Dashboard click keeps rewriting held cards (FRAGE-2, accepted by architect 2026-11-12 (t_a1deee87))

**Files:**
- Modify: `plugins/kanban/dashboard/plugin_api.py:123-128` (`_run_aux`)
- Create: `tests/plugins/test_kanban_dashboard_block_loop.py` (new file: `tests/plugins/test_kanban_dashboard_plugin.py`
  is 1261 lines; keep it from growing)

**Interfaces:**
- Consumes: Task 3/4 `force=` keyword on `specify_task` / `decompose_task` (the only two `_run_aux` targets:
  `plugin_api.py:1029`, `:1606`).
- Produces: no new API, no new plugin→core import (the plugin already imports these modules dynamically; no boundary
  baseline change).

- [ ] **Step 1: Write the failing test**

```python
"""Dashboard Specify on a held block-loop card (t_a1deee87): an explicit click is a human decision."""

from __future__ import annotations

import importlib.util
import json as jsonlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_router():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_block_loop_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _block_loop_card(conn):
    tid = kb.create_task(conn, title="Roll out X", body="Planned body.", assignee="worker")
    for _ in range(2):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="B1 or B2?", kind="needs_input")
        if kb.get_task(conn, tid).status == "blocked":
            assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_dashboard_specify_rewrites_held_card(client, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _block_loop_card(conn)
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = jsonlib.dumps({"title": "Clicked", "body": "rewrite"})
    call_llm = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", call_llm)

    body = client.post(f"/api/plugins/kanban/tasks/{tid}/specify", json={"author": "birk"}).json()

    assert body["ok"] is True, body
    assert call_llm.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_block_loop.py`
Expected: FAIL — `body["ok"] is False`, reason starts with `held:`.

- [ ] **Step 3: Write the implementation**

```python
def _run_aux(board: Optional[str], module: str, fn: str, task_id: str, author: Optional[str]) -> Any:
    """Run a slow auxiliary-LLM task helper (``hermes_cli.<module>.<fn>``) with the board pinned;
    the module is imported lazily so a missing aux client can't break plugin load. ``force``: a
    click on one card is a human decision, so a block-loop hold does not apply."""
    def _run():
        return getattr(importlib.import_module(f"hermes_cli.{module}"), fn)(
            task_id, author=(author or None), force=True)
    return _with_board_pinned(board, _run)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_block_loop.py`
Expected: PASS.
Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py`
Expected: PASS (existing specify/decompose endpoint tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_dashboard_block_loop.py
git commit -m "fix(kanban-dashboard): explicit Specify/Decompose click overrides block-loop hold

Reason: spec keeps the dashboard buttons as today; without force=True the new
guard would refuse a human's click on a held card.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Full verification

**Files:** none changed.

- [ ] **Step 1: Run the spec's four files plus the new and neighbouring files**

```bash
scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_specify.py tests/gateway/test_kanban_auto_decompose_live.py tests/gateway/test_kanban_auto_decompose_block_loop.py tests/gateway/test_kanban_auto_decompose_secret_scope.py tests/plugins/test_kanban_dashboard_block_loop.py tests/plugins/test_kanban_dashboard_plugin.py
```

Expected: all files PASS, 0 failed. Record the summary line in the final report.

- [ ] **Step 2: Lint/ratchet**

Run: `python scripts/check`
Expected: exit 0 (no `FILE_LINES` growth on `hermes_cli/kanban_db.py`; new functions under CC 20).

- [ ] **Step 3: Prove red on origin/main**

Copy only the new gateway test file onto a scratch branch from `origin/main` and run it:

```bash
git switch -c scratch/block-loop-red origin/main
git checkout feat/kanban-block-loop-hold -- tests/gateway/test_kanban_auto_decompose_block_loop.py
scripts/run_tests.sh tests/gateway/test_kanban_auto_decompose_block_loop.py
git reset --hard
git switch feat/kanban-block-loop-hold
git branch -D scratch/block-loop-red
```

Expected on origin/main: `test_tick_holds_block_loop_card_without_reply`, `test_tick_ignores_assignee_comment`,
`test_tick_releases_replied_card_unchanged`, `test_tick_holds_again_after_third_identical_block` FAIL (aux LLM called,
card rewritten and promoted); `test_tick_still_decomposes_plain_triage_card` PASSES. That is spec §6.6 ("tests 1-3 and
5 are red on origin/main"). The kernel/specify/decompose tests are red on origin/main by `ImportError`/`TypeError`
(new module, new `force` keyword).

- [ ] **Step 4: No commit** (verification only). Report the two run summaries and the `scripts/check` result.

## Base evidence (origin/main 72c880370ff2)

**ANNAHME (not executed):** at planning time `scripts/run_tests.sh` could not provision its test environment in this
worktree (no `.venv`; the PM bootstrap retried `urlopen error timed out` for over 15 minutes and was stopped). A probe
file with the four tick scenarios of Task 6 (held, assignee comment, reply, plain triage) was written against the
unmodified base, could not be run, and was deleted without being committed.

What the base code shows, line by line (so the red/green claim is verifiable without running it):

- `kanban_decompose.list_triage_ids()` (`hermes_cli/kanban_decompose.py:339-343`) returns every `status='triage'` id,
  so the block-loop card is listed.
- `gateway/kanban_watchers_dispatcher.py:282-285` `_decompose_one()` calls `decompose_task(tid, author="auto-decomposer")`
  with no state check.
- `decompose_task()` (`kanban_decompose.py:303-336`) calls `_call_aux` unconditionally once the task is in triage, then
  `_apply_single()` → `kb.specify_triage_task(..., title=, body=)` → status `todo`/`ready`, `specified` with
  `changed_fields`.

So on origin/main:

| Test | Expected on origin/main |
|---|---|
| `test_tick_holds_block_loop_card_without_reply` (acc. 1) | RED: `call_llm.call_count == 1`, status `ready`, title `LLM rewrite` |
| `test_tick_releases_replied_card_unchanged` (acc. 2) | RED: the first `_tick()` already rewrites; aux called |
| `test_tick_ignores_assignee_comment` (acc. 3) | RED: aux called, card promoted |
| `test_tick_still_decomposes_plain_triage_card` (acc. 4) | GREEN (existing behaviour) |
| `test_tick_holds_again_after_third_identical_block` (acc. 5) | RED: aux called on the first tick |
| Tasks 1-5 tests | RED by `ModuleNotFoundError` (`kanban_db_block_loop`) / `TypeError` (`force`) / held id listed |

Task 8 Step 3 replaces this ANNAHME with the real run; the implementer records its output in the final report.

## Self-review

- Spec §2 A (hold) → Tasks 1, 3, 4, 6; release without rewrite → Tasks 2, 6; B rejected → nothing rewrites a held card
  on any automatic path.
- Spec §3 definitions → Task 1 (exit kinds widened per FRAGE-1).
- Spec §4 table: tick column → Task 6; `list_triage_ids` row → Tasks 3, 4; direct call row → Tasks 3, 4; CLI single id
  → Task 5; dashboard row → Task 7 (FRAGE-2); audit comment text → Task 2.
- Spec §5 files → all covered; `kanban_db.py` replaced by sibling (FRAGE-3); tick tests in a new file (FRAGE-4).
- Spec §6 → acceptance map above. Spec §7 out of scope untouched.
