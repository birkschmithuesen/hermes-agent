# Design — the auto-decomposer must not un-park a loop-breaker triage card (t_95a7e463)

Stand 2026-11-13, base origin/main 547739e4. Evidence: `2026-11-13-auto-decompose-loop-breaker-measurements.md`
(same directory). Behaviour source of truth: decisions D1-D5 below (taken by the architect in the card brief; not
re-opened here).

## Problem

`block_task` routes a task to `triage` once the same block kind recurs `BLOCK_RECURRENCE_LIMIT` (2) times
(`hermes_cli/kanban_db.py:3238-3240`, event `block_loop_detected`). The gateway dispatcher's auto-decompose tick
(`gateway/kanban_watchers_dispatcher.py:246`, `auto_decompose_tick`) lists every triage task
(`hermes_cli/kanban_decompose.py:339`, `list_triage_ids`), LLM-rewrites it, and promotes it
`triage -> todo -> ready` through `kb.specify_triage_task`. The worker respawns into the same wall, blocks again,
and `block_recurrences` (which survives unblock and specify) sends it straight back to triage. Measured: 13 cycles
in 25 min on wing/t_a096323e, 64 on hermes/t_72559b11. needs_input, capability and transient blocks all loop.
The documented promise (`website/docs/user-guide/features/kanban.md:421-431`: triage "for orchestration attention",
"where a cron would just keep unblocking it") is broken: the auto-decomposer is that cron.

Card acceptance: a needs_input card leaves triage only through a comment from its commissioner or an explicit
`kanban_unblock`.

## Decisions (from the brief)

- **D1 Marker.** A card is *loop-parked* when its status is `triage` and the newest event that put it into triage is
  `block_loop_detected`. It's derived from the event log, so the schema gets no new column.
- **D2 Skip.** The gateway auto-decompose tick and the `hermes kanban decompose --all` / `specify --all` sweeps skip
  loop-parked cards. Explicit single-id `specify <id>` / `decompose <id>`, the dashboard Specify/Decompose buttons
  and a dashboard drag still work: they are the human way out.
- **D3 Release by comment.** If a `commented` event newer than the parking `block_loop_detected` event exists whose
  author is neither the task's assignee nor `auto-decomposer`, the tick promotes the card **as-is**
  (`specify_triage_task` with no title/body/assignee, so there is no LLM call and no body rewrite). Rationale: upstream
  has no machine-readable "commissioner" field; "not the card's own worker" is the generic, upstreamable form of
  "Auftraggeber". Narrowing: optional `kanban.loop_breaker_release_authors` (list, or comma-separated string).
  When it's set, only those authors release. When it's unset or empty, the generic rule applies. Why: an automation comment
  (`factory-lead` parking note on t_a096323e, 17:30) must not wake the card. It's read next to `kanban.auto_decompose`
  in `gateway/kanban_watchers_common.py::_resolve_auto_decompose_settings`, live on every tick.
- **D4 Release by unblock.** `unblock_task` also accepts a loop-parked triage card and restores it like a blocked card
  (resume status from the event log, parent re-gate). Fresh triage cards are still refused. `block_recurrences`
  still survives.
- **D5 Docs.** The "Where an unblocked task lands" note in `kanban.md` says that the auto-decomposer never touches
  loop-breaker cards and how they leave triage.

## Marker definition (D1, made precise)

Status-entering events into `triage` on this base:

| Path | Event written | Meaning for the marker |
|---|---|---|
| `create_task(triage=True)` | `created` | fresh card |
| `block_task` -> `_route_block` loop | `block_loop_detected` | **loop-parked** |
| dashboard drag/PATCH to triage (`plugin_api._set_status_direct`) | `status` with `payload.status == "triage"` | human put it there, so it's fresh |
| board import (`kanban_transfer.py:271`) | `imported` with `payload.parked == true` | fresh (workspace re-pointing) |

Rule: walk the task's events of kinds `created`, `block_loop_detected`, `status`, `imported`, newest first (by `id`).
The first one that *entered triage* decides. `block_loop_detected` means parked, and any other entering event means fresh.
Events of those kinds that did not enter triage (`status` to another column, `imported` with `parked: false`) are
skipped. A parked card that's exported and imported keeps its marker (its `imported` event has `parked: false`).
A card that leaves triage and later loops again gets a newer `block_loop_detected`, so it's parked again. Only
comments newer than *that* event can release it (D3).

I rejected a schema column (`tasks.parked_by_loop_breaker`). It needs a migration and every exit path (specify,
decompose, drag, unblock, archive) would have to clear it. The event log already answers the question, and it
can't go stale.

