# Managed Kanban workers die under host load: worker receipt budget shorter than the owner's own patience

Card: hermes/t_6740e5cd (absorbs hermes-dev/t_5cc88209). Status: diagnosis final, decisions D1-D6 final.
Base: origin/main @ 18e659dfc93e. The live gateway runs birk-local @ effc80b2d552, where line numbers differ. Both are given where they differ.
Measurement date: 2026-11-15 15:00-15:45 CET. Probes are read-only, and their scripts and outputs are in `2026-11-15-worker-receipt-budget-probes/`.

## 1. Symptom

On 2026-11-15, four crash waves hit 28 Kanban runs on boards hermes and cothinker:

| Wave | Crashes |
|---|---|
| 12:47-12:49 | 5 |
| 13:18-13:20 | 12 |
| 13:29-13:31 | 11 |

Every worker's last output was `Kanban owner refused: Kanban execution unknown; retained by owner`. The gateway (PID 3436273, up since 10:59) did not restart. Since 2026-11-08 there have been 118 such crashes on 5 boards (`probe_unknown.out`).

## 2. The card premise is refuted: admission did not fail

The card assumed that the owner's admission stays `unknown` under load (board DB busy, submitter-liveness watcher). That is not what happened.

- **All 28 crashed runs of 2026-11-15 carry an `owner_admitted` event.** They ran for 2.5 to 138 min before they died, 729 run-minutes in total (`probe_admitted.out`).
- `kanban_worker_client.py` (origin/main :130-131, live :149-150) only REPORTS `unknown`. The receipt turned `unknown` because the owner settled the admission as lost after its managed child died: `gateway/session_managed_worker.py` (origin/main :793-801, live :748-755) publishes `Worker execution is unknown.` and raises `ManagedExecutionUnknown`. The admission row in each profile's `state.db` reads `status=terminal, outcome=interrupted` (`probe_late.out`).
- The admission budget (`admission_answer_budget`, `kanban_worker_client.py:29`) is not involved. That wait had ended long before.

## 3. Root cause (measured)

The managed worker child (`agent/managed_worker.py`) sends each durable write to its owner as a `worker.persist` RPC and waits a **hard-coded 20 s** for the receipt:

- `agent/runtime_session_store.py:197`: `response = link.receive(20)`
- `agent/runtime_session_store.py:112`: on expiry, `raise TimeoutError('worker_receipt_timeout')`
- `agent/runtime_session_store.py:346-348`: `retry_pending` stores it as the **sticky** `self.failure`. Every later `_apply` and the final `flush_token_counts` (`:407-413`) re-raise it.
- `agent/managed_worker.py` (origin/main :494, live :417) calls `store.flush_token_counts()` at the end of the turn. It raises, the child logs `managed worker failed` and exits with code 1, and the owner books the turn as lost (section 2).

The owner answers the same RPC with a write that may wait **60 s** on the profile's `state.db` write lock:

- `hermes_state_runtime.py` (origin/main :1073, live :1005): `apply_worker_receipt` runs `db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)`
- `hermes_state.py` (origin/main :500, live :488): `_TRANSCRIPT_WRITE_PATIENCE_S = 60.0`

On top of that, the owner's event loop stalled for more than 10 s in exactly the crash windows. `tui_gateway/ws.py:213` logged `ws write slow (loop stalled >10.0s)` at 12:47:53, 13:18:47, 13:18:57, 13:29:01 and 13:29:12. The host had load ~55 on 16 cores, swap 7/15 GB, and `memory pressure is elevated` at 13:17:22.

So the child gives up after 20 s, while the owner is still allowed 60 s plus any loop stall to answer. The same mismatch was already fixed once for admission: `kanban_worker_client.py:126-132` (live) explains why the 30 s default there was replaced by `admission_answer_budget()`. The persist path still has the old fixed short budget.

### Evidence

