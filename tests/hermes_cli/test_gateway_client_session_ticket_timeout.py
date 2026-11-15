"""``_session_ticket`` hands a caller's timeout to the control query; every other caller keeps 5 s.

A managed worker mints its adoption ticket under its receipt budget
(``agent.runtime_session_store.worker_receipt_budget``), not the shared 5 s default.
"""
import json
from types import SimpleNamespace

from hermes_cli import gateway_client

_ENDPOINT = SimpleNamespace(profile_id='profile', instance_id='instance', control_home=None)
_GRANT = json.dumps({'ok': True, 'protocol': 1, 'id': 1, 'result': {
    'profile_id': 'profile', 'instance_id': 'instance', 'runtime_protocol': 1,
    'ticket': 'ticket-1'}}).encode() + b'\n'


def _control_socket(monkeypatch, answers):
    """Stands in for the owner's control socket; records the timeout of every query."""
    seen = []

    def query(home, request, timeout=5):
        seen.append(timeout)
        return answers.pop(0)
    monkeypatch.setattr(gateway_client, 'private_control_query', query)
    return seen


def test_other_callers_keep_the_five_second_default(monkeypatch, tmp_path):
    seen = _control_socket(monkeypatch, [_GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='native-http') == 'ticket-1'
    assert seen == [5]


def test_an_explicit_timeout_reaches_the_control_query(monkeypatch, tmp_path):
    seen = _control_socket(monkeypatch, [_GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='worker-adoption',
                                          timeout=120.0) == 'ticket-1'
    assert seen == [120.0]


def test_the_restart_wait_fallback_query_keeps_the_timeout(monkeypatch, tmp_path):
    refused = json.dumps({'ok': False, 'error': gateway_client._UNKNOWN_TICKET_PARAMS}).encode() + b'\n'
    seen = _control_socket(monkeypatch, [refused, _GRANT])
    assert gateway_client._session_ticket(tmp_path, _ENDPOINT, purpose='worker-adoption',
                                          refuse_restart_wait=True, timeout=120.0) == 'ticket-1'
    assert seen == [120.0, 120.0]
