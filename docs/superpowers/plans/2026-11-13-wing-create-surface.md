# Wing Create Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `session.create {source: "wing"}` succeeds on an authority-bound socket and runs as the Desktop surface with the Wing platform hint. `profiles.list` tells Wing which profiles it may address.

**Architecture:** Add `wing` to the canonical create Literal and to `SURFACES` (→ `desktop`). Regroup `SURFACE_GROUPS` per agent surface. Give `LocalSessionPolicy` an `agent_platform` that stays `wing`, used only where the AIAgent is built. Add `WING_SOURCE` next to `gui` at the gateway sites that mean "a Desktop chat". Add a per-row `addressable` flag to canonical `profiles.list`. Spec: `docs/superpowers/specs/2026-11-13-wing-create-surface-design.md`.

**Tech Stack:** Python 3, pydantic contracts (`tui_gateway/contracts`), pytest via `scripts/run_tests.sh`, generated TS/OpenRPC (`scripts/gen_gateway_contracts.py`).

## Global Constraints

- Base branch `feat/wing-session-source`. Each task is ONE commit, cherry-pickable onto it in order. Tasks 1-3 are Part 1 and land together; Task 1 alone already fixes "Sending is unavailable" (the Wing hint then needs Task 2). Task 4 is Part 3 and independent.
- No merge, no push. One command per Bash call; no `cd`, `&&`, `;`, `$VAR`, `$( )`.
- Test command (create the TMPDIR first, once: `mkdir -p /home/birk/scratch/t_35b15524/tmp`):
  `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh <files>`
- Contract regeneration: `/home/birk/.hermes/hermes-agent/venv/bin/python scripts/gen_gateway_contracts.py` (verify with `--check`).
- English for code, comments and commits. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests are behaviour contracts; never read source text in a new test; new tests go in new files.
- Do not change `session_sources` in `tui_gateway/ws.py` / `tui_gateway/entry.py`: it stays `[WING_SOURCE]`.

---

### Task 1: Accept `wing` as a canonical create surface (Part 1, the fix)

**Files:**
- Modify: `tui_gateway/contracts/canonical_sessions.py:23`
- Modify: `gateway/session_policy.py:82-110` (`SURFACES` comment + entry, `SURFACE_GROUPS`)
- Regenerate: `apps/shared/src/gateway-contract.generated.ts`, `apps/shared/src/gateway-contract.openrpc.json`
- Create: `tests/gateway/test_wing_create_surface.py`

**Interfaces:**
- Produces: `SURFACES['wing'] == 'desktop'`. `'wing' in STORED_AS_LOCAL`. `created_surface('wing') == 'desktop'`. `SURFACE_GROUPS['desktop'] == SURFACE_GROUPS['gui'] == SURFACE_GROUPS['wing'] == frozenset({'desktop', 'gui', 'wing'})`.

- [ ] **Step 1: Write the failing tests** in `tests/gateway/test_wing_create_surface.py`