| # | Measurement | Result | File |
|---|---|---|---|
| E1 | Child exception per crash, from the assignee profile's `logs/managed-worker.log` and the bound pid | 2026-11-15: 26x `WorkerPersistenceError: worker_receipt_timeout` and 2x `TimeoutError: timed out`. The 2x come from the connect path at turn start, `_connection`: `query_identify` and `_session_ticket` with a 5 s timeout (`runtime_session_store.py:143-159`). Since 2026-11-08: 74x `worker_receipt_timeout`, 39x `kanban_skills_unavailable` (a different, already known cause), 5 other. | `probe_causes.out` |
| E2 | Did the owner commit the request the child gave up on? (`worker-outboxes/<exec>/pending.json` `pending[0]` vs. owner `worker_receipts` row for that sequence) | **26 of 26 COMMITTED-LATE, 0 lost.** 2 had no pending entry (the connect-path cases). | `probe_late.out` |
| E3 | Send time (pending.json mtime) vs. the child's `worker_result` | 21.7 s to 63.0 s. Always above 20 s, which fits a 20 s timeout plus unwinding. | `probe_late.out` |
| E4 | Next row the owner wrote into the same session after the timed-out `transcript.append` (6 cases) | 41.8 s to 45.8 s after the send | `probe_commit_time.out` |
| E5 | Admission | 28/28 `owner_admitted` | `probe_admitted.out` |
| E6 | Owner loop stalls | each crash wave's sends (12:47:14-24, 13:18:35-47, 13:28:44-13:29:06) fall into a logged >10 s stall | gateway `agent.log` lines 22128, 22186-22187, 22208-22209 |
| E7 | `database is locked` / write-lock-holder warnings 12:00-14:00 | none | gateway `agent.log`, `errors.log` |

ANNAHME (verify if it matters): `worker_receipts` has no timestamp column, so the exact commit delay of the timed-out receipt is not recorded. E4 only bounds when the owner was writing again in that session (about 42-46 s after the send). Whether the delay was spent in the stalled loop or waiting for the SQLite lock (under 60 s, see E7) is not separated. The fix below does not depend on which one it was.

### Why the dispatcher then auto-blocked the cards

`hermes_cli/kanban_db_dispatch.py` (origin/main :1304/:1313, live :1317/:1326) treats 3 or more crashes with the same fingerprint in one tick as systemic, with `failure_limit=1`. The waves of 4 and 3 tripped this, so the cards went straight to `blocked` (`gave_up failures=1`). That is the dispatcher working as designed. It is not a second bug.

## 4. Options considered (one recommended)

| Option | Effect on this failure | Cost | Verdict |
|---|---|---|---|
| **R: The worker's owner-RPC budgets cover the owner's own patience** (D1-D6) | Child no longer gives up while the owner is still allowed to answer. All 26 measured requests were committed after 42-46 s (E2/E4), well inside a 120 s budget. | About 30 lines in one file plus tests (S/M). A silently hung owner (socket open, no answer) is detected after 120 s instead of 20 s. A closed socket is still detected at once, because the reader thread forwards the close (`runtime_session_store.py:101-102`). | **Recommended** |
| Treat `unknown` as retryable in `kanban_worker_client.py` | None. The child is already dead, so a retry is a fresh run. The turn's work is lost (729 run-minutes on 2026-11-15). The owner keeps `unknown` on purpose to pause followers (`session_managed_worker.py`, "deliberately pauses every accepted follower"). | Small. Hides real losses as retries. | Rejected |
| Raise the admission budget | None. Admission succeeded 28/28 (E5). | — | Rejected (premise refuted) |
| Cap spawns tighter under load (`kanban.max_in_progress` 14, `max_in_progress_per_profile` 8 in `~/.hermes/config.yaml`) | Indirect: fewer workers means fewer stalls, if the stall comes from host load (unproven). | Throughput for every board, every hour, not only during the 3 bad minutes. | Not now. Revisit only if `worker_receipt_timeout` recurs after R. |

## 5. Decisions (binding for the plan)

