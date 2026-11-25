"""Regression for Diag t_c1c20d41 (H1): a tight receipt poll must not re-read ``state.db`` on every
tick. ``SessionAuthority.receipt`` read-caches a non-terminal admission for
``RECEIPT_READ_CACHE_SECONDS`` and caches a terminal one forever, so N polls of the same admission
inside the window cost at most one SQLite read; a status change (``live.settling``) is never masked
by a stale cache entry."""
from types import SimpleNamespace

import pytest

from gateway.session_authority import admission_row
from gateway.session_persistence import authority_persistence
from tests.fixtures.storage_holder import raw_holder
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


def _owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    monkeypatch.setattr(run, '_load_gateway_config', lambda *a: {})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    db = store.persistence
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                             _set_session_reasoning_override=lambda route, value: None,
                             _resolve_session_agent_runtime=lambda **k: ('frozen', {}))
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    authority = SessionAuthority(runner, profile_id='owned', instance_id='owner',
                                 persistence=storage_handle(db.scope.profile_dir),
                                 epoch=db.runtime.begin_epoch(instance_id='owner'))
    owner = AuthorityConnection(authority, object(), {'user_id': 'human'})
    start = tmp_path / 'project'
    start.mkdir()
    ref = create_local_session(authority, owner.actor, dict(request_id='m', source='gui', cwd=str(start),
                                                            model='frozen', toolsets=[]))
    return store, db, authority, owner, ref


def _spy_on_admission_reads(monkeypatch):
    """Counts every raw SQLite read ``AdmissionsFacade.get`` performs, patched where production
    looks it up (the backend's ``_admission_store`` module), and still calls through."""
    import persistence.sqlite.authority._admission_store as admission_store
    calls = []
    original = admission_store.get_session_admission

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(admission_store, 'get_session_admission', counting)
    return calls


def _admit(authority, owner, ref, request_id):
    admission, _created = authority_persistence(authority).admissions.admit(
        epoch=authority.epoch, principal_id=owner.actor.subject, session_id=ref.session_id,
        request_id=request_id, payload={'text': 'hi'})
    return admission.admission_id


def _force_terminal(authority, admission_id, outcome='replied'):
    """Marks *admission_id* terminal directly on ``session_admissions``, as the settlement write
    does, without running a whole turn through the drain."""
    raw_holder(authority_persistence(authority))._execute_write(lambda conn: conn.execute(
        "UPDATE session_admissions SET status='terminal', outcome=? WHERE admission_id=?",
        (outcome, admission_id)))


@pytest.mark.asyncio
async def test_receipt_poll_of_a_queued_admission_reads_sqlite_at_most_twice(tmp_path, monkeypatch):
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    try:
        admission_id = _admit(authority, owner, ref, 'poll-queued')
        calls = _spy_on_admission_reads(monkeypatch)
        for _ in range(100):
            receipt = await authority.receipt(owner.actor, ref, admission_id)
            assert receipt.status == 'queued'
        assert len(calls) <= 2, f'expected at most one cache-filling read, saw {len(calls)}'
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_receipt_of_a_terminal_admission_never_reads_sqlite_again(tmp_path, monkeypatch):
    import time
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    try:
        admission_id = _admit(authority, owner, ref, 'poll-terminal')
        _force_terminal(authority, admission_id)
        calls = _spy_on_admission_reads(monkeypatch)
        first = await authority.receipt(owner.actor, ref, admission_id)
        assert first.status == 'terminal'
        assert len(calls) == 1
        time.sleep(0.3)  # past RECEIPT_READ_CACHE_SECONDS: a non-terminal entry would have expired
        for _ in range(10):
            again = await authority.receipt(owner.actor, ref, admission_id)
            assert again.status == 'terminal'
        assert len(calls) == 1, f'a terminal row must never be re-read, saw {len(calls)} reads'
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_receipt_cache_is_invalidated_by_a_live_settling_status_change(tmp_path, monkeypatch):
    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch)
    try:
        admission_id = _admit(authority, owner, ref, 'poll-settling')
        first = await authority.receipt(owner.actor, ref, admission_id)
        assert first.status == 'queued'
        live = authority.sessions[ref.session_id]
        started_row = admission_row(authority_persistence(authority).admissions.get(admission_id=admission_id))
        started_row['status'], started_row['outcome'] = 'started', None
        with live.event_stream.lock:
            live.settling[admission_id] = started_row
        second = await authority.receipt(owner.actor, ref, admission_id)
        assert second.status == 'started', 'a live.settling status change must not read as the stale cached status'
    finally:
        store.close_all_db_handles()


def test_kanban_worker_receipt_poll_backs_off_and_resets_on_status_change():
    from hermes_cli.kanban_worker_client import RECEIPT_POLL_MAX_S, RECEIPT_POLL_MIN_S, _next_poll_interval
    interval, sequence = RECEIPT_POLL_MIN_S, [RECEIPT_POLL_MIN_S]
    for _ in range(6):
        interval = _next_poll_interval(interval, status_changed=False)
        sequence.append(interval)
    assert sequence == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0])
    assert _next_poll_interval(RECEIPT_POLL_MAX_S, status_changed=True) == RECEIPT_POLL_MIN_S