```python
"""Card t_35b15524: the label ``gateway.ready`` advertises in ``session_sources`` (``wing``) is a canonical
create surface. An authority-bound socket accepts ``session.create {source: "wing"}`` and runs it as the
Desktop chat surface (spec docs/superpowers/specs/2026-11-13-wing-create-surface-design.md)."""
import json
import typing
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection


def _runner(store):
    runner = SimpleNamespace(session_store=store, _session_db=store._db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _set_session_reasoning_override=lambda route, value: None,
                             _handle_message=None, _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    return runner


async def _owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    authority = await initialize_session_authority(_runner(store), profile_id='owned', instance_id='wing')
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda frame: None), {'user_id': 'owner'})
    return authority, store, connection


async def _create(connection, source, request_id, cwd):
    return await connection.dispatch({'id': request_id, 'method': 'session.create', 'params': {
        'request_id': request_id, 'source': source, 'cwd': str(cwd), 'model': 'frozen'}})


async def _ready_session_sources(monkeypatch):
    """``session_sources`` of the real WebSocket ``gateway.ready`` frame."""
    from tui_gateway import server
    from tui_gateway import ws as ws_mod
    sent = []
    monkeypatch.setattr(server, '_WS_ORPHAN_REAP_GRACE_S', 0)

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    await ws_mod.handle_ws(FakeWS())
    ready = sent[0]['params']
    assert ready['type'] == 'gateway.ready'
    return ready['payload'].get('session_sources') or []


@pytest.mark.asyncio
async def test_wing_create_succeeds_and_runs_as_desktop(tmp_path, monkeypatch):
    from gateway.session_policy import policy_for_source
    authority, store, connection = await _owner(tmp_path, monkeypatch)
    try:
        created = await _create(connection, 'wing', 'phone', tmp_path)
        assert 'result' in created, created
        sid = created['result']['stored_session_id']
        live = authority.sessions[sid]
        policy = policy_for_source(authority.runner, live.source)
        assert (policy.source, policy.platform) == ('wing', 'desktop')
        store._record_gateway_session_peer(sid, live.route, live.source)  # every turn refreshes the peer
        # Listed with the Desktop's chats: --source desktop and the Desktop pickers include it.
        assert authority.db.get_session(sid)['created_source'] == 'desktop'
        listed = await connection.dispatch({'id': 9, 'method': 'session.list', 'params': {}})
        assert sid in [row['id'] for row in listed['result']['sessions']], listed
    finally:
        await connection.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_every_advertised_session_source_is_a_canonical_create_surface(tmp_path, monkeypatch):
    advertised = await _ready_session_sources(monkeypatch)
    authority, store, connection = await _owner(tmp_path, monkeypatch)
    try:
        for n, label in enumerate(advertised):
            created = await _create(connection, label, f'advertised-{n}', tmp_path)
            assert 'result' in created, (label, created)
    finally:
        await connection.close()
        store.close_all_db_handles()


def test_canonical_create_contract_and_policy_accept_the_same_sources():
    from gateway.session_policy import SURFACES
    from tui_gateway.contracts.canonical_sessions import CanonicalCreateParams
    accepted = set(typing.get_args(CanonicalCreateParams.model_fields['source'].annotation))
    assert accepted == set(SURFACES)


def test_surface_groups_hold_every_desktop_label_in_one_group():
    from gateway.session_policy import SURFACE_GROUPS
    for name, group in SURFACE_GROUPS.items():
        assert all(SURFACE_GROUPS[member] == group for member in group), (name, group)
    assert SURFACE_GROUPS['desktop'] == {'desktop', 'gui', 'wing'}
```

If `session.list` needs a runner attribute the `SimpleNamespace` runner lacks, switch `_owner` to the `GatewayRunner.__new__` harness in `tests/gateway/test_local_source_labels.py::_authority` (same assertions).

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_create_surface.py`
Expected: 4 failures. Create: `error ... 'fields': ['source']`. Advertised: the same refusal for `wing`. Literal test: sets differ. Groups test: `KeyError: 'wing'` or the group assertion.

- [ ] **Step 3: Implement**

`tui_gateway/contracts/canonical_sessions.py:23`:

```python
    source: Literal['cli', 'tui', 'gui', 'wing', 'acp', 'tool', 'oneshot'] = 'cli'
```

`gateway/session_policy.py`, replace lines 82-85 (comment + `SURFACES`):

```python
# Creation label -> agent surface. ``tool`` (third-party integrations, ``hermes chat --source tool``) and
# ``oneshot`` (finite ``chat -q`` / ``-z`` runs) run as the CLI but keep their own stored label, so human
# pickers hide them (INTERNAL_LISTING_SOURCES). ``wing`` (the Wing phone client) is the Desktop chat
# surface; only its agent's platform hint differs (``LocalSessionPolicy.agent_platform``).
SURFACES = {'cli': 'cli', 'tui': 'tui', 'gui': 'desktop', 'wing': 'desktop', 'acp': 'acp', 'tool': 'cli',
            'oneshot': 'cli'}