- **D1 One constant, one source of truth.** Define `WORKER_RECEIPT_MARGIN_S = 60.0` and a function `worker_receipt_budget()` in `agent/runtime_session_store.py`. It returns `SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + WORKER_RECEIPT_MARGIN_S`, which is 120.0 s today, read from `hermes_state.SessionDB` and never duplicated as a literal. The budget follows the owner's patience automatically if upstream changes it. This mirrors `admission_answer_budget()` in `hermes_cli/kanban_worker_client.py`.
- **D2** `WorkerRPC.__call__` uses `link.receive(worker_receipt_budget())` instead of `link.receive(20)`. The rest of the semantics stay unchanged: never resend after a send, sticky failure, and redial on the next call. The owner deduplicates exact retries by (execution, sequence, digest) (`hermes_state_runtime.py` `apply_worker_receipt`), but adding a retry is out of scope.
- **D3 The connect path gets the same budget** (E1: 2 of 28 crashes): `discover_gateway_endpoint(..., timeout=5)`, `query_identify(..., timeout=5)`, `_session_ticket(...)` (which reaches `private_control_query`'s default `timeout=5`) and `connect(..., open_timeout=5)` in `WorkerRPC._connection` (`runtime_session_store.py:143-159`). Each of these calls moves to `worker_receipt_budget()`, through an explicit parameter. If `_session_ticket` has no timeout parameter, add a keyword-only `timeout` that is passed through to `private_control_query`, with a default equal to today's value so that other callers do not change. Do NOT change the shared `hermes_cli` defaults.
- **D4 No library keepalive.** The comment at `runtime_session_store.py:156-157` stays true: `ping_interval=None` stays. Update the comment and the docstring of `test_owner_stall_shorter_than_the_receipt_budget_does_not_fail_on_a_ping_deadline` (`tests/agent/test_worker_rpc_connection_reuse.py:221-224`, which says "20 s") to name the constant.
- **D5 Tests (RED first, on the unmodified base):**
  (a) An owner stall longer than 20 s but shorter than the budget returns the receipt. Patch the budget function or a module attribute down so the test does not take 20 s: for example, set the margin so the budget is 1.5 s, stall 1.0 s, and expect success; then stall 2.0 s and expect `worker_receipt_timeout`. The test must fail on the base, where the receipt budget is the literal 20 and cannot be patched. Prove that on the base it is RED through the missing symbol or the literal.
  (b) `worker_receipt_budget()` equals `SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + WORKER_RECEIPT_MARGIN_S`, and is at least `SessionDB._TRANSCRIPT_WRITE_PATIENCE_S + 1`. This is the invariant "the worker never gives up before the owner".
  (c) A connect-path test: `WorkerRPC._connection` passes the budget to identify, ticket and open. Use monkeypatch spies and assert on the received timeout values.
  Mutants that must turn a test red: M1 `receive(20)` restored; M2 margin set to 0 (the budget equals the patience, which violates `>= patience+1`); M3 the ticket query keeps its default 5 s.
- **D6 Delivery.** New feat branch `feat/worker-receipt-budget`. No existing feat touches `agent/runtime_session_store.py` or `agent/managed_worker.py`; the 32 features.txt branches were checked. The plan, the spec and the probes stay on the LOCAL card branch only. Never push: the probe outputs name private cards and profiles, and the fork is public. The [Merge] card pushes the feat branch WITHOUT `docs/superpowers/specs/2026-11-15-worker-receipt-budget-probes/`, with code and tests only, and adds the feat to `~/.hermes/hermes-agent-features.txt`. The live effect needs `hermes-up` plus a gateway restart, and that is Birk's step (hermes-core-fork-workflow).

## 6. Out of scope

- Why the gateway loop stalls for more than 10 s under load. The trigger is host load (cpu PSI some avg60 = 51.66 at 15:00), but the blocking code was not identified. If stalls ever exceed 120 s, R stops helping. That is the signal to open a loop-stall investigation card.
- The 39 `kanban_skills_unavailable` crashes before 2026-11-15. They have a different cause, already documented in factory-lessons `hermes-kanban-board.md`.
- Unblocking the cards that were auto-blocked today. That is an operator step: the cards were re-queued by hand already, per t_5cc88209.
