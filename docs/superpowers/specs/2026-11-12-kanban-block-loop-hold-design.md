# Kanban: hold block-loop triage cards until someone replies

Card: t_a1deee87 (design), finding from t_4a3d4f17. Status as of 2026-11-12, base `origin/main` 72c880370ff2.

## 1. Problem (measured)

`_route_block()` (`hermes_cli/kanban_db.py`, `BLOCK_RECURRENCE_LIMIT = 2`) sends a card that blocks twice in a row
for the same kind to status `triage` and writes a `block_loop_detected` event. `triage` is also the status of a fresh
raw idea. The gateway's `auto_decompose_tick()` (`gateway/kanban_watchers_dispatcher.py`) lists every triage id via
`kanban_decompose.list_triage_ids()` (plain `status='triage'` query) and runs `decompose_task()` on it. That calls the
aux LLM, rewrites title/body and promotes the card to `todo` -> `ready`. A worker then picks it up, finds the question
still unanswered and blocks again.

Evidence, board `hermes`, card t_72559b11 (`task_events`, 2026-11-12 local time):

| block_loop_detected | specified (auto-decomposer) | changed_fields |
|---|---|---|
| 13:36:46 | 13:38:13 | title, body |
| 13:40:08 | 13:40:56 | title, body |
| 13:42:12 | 13:43:44 | title, body |
| 13:45:36 | 13:46:21 | body |
| 13:46:55 | 13:47:58 | title, body |
| 13:49:38 | 13:50:35 | body |
| 13:52:14 | 13:53:14 | body |

No comment from anyone other than the blocking worker (`ops`) arrived in that window. Seven LLM rewrites of a
finished rollout card, seven worker runs, zero human decisions. Two harms, not one:

1. the loop breaker has no effect (the card cycles without a human);
2. the card's original, carefully planned body was overwritten seven times by an LLM. Nothing keeps the old body.

Scale (all boards, same day): `block_loop_detected` occurred 179 times on 56 cards (hermes 16/7, kapsel 74/13,
cothinker 55/22, wissen 15/5, wing 12/4, hermes-dev 7/5).

## 2. Decision

Neither A nor B as written on the card. **A, sharpened, plus "never rewrite" instead of B.**

- **A (hold):** a card whose current stay in `triage` started with `block_loop_detected` is *held*: no automatic
  decompose/specify, no aux LLM call, no promotion, until a reply arrives.
- **Release without rewrite (replaces B):** once a reply has arrived, the gateway tick promotes the card
  `triage -> todo` **as is** (title, body, assignee unchanged). The answer lives in the comment thread, which every
  worker already receives in its `worker_context`. No LLM is involved.
- **B rejected.** B asks the LLM to carry the open question into the rewritten body. That is a prompt rule with no
  deterministic guarantee, and it keeps the second harm (body rewrite) alive. A block-loop card is not a raw idea;
  its body is already a spec. Not rewriting it makes B unnecessary: nothing can overwrite the question because
  nothing is overwritten. The question itself stays visible in the `block_loop_detected` event payload (`reason`) and
  in the notifier message, as today.

## 3. Definitions (binding for the implementation)

Order events by `task_events.id` (autoincrement), never by `created_at`: a comment and an unblock written in the same
second share a timestamp (t_72559b11: `commented` and `unblocked` both at 12:46:54).

Let `L` = the newest `block_loop_detected` event of the task.

`block_loop_hold_state(conn, task_id) -> str`, one of:

- `"none"` - the task is not in `triage`; or it has no `block_loop_detected` event; or a `specified` or `promoted`
  event exists with `id > L` (the card already left triage after that loop, so its current triage stay has another
  cause, e.g. a human dragged it back).
- `"awaiting_reply"` - in `triage`, `L` exists, and no *reply* after `L`.
- `"replied"` - in `triage`, `L` exists, and at least one *reply* after `L`.

A *reply* is a `commented` event with `id > L` whose payload `author`, stripped and lower-cased, is
**neither** the task's current `assignee` (the profile that blocked) **nor** `auto-decomposer`.

