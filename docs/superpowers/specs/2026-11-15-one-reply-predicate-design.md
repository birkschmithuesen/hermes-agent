# One reply predicate for the block-loop hold and the loop breaker

Stand 2026-11-15. Card t_52540fe8 (architect decision; follow-up of t_3af61c7b, question 2 of run 2046).
Base: `wt/t_3af61c7b` @ d1adb1bdc85a. This branch is merged as `feat/kanban-block-loop-reply-reset`
(merge card t_f4080327). Evidence: the read-only probes in `2026-11-15-one-reply-predicate-probes/`,
run by the architect on 2026-11-15 against all live board DBs.

## Problem

Two features ask the same question: "did the commissioner answer this card?". They answer it in two places
that disagree.

- **BLH** (`hermes_cli/kanban_db_block_loop.py`, `_hold`, lines 37-63). Its reply rule: a `commented` event
  after the latest `block_loop_detected` whose author, compared with `_norm` (`strip().lower()`), is neither the
  assignee nor `auto-decomposer`. It ignores `kanban.loop_breaker_release_authors`. It does not look at the body.
  It reports the LATEST such author.
  Readers: `block_loop_hold_state` (`kanban_specify._held`/`_unheld_ids`, which back the `--all` sweeps and
  the no-`force` refusal) and `release_block_loop_hold` (gateway `_release_one`).
- **LB** (`hermes_cli/kanban_db_loop_breaker.py`). `_is_reply_author` (line 41) compares exact stripped names.
  It excludes the assignee and `AUTO_DECOMPOSER_AUTHOR`, and when release authors are set it admits only those.
  Two callers use it:
  - `releasing_comment_author` (line 249): the release path. It applies no body filter and reports the FIRST author.
  - `_reply_author` (line 97): the reset path from t_3af61c7b. It adds the CLI audit-note filter
    `_carries_reply_body`, then applies a SECOND, inline release-author check `a in release_authors` (line 115).

So "reply" is defined in three places, with two case rules and two body rules. The 2026-11-14 spec
(`2026-11-14-reply-resets-block-loop-design.md`, D2 scope note) deliberately left the release path without
the audit filter "because that would change t_95a7e463's feature, outside this card". This card is where that
gets settled.

## Measurements (probes in `2026-11-15-one-reply-predicate-probes/`; stored as `*.py.txt` so the repo linters skip them, run with `python3 <file>`)

All 8 non-empty board DBs plus `~/.hermes/kanban.db`, read-only (`mode=ro`).

1. `probe_authors.py.txt`: 8019 comments, 22 distinct authors.
   - Exactly ONE comment has a non-lowercase author: `Robo` (1×, board hermes, t_67213677). `robo` appears 40×.
   - No author has leading or trailing whitespace. All assignees are lowercase.
   - 0 comments have an author that equals the card's assignee only case-insensitively.
   - The 209 comments after a card's latest `block_loop_detected` include 0 where `_norm` and an exact
     comparison decide differently.
2. `probe_stays.py.txt`: 450 loop stays. A stay runs from a `block_loop_detected` to the first status-changing
   event. Each stay is judged by three rules: H = BLH today; LB = release path today, with the live
   `release_authors = birk`; U = this spec (exact or casefold gives the same result here, plus release
   authors, plus the audit filter).
   - 46 stays: all three say replied. 396 stays: all three say not replied.
   - 7 stays: only H says replied. The authors are worker, ops, factory-lead (3×) and coder. These are
     non-commissioner comments, so H is wrong because it ignores release authors.
   - 1 stay: H and LB say replied, U does not. This is hermes-dev t_f5c5403a, where `birk` wrote the CLI note
     `BLOCKED: Zwischenschritt Robo: aus triage zurueck…`. That is an annotation of a block, not an answer.
3. `probe_divergence.py.txt`: 74 cards ever loop-parked. 46 `SCHEDULED:`/`BLOCKED:`/`UNBLOCK:` notes written as
   `birk` after a loop. These come from Robo's park sweeps, which run as profile birk. BLH counts every one
   of them as a reply, and so does LB's release path whenever no real reply exists.
4. Source facts (read at d1adb1b):
   - Profile ids are lowercase-only (`hermes_cli/profiles.py:349-351`, `_PROFILE_NAME_RULE`).
   - The `kanban_comment` tool's author is the worker's persisted identity, never caller args
     (`tools/kanban_tools.py:961-970`).
   - `hermes kanban comment` is denied to delegated children (`hermes_cli/kanban.py:208-214`).
   - Mixed-case authors therefore come only from human free text: CLI `--author`
     (`hermes_cli/kanban.py:758`) and the dashboard `author` field (`plugins/kanban/dashboard/plugin_api.py:799-810`).
   - `add_comment` stores the stripped author in `task_comments` but the RAW author in the `commented` event
     payload (`hermes_cli/kanban_db.py:1693-1695`).
5. Existing test contracts:
   - `tests/hermes_cli/test_kanban_block_kinds.py:104-108` (BLH) requires that `" Worker "` is treated as the
     assignee `worker` ("case/space-insensitive").
   - No LB test uses a mixed-case author (grep of the 9 TESTCMD files at d1adb1b).
   - The union of both suites can therefore be satisfied only by a case-insensitive rule.

## Decisions