```

Replace lines 106-110 (`SURFACE_GROUPS`):

```python
# Policy labels and their agent surface for one conversation, keyed by any of the names: ``--source gui``,
# ``--source wing`` and ``--source desktop`` are the same rows (both labels store created_source=desktop).
# One group per surface, so two labels on one surface never overwrite each other's group. ``tool`` and
# ``oneshot`` are not aliases of ``cli`` here: they keep their own stored label.
SURFACE_GROUPS = {name: group for group in (
    frozenset({surface, *(label for label in STORED_AS_LOCAL if SURFACES[label] == surface)})
    for surface in {SURFACES[label] for label in STORED_AS_LOCAL}) for name in group}
```

Then update the docstring in `hermes_cli/gateway_session_store.py:415-419`. Replace "gui and desktop are one conversation (the in-process agent is desktop, the managed worker creates with gui)" with "gui, wing and desktop are one conversation (the in-process agent is desktop, the managed worker creates with gui, Wing creates with wing)". Code unchanged.

Regenerate contracts:
Run: `/home/birk/.hermes/hermes-agent/venv/bin/python scripts/gen_gateway_contracts.py`
Expected: `apps/shared/src/gateway-contract.generated.ts` and `.openrpc.json` change only by adding `"wing"` to the canonical `session.create` source union/enum.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_create_surface.py tests/tui_gateway/test_wing_session_source.py tests/gateway/test_local_source_labels.py tests/gateway/test_session_row_cwd.py tests/tui_gateway/contracts/test_generated.py tests/gateway/test_canonical_wire_contract.py`
Expected: all pass.

- [ ] **Step 5: Mutant (acceptance check, not committed)**

Delete `'wing': 'desktop', ` from `SURFACES` and re-run `tests/gateway/test_wing_create_surface.py`.
Expected RED: `test_wing_create_succeeds_and_runs_as_desktop` (refused `field=source detail=not a surface: 'wing'`), `test_every_advertised_session_source_is_a_canonical_create_surface`, `test_canonical_create_contract_and_policy_accept_the_same_sources`. Re-add the entry by hand (Step 3 is not committed yet, so do not `git checkout` the file) and run again: green.

- [ ] **Step 6: Commit**

```
git add tui_gateway/contracts/canonical_sessions.py gateway/session_policy.py hermes_cli/gateway_session_store.py apps/shared/src/gateway-contract.generated.ts apps/shared/src/gateway-contract.openrpc.json tests/gateway/test_wing_create_surface.py
git commit -m "fix(gateway): accept session.create source=wing as the desktop surface

gateway.ready advertises session_sources=[wing], but the canonical create contract and
session_policy.SURFACES refused it (4001 invalid_params fields=['source']); Wing could not
start a chat. wing maps to the desktop surface like gui; SURFACE_GROUPS is grouped per
surface so gui and wing no longer overwrite each other's desktop group. A contract test
creates a session for every label the ready frame advertises.

Card t_35b15524.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The turn agent keeps platform `wing` (the Wing hint)

**Files:**
- Modify: `gateway/session_policy.py` (`LocalSessionPolicy`, add a property after `client_env_scope`, ~line 173)
- Modify: `gateway/run_turn_runner.py:1303-1305`
- Modify: `gateway/session_tui_side.py:110`
- Create: `tests/gateway/test_wing_agent_platform.py`

**Interfaces:**
- Consumes: `SURFACES['wing'] == 'desktop'` (Task 1).
- Produces: `LocalSessionPolicy.agent_platform -> str`: `'wing'` when `source == 'wing'`, else `platform`.

- [ ] **Step 1: Write the failing test** in `tests/gateway/test_wing_agent_platform.py`

```python
"""Card t_35b15524: a Wing session's agent runs as platform ``wing`` (its prompt carries the Wing hint,
tests/agent/test_platform_hint_wing.py) while every display/streaming setting reads the desktop surface."""
from types import SimpleNamespace

import pytest

LAUNCH = {'model': 'm', 'provider': 'custom', 'base_url': 'http://127.0.0.1:9/v1'}


class _Built(Exception):
    pass


