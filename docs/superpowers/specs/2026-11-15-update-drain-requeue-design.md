# Update drain is a run-state event, not a card failure — design (card t_bb79440f)

Status: decided by the architect (no open Birk gate). Stand 2026-11-15, measured on the live boards
read-only. Probes and their raw output: `docs/superpowers/specs/2026-11-15-update-drain-probes/`
(`*.py.txt` = probe script, `*.out` = its output).

## 1. Problem (measured)

On 2026-11-15, two core updates (~17:05 and ~18:22–18:37 local) produced 40 worker crashes
whose output ends with an update-drain or mixed-version text. 31 of them became `gave_up`
(hermes: 20, cothinker: 8 drain + 3 ImportError; `probe2` counts). Every gave_up was
`failures: 1, effective_limit: 1, limit_source: dispatcher, sticky: true`.

Why they tripped at 1 (code, base `feat/kanban-review-crash-loop` @ c350c529a491, file
`hermes_cli/kanban_db_dispatch.py`):
- `_account_crashes` (l. 1314): `is_systemic = fp_counts[_error_fingerprint(error_text)] >= 3`
  (l. 1388) → `failure_limit=1` + `sticky` (l. 1397). The fingerprint is the first 80 chars of
  `pid N exited with code 1 Worker's last output: 'hermes: source-update completion ...` —
  identical for every worker killed by the same drain. So the "systemic wave" rule, meant for a
  broken card population, fired on a host event and made the blocks sticky (no auto-release by
  `recompute_ready`).

Two populations (`probe4.out`, claim time vs. crash time, claimer pid):
- **Spawned into the fence (29 of 40):** cothinker 17:08–17:21 (old gateway pid 3436273) and
  hermes 18:29:29–18:33:48 (NEW gateway pid 2557530, up since 18:26:18). The dispatcher claimed
  and spawned while `hermes update` still held the install; each worker waited
  `OWNER_WAIT_S = 180 s` (`hermes_cli/kanban_worker_client.py:24`) and died with
  `Kanban owner refused: Kanban owner silent for 18Xs: Gateway draining: update_paused`.
- **Running when the drain hit (11 of 40):** the 18:29:28 wave (runs started 16:19–18:22),
  exit 130, `owner admission settled interrupted`, same final text — or an ImportError (below).

The text is produced by our own code, never by the model:
- `hermes_cli/gateway_runtime.py:387` `_fenced_verdict` → `GatewayDiscovery("draining", reason_code="update_paused")`
- `hermes_cli/gateway_client.py:368` → `Gateway draining: update_paused`
- `hermes_cli/kanban_worker_client.py:97` → `Kanban owner silent for {n}s: ...`; `:189` prints `Kanban owner refused: {exc}`.

All-time shapes (`probe7.out`, `probe8.out`; 925 `crashed` events on all boards):
- 36 end with `Kanban owner refused: (Kanban owner silent for Ns: )?Gateway draining: update_paused`.
- 2 older ones (hermes-dev/kapsel, 21.10.) end with `... connection: Gateway draining: update_paused. Your message was not sent. ... Goodbye! ☤` (old one-shot worker, before the worker client). Not matched on purpose: that worker path no longer exists.
- 7 kapsel ones end with `Gateway inaccessible: service_identity_unverified` — a different class, out of scope.

Mixed-version ImportError (`probe8.out`): 4 of 925 crashes end with
`ImportError: cannot import name '<n>' from '<mod>' (<path>.py)`. All 4: `<path>` is under the
live install root `/mnt/HC_Volume_106183673/hermes/hermes-agent/`, the file's mtime is
`11-15 18:22:30` (written by the update), and the crashed run started BEFORE that mtime
(16:19, 16:22, 17:00, 18:22:21). That is a process that imported half an old and half a new
tree. Criterion is mechanical and read-only (one `stat`).

Fence probe (`probe_fence.py.txt`): `gateway_runtime._update_fenced({get_default_hermes_root()})`
answers in 48 ms on this host (False at 19:5x, no update running).

## 2. Decisions

D0 — **Branch base.** `feat/kanban-review-crash-loop` @ c350c529a491 (active in
`~/.hermes/hermes-agent-features.txt` l. 1002, in birk-local). It rewrites exactly the functions
this fix touches (`_classify_dead_worker`, `_account_crashes`, `_DeadWorker`). A sibling feat on
bare origin/main would collide at every `hermes-up`. New feat `feat/kanban-update-drain-requeue`
is stacked on it; its features.txt line goes directly BELOW `feat/kanban-review-crash-loop`.

