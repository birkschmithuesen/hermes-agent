# Wing as a canonical create surface, and what Wing can rely on for profiles

Card t_35b15524. Base: `feat/wing-session-source` (1c0360ef1f3e). Every commit this spec plans must
cherry-pick cleanly onto that branch. It replaces the live quickfix (`session_sources: []`).

## 1. Problem, verified in the tree

`gateway.ready` advertises `session_sources: ["wing"]` (`tui_gateway/ws.py:649`,
`tui_gateway/entry.py` stdio frame). Wing 1.4.1 therefore sends `session.create {source: "wing"}`.
A WebSocket bound to a session authority (`ws.py:638`, ready flag `session_authority: true`)
dispatches that frame to `gateway/session_controls.py::AuthorityConnection.dispatch`, and two
separate checks refuse it:

1. **Wire contract, first.** `tui_gateway/contracts/canonical_sessions.py:23`
   `source: Literal['cli', 'tui', 'gui', 'acp', 'tool', 'oneshot']`. `dispatch` validates params
   against it (`session_controls.py:298-303`) and answers
   `4001 invalid_params data={reason: invalid_params, fields: ['source']}`. This is the exact
   refusal reproduced live, so the card's pointer to `SURFACES` alone is not enough.
2. **Policy, second.** `gateway/session_policy.py:426` `build_policy` refuses any label not in
   `SURFACES` (line 85) with `field=source detail="not a surface: 'wing'"`.

The legacy stdio/no-authority path (`tui_gateway/methods_session.py::_create_session`) takes any
`source` string, so it already works. Only the canonical path is broken.

## 2. Decision: accept `wing`, mapped to the desktop surface

The architect's choice, matching the feat's intent (`agent/session_source.py:17-20`: "everywhere
else it is the desktop chat surface"). It is possible without a large change. The tree shows what
the mapping does at each site:

| Site | Effect of `'wing': 'desktop'` in `SURFACES` | Action |
|---|---|---|
| `canonical_sessions.py:23` Literal | must list `wing` too, or (1) still refuses | add `'wing'` |
| `STORED_AS_LOCAL` (`session_policy.py:89`) | gains `wing`: the row is created with `source='wing'` (`hermes_state_local.py:66-72`), peer refresh rewrites it to `local` like `gui` | none: Wing chats list like Desktop chats |
| `created_surface` (`:94-103`) | `wing` → `desktop`: `created_source='desktop'`, so `--source desktop` and the Desktop pickers include Wing chats | none |
| `SURFACE_GROUPS` (`:109-110`) | **bug**: groups `{gui, desktop}` and `{wing, desktop}` both write key `desktop`, so the last one wins depending on frozenset order. `hermes_cli/gateway_session_store.py:421-428` then drops either gui or wing rows from `--source desktop` | regroup by agent surface: `desktop → {desktop, gui, wing}` |
| `restore_policy` (`:985-987`) | invariant `platform == SURFACES[source]` holds (`desktop`) | none |
| peer refresh (`session_policy_refresh.py:142-150`) | `wing in SURFACES`: follows the profile like `gui` | none |
| `runtime.describe` `session_create.sources` (`session_controls.py:523`) | lists `wing` automatically | none |
| **Agent platform** (`run_turn_runner.py:1239,1303-1304` → `session_execution.py:173` `AIAgent(platform=…)`; `/bg` side agent `session_tui_side.py:110`) | `policy.platform` = `desktop`, so `agent.platform == 'desktop'` and the Wing hint (`agent/system_prompt.py:451-455`, keyed only on `agent.platform`) is **lost** | new seam `LocalSessionPolicy.agent_platform`: `wing` for a `wing` policy, else `platform`. Display, streaming and tool-drop settings keep reading `policy.platform` (`desktop`) |
| Desktop parity: sites that compare the policy or row label with `gui` | `session_policy.py:687` (surface toolsets), `gateway/local_viewer_events.py:61` (show_reasoning default), `gateway/run_housekeeping_disk.py:93` (prune notice), `gateway/session_queued_controls.py:30` (queued steer/replace), `gateway/session_worker_delegation.py:57` (async delegation) | add `WING_SOURCE` next to `gui` |
| Desktop-only client specifics | `session_policy.py:673,695` (`desktop_ui`/`catalog` toolsets), `:1039` and `session_local.py:280` (local-Desktop marks) | **keep `gui`-only**: Wing gets `{project}` like the legacy `_gui_surface_toolsets` (`tui_gateway/agent_factory.py:329`) |

The "stored label `wing`" that the platform hint needs, in the canonical path, is the frozen
policy (`policy.source == 'wing'`, persisted in the receipt and restored on every turn), not the
row's `source` column. The row column becomes `local`/`desktop` like any Desktop chat.

**Known limit (not this card):** in the canonical path the agent platform is frozen per session
(policy). The feat's surface switch (Mac session opened in Wing → Wing hint as a turn note,
`agent/surface_switch.py`) needs a per-turn client platform that canonical `prompt.submit` does not
carry. A Wing-created session keeps the Wing hint when opened on the Mac, and vice versa.