@pytest.mark.parametrize(('source', 'agent', 'stream'), [('wing', 'wing', 'desktop'), ('gui', 'desktop', 'desktop')])
def test_the_turn_agent_platform_and_the_stream_surface(tmp_path, monkeypatch, source, agent, stream):
    from gateway import session_policy
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_policy import build_policy
    policy = build_policy({'source': source, 'cwd': str(tmp_path), **LAUNCH}, {'platform_toolsets': {'cli': []}})
    monkeypatch.setattr(session_policy, 'policy_for_source', lambda runner, source: policy)
    runner = SimpleNamespace(
        _provider_routing={}, _peek_session_state=lambda key: None,
        session_authority=SimpleNamespace(profile_id=str(tmp_path)),
        _resolve_session_agent_runtime=lambda **kw: ('m', {'provider': 'custom'}),
        _resolve_turn_agent_config=lambda message, model, runtime: {'model': model, 'runtime': runtime})
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL, chat_id='c'), session_key='k',
                                user_config={}, message='hi', inbound_message_id=None, enabled_toolsets=[])
    turn._combined_ephemeral_prompt = lambda: None
    seen = {}
    turn._setup_stream_consumer = lambda platform_key: seen.update(stream=platform_key) or (None, None, None, False)

    def built(route, platform_key, ephemeral, max_iterations, reasoning, pr):
        raise _Built(platform_key)
    turn._resolve_turn_agent = built
    with pytest.raises(_Built) as caught:
        turn._run_sync_scoped()
    assert (caught.value.args[0], seen['stream']) == (agent, stream)
```

(The harness is the one in `tests/gateway/test_session_config_loaders.py::test_in_process_turn_routes_with_the_sessions_own_provider_routing`. If `_run_sync_scoped` needs another runner attribute there, copy it from that test.)

- [ ] **Step 2: Run and confirm it fails**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_agent_platform.py`
Expected: the `wing` case FAILS with `('desktop', 'desktop') != ('wing', 'desktop')`; the `gui` case passes.

- [ ] **Step 3: Implement**

`gateway/session_policy.py`, in `LocalSessionPolicy` directly after `client_env_scope`:

```python
    @property
    def agent_platform(self):
        """The platform the session's AIAgent runs as: its surface, except Wing, whose prompt carries its own
        platform hint (``platform_hints.wing``). Display, streaming and tool settings keep reading ``platform``."""
        from agent.session_source import WING_SOURCE
        return WING_SOURCE if self.source == WING_SOURCE else self.platform
```

`gateway/run_turn_runner.py:1303-1305` becomes:

```python
        agent, reused_cached_agent = self._resolve_turn_agent(
            turn_route, policy.agent_platform if policy else platform_key, combined_ephemeral, max_iterations,
            reasoning_config, pr,
        )
```

`gateway/session_tui_side.py:110`: `platform=policy.platform` → `platform=policy.agent_platform`.

- [ ] **Step 4: Run and confirm it passes**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_agent_platform.py tests/agent/test_platform_hint_wing.py tests/gateway/test_session_config_loaders.py tests/gateway/test_wing_create_surface.py`
Expected: all pass. Mutant check: revert the `run_turn_runner.py` argument to `platform_key` → the `wing` case goes red.

- [ ] **Step 5: Commit**

```
git add gateway/session_policy.py gateway/run_turn_runner.py gateway/session_tui_side.py tests/gateway/test_wing_agent_platform.py
git commit -m "fix(gateway): a wing session's agent keeps platform wing for its hint

The canonical policy maps wing to the desktop surface, so the turn agent was built with
platform=desktop and the Wing platform hint was lost. LocalSessionPolicy.agent_platform
keeps wing for the AIAgent (turn and /bg side agent); display and streaming settings still
read the desktop surface.

Card t_35b15524.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Desktop parity at the gateway sites that name `gui`

**Files:**
- Modify: `gateway/session_policy.py:687` (surface toolsets; line 695 stays `gui`-only)
- Modify: `gateway/local_viewer_events.py:61`
- Modify: `gateway/run_housekeeping_disk.py:93`
- Modify: `gateway/session_queued_controls.py:30`
- Modify: `gateway/session_worker_delegation.py:57`
- Create: `tests/gateway/test_wing_desktop_parity.py`

**Interfaces:**
- Consumes: `SURFACES['wing']` (Task 1). `agent.session_source.WING_SOURCE == 'wing'` (feat).
- Produces: nothing new. Behaviour only.