**D1: one predicate.** `kanban_db_loop_breaker` owns the single definition of a reply. A `commented` event
counts as a reply iff all four hold:
- (a) its author is not the assignee;
- (b) its author is not `AUTO_DECOMPOSER_AUTHOR`;
- (c) if `release_authors` is non-empty, its author is in it;
- (d) the comment behind it is not a CLI audit note. This is the existing `_carries_reply_body` with
  `CLI_AUDIT_PREFIXES` and the 1-second window.

All three call sites use this one predicate:
- the reset path (`_reply_author`);
- the release path (`releasing_comment_author`);
- BLH `_hold`.

These go away: `_norm`, BLH's local `not_replies` set, and the inline `a in release_authors` in
`_reply_author`. `RELEASE_AUTHOR` in `kanban_db_block_loop` becomes an alias of `AUTO_DECOMPOSER_AUTHOR`,
because it is also the audit author of the BLH release.
*Why:* this is one concern, so it gets one source of truth. Measurement 2 shows the two copies disagree on
8 of 450 stays, and the single rule is right in every one of those 8.

**D2: names compare case-insensitively.** Both sides go through `str(x or "").strip().casefold()`: the
author, the assignee, `AUTO_DECOMPOSER_AUTHOR`, and every release author. The stripped original spelling
is still what gets reported, logged, and used in the `task_comments` SQL match.
*Why:*
- (i) Measurement 1: case never decided anything in the live data (1 of 8019 comments, 0 of 209 post-loop
  comments).
- (ii) Profile ids are lowercase-only, so folding can never merge two distinct agent identities.
- (iii) The only mixed-case sources are human free text. An exact rule would silently drop `--author Birk`
  while the card stays parked.
- (iv) Measurement 5: the BLH test contract `" Worker "` == `worker` survives unchanged. With an exact rule
  it would have to be rewritten.
- This also changes LB: a `Birk` comment now counts for `release_authors=("birk",)`. No live comment is
  affected (measurement 1).

**D3: the audit-note filter applies on all three paths.** This includes the LB release path and BLH. It
revises the scope note of D2 in `2026-11-14-reply-resets-block-loop-design.md`.
*Why:* `hermes kanban block/schedule/unblock --reason` writes `PREFIX: reason` as the operator profile.
`_commented` writes the comment BEFORE the op runs, so a `schedule` that fails on a triage card still leaves
the note behind. Measurement 3 found 46 such notes written as `birk` on loop-parked cards. Measurement 2
found that one of them (t_f5c5403a) is the only reason that stay counts as replied.

**D4: where `_hold` gets the release authors.** `_hold(conn, task_id, *, release_authors=None)`:
- An explicit tuple is used as given. The gateway `_release_one` passes the tick's tuple, the same one
  `_release_loop_parked` gets, so both release paths in one tick read the same config.
- `None` reads the calling profile's config through the existing `_configured_release_authors()`. This is
  what `kanban_specify._held` / `_unheld_ids` (CLI, library) do. The config is read only once a candidate has
  passed (a), (b) and (d), as in D3 of the 2026-11-14 spec.
- If the config cannot be read, there is no reply and the state stays `awaiting_reply` (fail closed). The
  sweeps then skip the card, and a human can still name it with `force`.
`block_loop_hold_state` and `release_block_loop_hold` grow the same keyword and pass it through.

**D5: which reply author is reported.** The FIRST qualifying reply after the anchor event, on all paths.
BLH used to report the latest. Only the text of the BLH audit comment ("after reply by X") depends on this.
If an existing test asserts the latest author when there are several replies, raise a `FRAGE:`. Do not
change that test silently.

**D6: no other behaviour changes.** These stay as they are: `_EXIT_KINDS`, `is_loop_parked`, the composed
`list_triage_ids` (held OR loop-parked are dropped, D2 answer of t_30c69e47), the reset rule (D1/D4/D9 of
the 2026-11-14 spec), and `force` semantics.

## Non-goals

- The two "is this card held/parked" definitions (`_hold`'s `_EXIT_KINDS` vs `_parking_event_id`) remain
  separate. t_30c69e47 Q1 decided that on purpose. They are not touched here.
- Adding a `comment_id` to `commented` payloads was already rejected in the 2026-11-14 spec D2.
- No config migration is needed. `loop_breaker_release_authors` is set only in `~/.hermes/config.yaml:309`
  and `profiles/birk/config.yaml:319`, both as `birk` in lowercase.

## Acceptance (for the build chain)

1. TESTCMD (the 9-file command of the 2026-11-14 plan) reports `0 failed`. Every existing assertion stays
   unchanged, except edits the plan names one by one with a reason (expected: none).
2. New tests are red on the base d1adb1b (detached scratch worktree) and green on the branch. They cover:
   - BLH stays held on a non-release-author comment;
   - BLH stays held on an audit note;
   - the release path ignores an audit note;
   - `Birk` counts for `release_authors=("birk",)` on all three paths;
   - `_hold` with an unreadable config stays `awaiting_reply`;
   - the gateway `_release_one` honours the tick's release authors.
3. Mutants, each with named red tests:
   - drop the casefold;
   - drop the audit filter on the release path;
   - make `_hold` ignore release authors.
4. `grep -n "_norm\|not_replies" hermes_cli/kanban_db_block_loop.py` prints nothing.
   `grep -n "in release_authors" hermes_cli/kanban_db_loop_breaker.py` shows hits only inside the predicate.
5. `python scripts/check` reports no finding beyond the 2 pre-existing ones on the base
   (`gateway/kanban_watchers_dispatcher.py:297 BLE001`, `tests/hermes_cli/test_kanban_db.py FILE_LINES`).
