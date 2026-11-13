# Measurements for t_95a7e463 (architekt, 2026-11-13, read-only probes)

Stand 2026-11-13 ~17:45 CET. Source: read-only sqlite probes over
`~/.hermes/kanban/boards/*/kanban.db` (mode=ro), code on origin/main 547739e4.

## 1. Event sequence of wing/t_a096323e (task_events, kind + payload.kind/recurrences)

```
13 14:57:52 blocked             needs_input rec=1 source=ready
13 15:23:52 unblocked           (Birk's ok comment + unblock)
13 16:07:20 block_loop_detected needs_input rec=2   -> status triage
13 16:09:22 specified  (auto-decomposer, changed title+body) -> todo -> ready -> worker spawned 16:09:24
13 16:51:53 block_loop_detected needs_input rec=3
13 16:52:40 specified
13 16:56:50 block_loop_detected needs_input rec=4
13 16:58:13 specified
... (pattern repeats every 1-2 min)
13 17:15:57 block_loop_detected needs_input rec=13
13 17:16:38 specified
13 17:17:44 scheduled (worker parked itself)
```
Final row: status=scheduled, block_kind=needs_input, block_recurrences=13.
Every `specified` comment is authored `auto-decomposer` ("Specified — updated body and promoted to todo.").
Between 16:52 and 17:16 there was NO comment by Birk/Robo (last one 16:48:04); only worker comments.

## 2. Same pattern on other boards (specified events after a block_loop_detected, >=3)

```
hermes    t_72559b11 specified 64  loop kinds needs_input:64   (auto-decomposer 64)
kapsel    t_a295b219 specified 36  needs_input:36              (auto-decomposer 36)
kapsel    t_b893e2be specified 23  capability:23               (auto-decomposer 11)
cothinker t_5232a12f specified 23  transient:23                (auto-decomposer 15)
hermes    t_885a00f0 specified 20  needs_input:20              (auto-decomposer 19)
wing      t_a096323e specified 12  needs_input:12              (auto-decomposer 12)
wissen    t_3b3814f3 specified 11  needs_input:11              (auto-decomposer 6)
wing      t_d1d49ff1 specified 7   needs_input:8               (auto-decomposer 6)
cothinker t_3a83f328 3 transient; cothinker t_be1d6e04 3 transient; kapsel t_b0e38b05 3; wing t_dc830669 3
```
So it is not specific to needs_input: every block kind that hits the loop breaker cycles.

## 3. Code path (origin/main 547739e4)

- `hermes_cli/kanban_db.py:115` `BLOCK_RECURRENCE_LIMIT = 2`
- `hermes_cli/kanban_db.py:3238-3240` `_route_block`: `recurrences >= BLOCK_RECURRENCE_LIMIT` -> `return "triage", "block_loop_detected", ...`
  (recurrences = prev+1 when same kind; `block_recurrences` survives unblock AND specify, so once >=2 EVERY later
  block of the same kind goes straight to triage again)
- `gateway/kanban_watchers_dispatcher.py:246` `auto_decompose_tick` -> line 270 `_decomp.list_triage_ids()` -> `_decompose_one` -> `decompose_task(tid, author="auto-decomposer")`
- `hermes_cli/kanban_decompose.py:339-343` `list_triage_ids`: returns EVERY task with status triage — no distinction between
  a fresh idea card and a card the loop breaker parked there.
- `hermes_cli/kanban_decompose.py` `_apply_single` -> `kb.specify_triage_task` (`hermes_cli/kanban_db.py:3732`): triage -> todo,
  writes the "Specified — ..." comment, then `recompute_ready` -> ready -> dispatcher spawns the worker on the next tick.
- Config on the live gateway profiles: `kanban.auto_decompose: true`, `auto_decompose_per_tick: 2` (every dispatcher tick ~60 s).
- Docs `website/docs/user-guide/features/kanban.md` ~line 421-431 promise: loop breaker routes to triage "for orchestration attention"
  and "stops sending it back ... where a cron would just keep unblocking it". The auto-decomposer IS such an automatic un-parker.

## 4. Existing release paths (origin/main)
- `unblock_task` (`hermes_cli/kanban_db.py:3557`) only accepts status IN ('blocked','scheduled') — it cannot release a triage card.
- Dashboard drag triage -> todo/ready uses `_set_status_direct` (plugins/kanban/dashboard/plugin_api.py `_drag_to`).
- CLI `hermes kanban specify <id> [--as-is]` is the documented human way out of triage.
- Comments: `add_comment` (`kanban_db.py` ~1701) appends a `commented` event with `{"author", "len"}`.