Keep these `gui`-only (desktop-only client specifics, spec §2): `session_policy.py:673` (`desktop_ui` refusal), `:695` (`CLIENT_SURFACE_TOOLSETS` for `gui`, `{project}` otherwise), `:1039` and `session_local.py:280` (local-Desktop marks).

- [ ] **Step 1: Write the failing tests** in `tests/gateway/test_wing_desktop_parity.py`

```python
"""Card t_35b15524: a wing session is the Desktop chat surface on the canonical owner except the
desktop-only client toolsets (``desktop_ui``, ``catalog``), as on the legacy path
(tui_gateway/agent_factory.py::_gui_surface_toolsets)."""
from types import SimpleNamespace

LAUNCH = {'model': 'm', 'provider': 'custom', 'base_url': 'http://127.0.0.1:9/v1'}
CONFIG = {'platform_toolsets': {'cli': []}}


def _policy(source, tmp_path, config=CONFIG):
    from gateway.session_policy import build_policy
    return build_policy({'source': source, 'cwd': str(tmp_path), **LAUNCH}, dict(config))


def test_wing_gets_the_project_toolset_but_not_the_desktop_client_toolsets(tmp_path):
    wing, gui, cli = (set(_policy(source, tmp_path).toolsets) for source in ('wing', 'gui', 'cli'))
    assert 'project' in wing and 'project' not in cli
    assert gui - wing == {'desktop_ui', 'catalog'}


def test_wing_defaults_show_reasoning_from_config_like_the_desktop(tmp_path):
    from gateway.local_viewer_events import launch_shows_reasoning
    config = {**CONFIG, 'display': {'show_reasoning': True}}
    assert launch_shows_reasoning(_policy('wing', tmp_path, config), None) is True
    assert launch_shows_reasoning(_policy('cli', tmp_path, config), None) is False


def test_a_wing_row_is_an_interactive_source_for_queued_controls(tmp_path):
    from gateway.session_queued_controls import _interactive
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('w', source='wing', model='m')
        assert _interactive(SimpleNamespace(launch_sources={}, db=db), 'w') is True
    finally:
        db.close()
```

- [ ] **Step 2: Run and confirm they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_desktop_parity.py`
Expected: 3 failures (`'project' in wing` false, `False is True`, `False is True`).

- [ ] **Step 3: Implement**

`gateway/session_policy.py:687`. Add `from agent.session_source import WING_SOURCE` to the function's existing local imports (lines 652-654), then:

```python
    if explicit is None and source in {'tui', 'gui', WING_SOURCE}:
```

Leave line 695 unchanged (`gui` → `CLIENT_SURFACE_TOOLSETS`, any other → `{'project'}`).

`gateway/local_viewer_events.py`: add `from agent.session_source import WING_SOURCE` below `from gateway import local_desktop_ui` (line 18), then line 61:

```python
# Launch sources whose viewer never sets the session flag; main's TUI gateway defaulted it from config.
_CONFIG_DEFAULT_SOURCES = frozenset({"tui", "gui", WING_SOURCE})
```

`gateway/run_housekeeping_disk.py`: add `from agent.session_source import WING_SOURCE` after `from pathlib import Path` (line 15), then line 93:

```python
    if policy is None or policy.source not in {"tui", "gui", WING_SOURCE}:
```

`gateway/session_queued_controls.py`: add `from agent.session_source import WING_SOURCE` after `from functools import partial` (line 15), then line 30:

```python
_INTERACTIVE_SOURCES = frozenset({'cli', 'tui', 'gui', WING_SOURCE, 'desktop', 'acp'})
```

and extend the comment at line 25 to "(classic CLI, TUI, Desktop, Wing, ACP editor)".

`gateway/session_worker_delegation.py`: add `from agent.session_source import WING_SOURCE` after `import time` (line 9), then line 57:

```python
    if session is None or session['source'] not in ('cli', 'tui', 'gui', WING_SOURCE, 'tool', 'oneshot'):