## 3. Advertise/accept consistency

`session_sources` stays `["wing"]`. A contract test reads the real ready frame (`ws.handle_ws`)
and sends `session.create` through `AuthorityConnection.dispatch` for every advertised label. Each
must answer `result`. A second invariant ties the canonical Literal to `SURFACES` (two data sets
that must agree, not a change-detector).

Mutant (acceptance): delete `'wing': 'desktop'` from `SURFACES` → `build_policy` refuses →
`test_wing_create_succeeds_and_runs_as_desktop`, the ready/create contract test and the
Literal==SURFACES invariant go red.

## 4. Part 3: profiles Wing asks for, and the `subagent` 4001s

### 4.1 `session.list` 4001 for default / coder / reviewer

- Wing learns these names from `profiles.list`: `gateway/session_group_controls.py:449-458` lists
  **every** profile under the root as a metadata-only row (RT-38, on purpose: Desktop needs the
  other bots' rows). Only the served row (`:398-416`) and siblings this socket reaches (`:437-444`)
  are addressable. Nothing in a row says which kind it is (`ProfileRow`,
  `tui_gateway/contracts/profiles_vault_complete_foreign_subagents.py:151-168`).
- A frame with `profile: "<unserved>"` is refused in `AuthorityConnection._sibling_for`
  (`session_controls.py:148-174`), answered at `:280-287`:
  `{code: 4001, message: "profile_mismatch", data: {reason: "profile_mismatch"}}`. That happens on
  a profile-bound socket for any other name (`:156-157`) and on a host-scoped socket for a name
  no serving authority answers (`:174`). It is **final**.
- The one retryable variant: `{code: 4001, message: "runtime_draining", data: {reason:
  "runtime_draining", field: "profile", detail: "the profile is still starting"}}`
  (`:169-173`, hot-serve still building it).

### 4.2 `subagent.roster` 4001 for the served profile `birk`

- **There is no `subagent.roster` method in core** (no match in `gateway/`, `tui_gateway/`,
  `agent/`, `apps/shared/src`). An unknown method answers `-32601` with `data.reason:
  "unknown_method"` (`session_controls.py:326-332`), never 4001. So the 4001 frames come from the
  roster verb that exists, `subagent.list` (`gateway/session_ancillary.py:24-27`). "Roster" is
  Wing's own name for it (the AGENTS.md wording "the roster is read-only…").
- `session_ancillary.py::read` (`:226-250`) refuses with 4001 and `data = {reason}` only (no
  `field`/`detail`; `_refusal_data`, `session_controls.py:22-35`):
  - `invalid_params` (`:228-231`): a key other than `session_id`/`profile`, or an empty or
    non-string `session_id`;
  - `not_found` (`:234-235`): the id is not a session this authority holds live. "Cold adoption
    belongs to session.resume, never an ancillary poll": a poll does not restore a stored session
    the way `authorize` would (`session_authority.py:196-209`);
  - also `profile_mismatch` (`session_busy_controls.py:26-27`), `permission_denied`,
    `runtime_retired`, `stale_generation` (`session_ancillary.py:417-425`, a reused route) and
    `unsupported_projection` (`:428-434`, a managed worker).
- Why it fires for `birk`: Part 1. Every new chat's `session.create` failed, so Wing polls
  `subagent.list` for a session id this authority never created or resumed (or an empty one).
  That gives `not_found` / `invalid_params`, both final and both client state. **This is not a
  core bug.** Refusals without `field`/`detail` log at DEBUG only (`_log_refusal`,
  `session_controls.py:49-64`, on purpose: a retrying client must not flood the log), which is why
  the gateway log shows nothing. To confirm on the device, set `logging` to DEBUG for
  `gateway.session_controls` and look for `subagent.list refused for profile=birk: <reason>`.
- The core needs no change here. Part 1 removes the trigger, and the app must stop polling on a
  final reason (4.3).

### 4.3 Contract the Wing app can rely on (canonical socket, `session_authority: true`)

- **Refusal frame:** `error.code == 4001`, `error.message == error.data.reason`, `error.data.reason`
  always present; `data.field` / `data.detail` only when the check named them; `data.fields` (list
  of key paths) only for wire-contract violations (`session_controls.py:299-303`).
- **Final (never retry the same frame):** `profile_mismatch`, `not_found`, `invalid_params`,
  `permission_denied`, `unsupported_projection`, `runtime_retired`. `stale_generation` means
  "re-resume, then ask again".
- **Retryable:** `runtime_draining` (with `field: "profile"`: the profile is still starting) and
  `error.code == 5001` `storage_unavailable`.
- **Unknown verb:** `error.code == -32601`, `data.reason == "unknown_method"`.
- **Ready frame:** `session_authority: true` (canonical wire) and `session_sources` (labels
  `session.create` accepts beyond the defaults; after this card `["wing"]`, guaranteed by the §3
  test).
- **New (§5):** `profiles.list` rows carry `addressable: bool`. Only rows with
  `addressable: true` may be named in a `profile` param. List and poll only those.

## 5. Core addition: `addressable` on canonical `profiles.list` rows

Minimal, live, per connection. `profiles.list` is already polled every 5 s and follows hot-serve.
A `served_profiles` snapshot in `gateway.ready` would go stale on serve/unserve and would have to
repeat the host-scope rule. Instead:

- `CanonicalProfileRow.addressable: bool` (required on the canonical wire; `canonical_projections.py:40`).
- `_profiles` (`session_group_controls.py:390`): own row `True`. A sibling row is `True` iff
  `connection.host_scoped and bot.problem is None`, the same rule `_sibling_for` routes by (`:148-168`)
  and independent of `include_sessions`. Every metadata-only discovery (hosted-room homes, root,
  `profiles/*`) is `False`.
- Regenerate `apps/shared/src/gateway-contract.generated.ts` and `.openrpc.json`
  (`scripts/gen_gateway_contracts.py`), as the feat did for `session_sources`.
- Multiplexing stays out of scope.

## 6. Tests (all `scripts/run_tests.sh`, behaviour contracts)

- `tests/gateway/test_wing_create_surface.py` (new): canonical create with `source=wing` succeeds.
  The row lists in `session.list`, `created_source == 'desktop'`, the policy is `wing`/`desktop`,
  and the advertised ready labels are all accepted. Literal == `SURFACES`. `SURFACE_GROUPS` is
  consistent (every member of a group maps to that same group).
- `tests/gateway/test_wing_agent_platform.py` (new): a `wing` policy's turn builds its agent with
  `platform='wing'` while the stream consumer gets `desktop`. Toolsets and launch defaults behave
  like `gui` except the desktop-only client toolsets.
- `tests/gateway/test_profiles_addressable.py` (new): own row addressable, discovered rows not, a
  host-scoped sibling addressable, the same sibling on a profile-bound socket not.
- Existing: `tests/tui_gateway/test_wing_session_source.py`, `tests/agent/test_platform_hint_wing.py`,
  `tests/tui_gateway/contracts/test_generated.py`, `tests/gateway/test_local_source_labels.py`,
  `tests/gateway/test_canonical_wire_contract.py`.
