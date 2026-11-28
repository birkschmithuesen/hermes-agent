# Fabrik-Changelog (Kanban-Board `hermes-dev` + Claude-Code-Hülle)

**Zweck:** Spätere Sessions sehen hier in zwei Minuten, *was* an der Fabrik
optimiert wurde, *warum* (gemessener Anlass) und *wie sich das zeigt*. Neueste
oben. Ein Eintrag = eine Wirkung, nicht ein Commit.

**Pflicht:** Jede Änderung an `scripts/kanban-code-card.py`, `scripts/cc-run.py`,
`scripts/cc-wait.py`, `scripts/fabrik-*.py`, `scripts/kanban-*.py`, den
Rollen-Profilen (`docs/fabrik-profile-changes/`) oder den Board-Konfig-Werten
(`kanban.*` in config.yaml) bekommt hier einen Eintrag — im selben Commit.
Format (new entries in English, `docs/refactoring-guidelines.md` §7; older entries keep
their German keys `Anlass`/`Aenderung`/`Tell`):

```

## 2026-11-28 — kanban_db_dispatch.py: `active_pr` respawn guard held merge/closer cards whose only PR-URL comment was pre-staged before the first claim (t_e5ed82b4)

- **Reason:** measured on board `cothinker` 2026-11-28: 3 ready merger cards held 26+
  dispatcher ticks with event `respawn_guarded` reason `active_pr`, because a generator (or
  the coder of the card's parent) wrote "draft PR https://.../pull/N" onto the merge/closer
  card before it was ever claimed. The existing handoff exemption only lifts `active_pr` for
  an `assigned`/`changes_requested`/`review_reopened` event strictly AFTER the PR comment;
  a card's initial assignee is recorded by the `created` event, not an `assigned` event, so a
  same-profile no-op reassign is the only thing `_is_handoff_event` already excludes
  (#111910) — it has no way to recognize a comment that predates the card's first claim
  altogether. There is no "merge lane" in the dispatcher kernel to special-case: `lane` is
  only `"ready"`/`"review"`, derived from the board column, never from the assignee's role —
  `"merger"`/`"closer"`/`"coder"` are a pure naming convention from an external generator
  script, unknown to `check_respawn_guard`.
- **Change:** `hermes_cli/kanban_db_dispatch.py::check_respawn_guard` gained
  `_is_foreign_prestaged_pr_comment(conn, task_id, pr_rows, current_assignee)`: the
  `active_pr` guard now also lifts when the card has never had a `task_runs` row AND the
  newest PR-URL comment's author matches NEITHER the card's current assignee NOR its
  original assignee (from the `created` event payload). Matching either reference point
  still counts as the card's own worker, so a no-op reassign or an unassign does not turn
  that worker's own PR comment foreign — the existing same-profile-reassign/unassign
  protection (#111910) is unchanged.
- **Tell:** `scripts/run_tests.sh tests/hermes_cli/test_kanban_review_lifecycle.py
  tests/hermes_cli/test_kanban_review_lifecycle_complete.py tests/hermes_cli/test_kanban_db.py
  tests/hermes_cli/test_kanban_host_cap.py tests/hermes_cli/test_kanban_list_undecodable_text.py
  tests/hermes_cli/test_kanban_update_drain.py tests/hermes_cli/test_kanban_dispatch_claim_window.py`
  — new `test_active_pr_guard_lifts_for_prestaged_pr_comment_on_never_run_card` green (red
  before the fix: `active_pr`); mutant guard (removing the new exception) turns it red
  again; all 171 pre-existing tests across the seven files stay green (one unrelated
  pre-existing failure, `test_review_dispatch_preserves_task_skills_and_adds_reviewer_skill`,
  reproduces identically on unmodified `main` and is out of scope for this card).
- **Commits/cards:** t_e5ed82b4.