```

- [ ] **Step 4: Run and confirm they pass**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_wing_desktop_parity.py tests/gateway/test_wing_create_surface.py tests/gateway/test_wing_agent_platform.py tests/tui_gateway/test_wing_session_source.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```
git add gateway/session_policy.py gateway/local_viewer_events.py gateway/run_housekeeping_disk.py gateway/session_queued_controls.py gateway/session_worker_delegation.py tests/gateway/test_wing_desktop_parity.py
git commit -m "fix(gateway): wing sessions get the desktop chat behaviour on the canonical owner

Surface toolsets (project, without the desktop-only desktop_ui/catalog), the show_reasoning
config default, the prune notice, queued steer/replace and async delegation named only gui.
Wing is the desktop chat surface everywhere but its hint and the client-only toolsets.

Card t_35b15524.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `profiles.list` rows say whether this connection can address them (Part 3)

**Files:**
- Modify: `tui_gateway/contracts/canonical_projections.py:40-41` (`CanonicalProfileRow`)
- Modify: `gateway/session_group_controls.py:75-76` (call), `:390-444` (`_profiles`)
- Regenerate: `apps/shared/src/gateway-contract.generated.ts`, `apps/shared/src/gateway-contract.openrpc.json`
- Create: `tests/gateway/test_profiles_addressable.py`

**Interfaces:**
- Produces (wire, for the Wing app): every canonical `profiles.list` row carries `addressable: bool`. `true` exactly when a `profile: <row.name>` param on this socket is routed (not refused with `profile_mismatch`).

- [ ] **Step 1: Write the failing tests** in `tests/gateway/test_profiles_addressable.py`

```python
"""Card t_35b15524, Part 3: a client lists only the profiles its socket can open. ``profiles.list`` names every
profile under the root (RT-38) but only some are addressable; ``addressable`` agrees with the router
(``AuthorityConnection._sibling_for``): a row marked false answers ``profile_mismatch``, a final refusal."""
import pytest

from tests.gateway.test_bot_roster_fleet import _rows, fleet  # noqa: F401  (fixture)
from gateway.session_authorities import owner_scope


async def _session_list(connection, profile):
    with owner_scope(connection.authority):
        return await connection.dispatch({'id': 2, 'method': 'session.list', 'params': {'profile': profile}})


async def _assert_flags_match_routing(fleet, connection, expected):
    rows = _rows(await fleet.call(connection, 'profiles.list'))
    assert {name: row['addressable'] for name, row in rows.items()} == expected
    for name, addressable in expected.items():
        reply = await _session_list(connection, name)
        if addressable:
            assert 'result' in reply, (name, reply)
        else:
            assert reply['error']['code'] == 4001 and reply['error']['data']['reason'] == 'profile_mismatch', reply


@pytest.mark.asyncio
async def test_a_host_scoped_socket_addresses_every_served_profile(fleet):
    host = fleet.connect('default', scope='host')
    await _assert_flags_match_routing(fleet, host, {'default': True, 'alpha': True, 'beta': True, 'gamma': True})


@pytest.mark.asyncio
async def test_a_profile_bound_socket_addresses_only_its_own_profile(fleet):
    # Birk's topology from the socket's side: one addressable profile, every other row discovery only,
    # including a profile directory nobody serves (``profiles/coder``, metadata-only, RT-38).
    coder = fleet.homes['default'] / 'profiles' / 'coder'
    coder.mkdir(parents=True)
    (coder / 'config.yaml').write_text('model:\n  default: fixture\n')
    bound = fleet.connect('beta')
    await _assert_flags_match_routing(
        fleet, bound, {'default': False, 'alpha': False, 'beta': True, 'gamma': False, 'coder': False})
```