## Components

1. **New sibling `hermes_cli/kanban_db_loop_breaker.py`** (topic: the unblock-loop breaker). It holds:
   - `route_block(...)`, moved verbatim from `kanban_db._route_block`. The move offsets the growth in `kanban_db.py`
     (4231 lines; the size ratchet allows no growth there) and puts the loop breaker's routing next to its marker.
   - `AUTO_DECOMPOSER_AUTHOR = "auto-decomposer"`.
   - `is_loop_parked(conn, task_id) -> bool` (D1).
   - `loop_parked_ids(conn, task_ids) -> set[str]` (D2 filter).
   - `releasing_comment_author(conn, task_id, *, release_authors) -> Optional[str]` (D3 predicate).
   - `release_commented(conn, *, release_authors) -> list[str]` (D3 action: `specify_triage_task` as-is).
   The facade (`kanban_db`) keeps `BLOCK_RECURRENCE_LIMIT` (tests and docs reference `kb.BLOCK_RECURRENCE_LIMIT`).
   The sibling late-imports facade names inside functions, so there's no module-level cycle.
2. **`kanban_db.unblock_task`** (D4): it accepts `triage` only when `is_loop_parked`, the resume status comes from the
   events as for `blocked`, and the guarded UPDATE adds `'triage'`.
3. **`kanban_decompose.list_triage_ids` and `kanban_specify.list_triage_ids`** (D2) drop loop-parked ids.
   `kanban_decompose.release_loop_parked(*, release_authors)` is a thin connect-and-call wrapper for the tick.
4. **`gateway/kanban_watchers_common._resolve_auto_decompose_settings`** returns
   `(enabled, per_tick, release_authors)`. On a config error it returns `(False, 3, ())`.
5. **`gateway/kanban_watchers_dispatcher.auto_decompose_tick(per_tick, release_authors=())`**: per board, it releases
   commented parked cards first (no LLM, so they don't count against `per_tick`), then decomposes from the filtered list.
   The caller in `gateway/kanban_watchers.py:304-307` passes the third value.
6. **Config** `kanban.loop_breaker_release_authors: None` in `hermes_cli/config_defaults.py` with a docs row.
7. **Docs** `kanban.md` note + config table, `reference/cli-commands.md` rows for `unblock` and `specify`/`decompose --all`.

## Data flow after the fix

```
block (rec>=2) --block_loop_detected--> triage [parked]
   tick: list_triage_ids()  -> parked id filtered out      (no LLM, no promotion)
   tick: release_loop_parked -> foreign comment newer than parking event?
            yes -> specify_triage_task(as-is) -> todo -> ready (body unchanged)
            no  -> stays parked
   human: hermes kanban unblock <id> / kanban_unblock      -> ready|todo|review (D4)
   human: hermes kanban specify|decompose <id>, dashboard -> unchanged behaviour (D2)
```

## Error handling

- Release and filtering run inside the tick's existing per-board `try`. A DB error on one board is logged at DEBUG
  (same as `list_triage_ids` today) and the next board runs.
- `specify_triage_task` already guards `status = 'triage'` in its UPDATE. A race with a human specify or unblock just
  returns False and isn't counted.
- Malformed `commented` payloads (no author) are skipped, never treated as a release.

## Residual risks (documented, not fixed here)

- A worker comment authored with the generic fallback `"worker"` (`tools/kanban_tools.py:150`, only when no profile
  name resolves) is "not the assignee" and would release under the generic rule. Dispatcher-spawned workers pin
  `HERMES_PROFILE`, so this shouldn't occur. Setting `loop_breaker_release_authors` closes it too.
- With `kanban.auto_decompose: false` nothing auto-releases on comment: the card waits for an unblock or an explicit
  specify. That was already true before (nothing touched triage), so the manual mode loses nothing.

## Testing (red first)

The tests extend existing files only: `tests/hermes_cli/test_kanban_decompose.py`,
`tests/hermes_cli/test_kanban_block_kinds.py`, `tests/hermes_cli/test_kanban_specify_db.py`,
`tests/gateway/test_kanban_auto_decompose_live.py`, `tests/gateway/test_kanban_auto_decompose_secret_scope.py`
(fakes gain `release_loop_parked`). Covered: characterization of the loop through the real tick with a stubbed aux
LLM (red on base); D2 sweep skip plus explicit single-id still works; D3 worker comment does not release, foreign comment
releases as-is with body unchanged, `release_authors` narrows; D4 unblock from loop-parked works and fresh triage is refused;
regression: a fresh triage card is still decomposed; settings parsing of the new key.