D1 — **No new claims while the install is update-fenced** (the primary fix; it alone would have
prevented 29 of 40). In `_dispatch_once_locked`, after the reclaim phase and after
`_tick_spawn_budget`, before any lane row is claimed: if the fence is up, log ONE warning per
tick (`kanban dispatch: an update holds the install; spawning no new workers this tick (deferred,
not dropped)`) and return the result. Reclaim/promotion bookkeeping still runs (same contract as
the memory-pressure guard, l. ~2330). The fence check is a public wrapper in a NEW module
`hermes_cli/kanban_update_drain.py`: `install_update_fenced() -> bool` =
`gateway_runtime._update_fenced({get_default_hermes_root(), get_hermes_home()})`. Any exception
→ `False` plus a debug log (fail-open: a broken probe must not stop the factory; failure mode =
today's behaviour). Do NOT touch `describe_suppression` or the memory-pressure lines in
`_tick_spawn_budget` (sibling `feat/kanban-gateway-live-caps` edits them).

D2 — **Drain signal = the worker's own final output, anchored at its end.** In the new module:
`UPDATE_DRAIN_RE = r"Kanban owner refused: (?:Kanban owner silent for \d+s: )?Gateway draining: update_paused\s*$"`.
Matched against `dead.event_payload["worker_output"]` (the current spawn's segment only — that is
what `feat/kanban-review-crash-loop` guarantees). Not the fence state at reap time: by the time
the crash is reaped the fence can already be gone (18:33:41 reaps, fence released by ~18:37).

D3 — **Mixed-version ImportError counts as the same class** (card item 2: decidable cleanly, so
included). `MIXED_IMPORT_RE = r"ImportError: cannot import name '\w+' from '[\w.]+' \(([^()]+\.py)\)\s*$"`,
AND the captured path resolves inside the install root
(`Path(hermes_cli.__file__).resolve().parents[1]`), AND `stat(path).st_mtime > run started_at`.
A card's own worktree code never qualifies (path outside the install root). A genuinely broken
new tree is self-limiting: the respawn starts after the mtime, so its ImportError counts normally.

D4 — **Booking.** A dead worker matching D2 or D3 (and not already `rate_limited`, protocol
violation, terminal provider or preload failure) is rebooked like a quota wall: card goes back to
its source phase (`retry_status`: ready or review), claim cleared, `consecutive_failures`
UNCHANGED, `last_failure_error` stamped, run outcome `update_drain`, event kind `crashed` with
payload `update_drain: "drain" | "mixed_import"` (the watchers notifier keeps working; payload says
why). It goes to a new `_CrashSweep.update_drained` list, NOT into `crash_details` — so it never
reaches `_account_crashes` and never feeds the systemic-fingerprint count. New
`DispatchResult.update_drained: list[str]` field, inserted directly after `rate_limited`
(before `skipped_locked`; sibling feats edit after `memory_pressure`). `porter/card_states.py`
does not list `update_drain` in `FAILING`, so the Pforte neither counts nor routes it (checked in
chain B).

D4a — **Respawn guard.** `check_respawn_guard` (l. 1707) skips the `blocker_auth` regex only
when the latest run outcome is `crashed`, because a crash's error carries the worker's own output
(#117097). An `update_drain` run carries the same kind of output, so the exemption must cover it:
`latest_outcome not in ("crashed", "update_drain")`. Without this, a drained card whose output
happens to contain e.g. "auth" would be parked as `blocker_auth` forever. Run start for D3 is
`task_runs.started_at` of the run being closed (read before `_end_run`), not `tasks.started_at`.
The install root is a module-level helper `_install_root()` in `kanban_update_drain.py` so tests
can patch it.

D5 — **Bound.** Free requeues are capped by a streak: `_UPDATE_DRAIN_FREE_LIMIT = 3` consecutive
closed runs with outcome `update_drain` (walk `task_runs` newest-first like
`_protocol_violation_streak`; `rate_limited` runs neutral, anything else breaks the streak). The
4th consecutive drain death books as a normal crash (counts, may trip). Each such death costs ≥
180 s, so the cap bounds a stuck fence at ~12 min of wasted spawns per card, while D1 normally
prevents the spawns in the first place.

D6 — **Regression contract.** A plain crash (`pid N exited with code 1`, any other output) books
exactly as today: outcome `crashed`, `consecutive_failures + 1`, systemic rule unchanged. The
systemic rule itself stays; drained deaths just no longer feed it.

D7 — **Out of scope (named, not built):** `Gateway inaccessible: service_identity_unverified`
(kapsel 21.10., 7 crashes); the pre-worker-client one-shot text (2 crashes, path gone); the
gateway's own non-update drains (no measured crash). Changing `OWNER_WAIT_S`.

D8 — **Catalogue §4.13** (`docs/superpowers/specs/2026-10-19-porter-multi-human-design.md` in
the birk profile repo): the class is missing — the self-fix row lists crashed/timed_out/stale/
spawn_failed/rate_limited, nothing for update drains. Chain B adds one row: "update drain /
mixed-version crash" → self-fix by the core (requeue without counting, claims held while fenced);
never a human item; does not count toward the 3rd-failure error item. Same commit:
`docs/fabrik-changelog.md` entry (rule in that file's header) and a porter test that an
`update_drain` run produces no human item and does not raise the failure count.

D9 — **Source of truth per concern.** Behaviour: this spec §2. Test names in the plan point at
D-numbers. Standard tools only: pytest via `scripts/run_tests.sh`, the existing kanban test
fixtures (`tests/hermes_cli/test_kanban_preload_failure_block.py` style: `HERMES_HOME` tmp,
`HERMES_KANBAN_CRASH_GRACE_SECONDS=0`, `kb._pid_alive` patched, `_record_worker_exit`). No custom
infra.

## 3. Acceptance (card items, checkable)

1. Fake worker exit whose log ends with the drain text → task `ready` (or `review` for a review
   claim), `consecutive_failures` unchanged, run outcome `update_drain`, no `gave_up` event.
2. Three such exits in ONE sweep → none blocked (the systemic rule no longer sees them).
3. A plain crash still counts: `consecutive_failures + 1`, outcome `crashed` (regression).
4. Mixed-import exit with path inside a fake install root and mtime > run start → requeued free;
   same text with mtime < run start, or path outside the root → counts as a crash.
5. Streak: the 4th consecutive drain death counts.
6. Fence up (`install_update_fenced` patched True) → `dispatch_once` spawns nothing, ready rows
   stay ready, the warning is logged; fence down → spawns as before.
7. `git merge-tree --write-tree birk-local feat/kanban-update-drain-requeue` is clean (no
   collision with any active sibling feat).