Why "anyone but the blocker" and not "the creator", as the card proposed: on t_72559b11 the creator is `architekt`,
but the decisions came from `birk` (G1-G4 comments, 12:43-12:46) and later from `architekt` on another card. A
creator-only rule would ignore Birk's own answers. The blocker cannot answer its own question; anyone else who
comments has taken a position on it. If the reply does not resolve the question, the worker blocks again with the same
kind; `block_recurrences` is not reset by triage -> todo (only `complete_task` resets it), so the very next identical
block lands in `triage` again and is held again. Each further cycle therefore costs exactly one human (or other
profile's) comment, never an LLM loop.

## 4. Behaviour by entry point

| Entry point | `none` | `awaiting_reply` | `replied` |
|---|---|---|---|
| gateway `auto_decompose_tick` | decompose (as today) | skip: no aux call, card unchanged, debug log | promote as is (`triage -> todo`, no aux call), audit comment |
| `kanban_decompose.list_triage_ids()` / `kanban_specify.list_triage_ids()` (used by CLI `--all` sweeps and the tick) | listed | **not listed** | listed |
| `decompose_task(id)` / `specify_task(id)` called directly | as today | refuse `ok=False`, reason starts with `held:` - unless `force=True` | as today (an explicit call is a human choosing a rewrite) |
| CLI `hermes kanban decompose <id>` / `specify <id>` (single id) | as today | passes `force=True`: a human naming one card has decided | as today |
| dashboard decompose endpoint (`plugins/kanban/dashboard/plugin_api.py`) | as today | as today (explicit human click; out of scope) | as today |

Audit comment on release (author `auto-decomposer`): `Released from block-loop hold after reply by <author>; title and
body unchanged, promoted to todo.` Its `commented` event is excluded from the reply rule by author.

Promotion uses the existing `kb.specify_triage_task(conn, task_id, author=...)` with `title=None, body=None,
assignee=None`: it already flips `triage -> todo` in one transaction, writes a `specified` event with no
`changed_fields`, and calls `recompute_ready()`. No new status, no new column, no schema migration.

## 5. Files

- `hermes_cli/kanban_db.py` - new `block_loop_hold_state()` next to `_route_block()`; constants for the three states.
- `hermes_cli/kanban_decompose.py` - `list_triage_ids()` filters `awaiting_reply`; `decompose_task(..., force=False)`
  guard.
- `hermes_cli/kanban_specify.py` - same for `list_triage_ids()` and `specify_task(..., force=False)`.
- `gateway/kanban_watchers_dispatcher.py` - `auto_decompose_tick` / `_decompose_one`: branch on the state; release
  path for `replied`.
- `hermes_cli/kanban.py` - single-id `decompose`/`specify` pass `force=True` (only if the call shape needs it).
- Tests: `tests/hermes_cli/test_kanban_block_kinds.py` (state function), `tests/hermes_cli/test_kanban_decompose.py`
  and `tests/hermes_cli/test_kanban_specify.py` (guards, sweep filter), `tests/gateway/test_kanban_auto_decompose_live.py`
  (tick: held, released, normal triage still decomposed).

## 6. Acceptance (checkable without asking)

1. A card driven into `triage` by two identical `needs_input` blocks, with no other comment, survives
   `auto_decompose_tick()` unchanged: status `triage`, title and body byte-identical, the aux LLM stub is called
   0 times, no `specified` event.
2. After `add_comment(conn, id, "birk", "...")` the next tick moves it to `todo`/`ready`, title and body still
   byte-identical, the aux LLM stub still called 0 times, one `specified` event without `changed_fields`.
3. A comment by the card's own assignee after the loop does not release it.
4. A plain triage card (never blocked) is still decomposed by the tick (existing behaviour, regression guard).
5. After release, a third identical `needs_input` block returns the card straight to `triage` with a new
   `block_loop_detected`, and it is held again.
6. `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_kinds.py tests/hermes_cli/test_kanban_decompose.py
   tests/hermes_cli/test_kanban_specify.py tests/gateway/test_kanban_auto_decompose_live.py` is green; tests 1-3 and 5
   are red on `origin/main`.

## 7. Out of scope

- The B1 eval decision on t_72559b11 (Birk's).
- Restoring the seven overwritten bodies of t_72559b11 (no copy exists in the DB; card is already re-planned).
- Dashboard UI for held cards (a later card may add a "held" badge; the state function makes that cheap).
- Upstream PR (fork rule: PR only on Birk's word).

## 8. Fork notes

- Branch from `origin/main` as `feat/kanban-block-loop-hold`; add to `hermes-agent-features.txt` at merge.
- Hotspots shared with active feats: `hermes_cli/kanban_specify.py` (feat/kanban-specify-asis-actor-events adds
  `promote_as_is()` right above `list_triage_ids()`), `gateway/kanban_watchers_dispatcher.py`
  (feat/kanban-gateway-live-caps adds `refresh_caps()` above `tick_once`). Keep edits local to the functions named
  in §5 so `hermes-up` merges cleanly.