(A profile-bound socket refuses every other name before any registry lookup, `session_controls.py:156-157`, so a hot-serve rescan picking up `coder` cannot change this test's answer.)

Add one more test with the same `fleet` fixture, using `include_sessions=False`, to prove the flag does not depend on session fields:

```python
@pytest.mark.asyncio
async def test_addressable_does_not_depend_on_session_fields(fleet):
    host = fleet.connect('default', scope='host')
    rows = _rows(await fleet.call(host, 'profiles.list', include_sessions=False))
    assert all(row['addressable'] for row in rows.values()), rows
```

- [ ] **Step 2: Run and confirm they fail**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_profiles_addressable.py`
Expected: FAIL with `KeyError: 'addressable'` (or a contract violation once the field is declared but not set).

- [ ] **Step 3: Implement**

`tui_gateway/contracts/canonical_projections.py`:

```python
class CanonicalProfileRow(ProfileRow):
    last_session: ProfileCanonicalSession | None = None
    #: Whether a ``profile: <name>`` param on THIS socket is routed: the served profile's own row, and a
    #: sibling this process serves when the socket is host-scoped. A false row is discovery metadata only;
    #: naming it answers ``4001 profile_mismatch`` (final).
    addressable: bool
```

`gateway/session_group_controls.py`, in `dispatch_group_control` (line 76):

```python
                return _profiles(authority, actor, home, supplied, siblings, host_scoped=connection.host_scoped)
```

In `_profiles` (line 390), change the signature and docstring:

```python
def _profiles(authority, actor, home, params, siblings=(), *, host_scoped=False):
    """The Bots roster: this profile's row, then every profile it can name. ``siblings`` is
    ``_roster_siblings``' snapshot; a sibling with a reader carries the same session fields.
    ``addressable`` follows ``AuthorityConnection._sibling_for``: own row, or a served sibling
    addressable by its own name on a host-scoped socket."""
```

Set the flag on the own row, right after `row = {...}` (line 401-404):

```python
    row['addressable'] = True
```

Give `discovered` the flag (line 419) and stamp it into `entry`:

```python
    def discovered(configured, target, reach=None, addressable=False):
        ...
        entry = {'name': configured, 'path': str(target), 'is_default': configured == 'default',
                 'model': '', 'provider': '', 'description': '', 'display_name': configured,
                 'skill_count': 0, 'addressable': addressable}
```

and pass it for siblings (line 439-444):

```python
    for bot, reader in siblings:
        ...
        discovered(served_profile_name(bot.home), bot.home,
                   (bot.authority, reader) if reader is not None else None,
                   addressable=host_scoped and bot.problem is None)
```

All other `discovered(...)` calls keep the default `False`.

Regenerate contracts:
Run: `/home/birk/.hermes/hermes-agent/venv/bin/python scripts/gen_gateway_contracts.py`
Expected: the canonical profile row type gains `addressable: boolean` (required) in both generated files.

- [ ] **Step 4: Run and confirm they pass**

Run: `HERMES_PYTHON=/home/birk/.hermes/hermes-agent/venv/bin/python TMPDIR=/home/birk/scratch/t_35b15524/tmp scripts/run_tests.sh tests/gateway/test_profiles_addressable.py tests/gateway/test_bot_roster_fleet.py tests/gateway/test_session_group_controls.py tests/gateway/test_local_source_labels.py tests/gateway/test_canonical_wire_contracts.py tests/tui_gateway/contracts/test_generated.py`
Expected: all pass. If a sibling row is `addressable: true` but `session.list` refuses it, the flag disagrees with `_sibling_for`. Fix the flag, not the test.

- [ ] **Step 5: Commit**

```
git add tui_gateway/contracts/canonical_projections.py gateway/session_group_controls.py apps/shared/src/gateway-contract.generated.ts apps/shared/src/gateway-contract.openrpc.json tests/gateway/test_profiles_addressable.py
git commit -m "feat(gateway): profiles.list rows say whether the socket can address them

profiles.list names every profile under the root, but only the served one (and, on a
host-scoped socket, served siblings) can be opened; naming another answers 4001
profile_mismatch. Wing listed them all and retried session.list forever. Each canonical
row now carries addressable, which agrees with AuthorityConnection._sibling_for.

Card t_35b15524.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Handoff to the Wing repo (no core task)

The app contract is in spec §4.3. In short: list and poll only `addressable: true` profiles. Stop retrying on 4001 `profile_mismatch` / `not_found` / `invalid_params` / `permission_denied` / `unsupported_projection` / `runtime_retired`. Retry only on `runtime_draining` and 5001. Do not poll `subagent.list` for a session that `session.create`/`session.resume` has not returned on this socket.
