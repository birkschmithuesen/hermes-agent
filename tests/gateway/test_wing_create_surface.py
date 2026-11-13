"""Card t_35b15524: the label ``gateway.ready`` advertises in ``session_sources`` (``wing``) is a canonical
create surface. An authority-bound socket accepts ``session.create {source: "wing"}`` and runs it as the
Desktop chat surface (spec docs/superpowers/specs/2026-11-13-wing-create-surface-design.md)."""
import json
import typing
from types import SimpleNamespace

import pytest

from gateway.session_controls import AuthorityConnection
# Import at collection time, not inside the tests: a first import of these modules inside a test
# (a) trips tests/home_io_guard.py (pydantic's plugin loader stats the real venv; hermes_bootstrap
# stats <repo>/.git/.../hermes-update-pull) and (b) re-declares contracts ("contract declared twice:
# prompt.receipt"). Measured by the architect 2026-11-13 on 68471c204ca1 + feat: without these
# lines 3 of 4 tests fail for harness reasons; with them the 4 fail only on the real refusal.
import gateway.run  # noqa: F401,E402
import gateway.session  # noqa: F401,E402
import gateway.session_authority  # noqa: F401,E402
import gateway.session_policy  # noqa: F401,E402
import tui_gateway.contracts.registry  # noqa: F401,E402
import tui_gateway.server  # noqa: F401,E402
import tui_gateway.ws  # noqa: F401,E402


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
