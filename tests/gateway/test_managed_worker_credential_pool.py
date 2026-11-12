"""A key-less managed worker (every Kanban run) carries the credential pool its key was selected
from, as the in-process turn does (``_runtime_agent_kwargs``): a key with no credit left rotates to
the next pool entry inside the same run, and the exhausted status lands in the profile's
``auth.json``, where the next worker selects from.

Live incident 2026-10-23: one Anthropic key ran dry and every Kanban worker failed on it
(~480 runs) because ``_construct_agent`` built the agent with the bare key and no pool."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from tests.gateway.test_managed_turn_contract import assignment

KEY1, KEY2 = 'sk-ant-api03-fake-key-one', 'sk-ant-api03-fake-key-two'
MODEL = 'claude-sonnet-4-5'
CREDIT = ('Your credit balance is too low to access the Anthropic API. '
          'Please go to Plans & Billing to upgrade or purchase credits.')


def _sse(events):
    return ''.join(f'event: {name}\ndata: {json.dumps(body)}\n\n' for name, body in events).encode()


_OK_STREAM = _sse([
    ('message_start', {'type': 'message_start', 'message': {
        'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'content': [], 'model': MODEL,
        'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
    ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
    ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'ok'}}),
    ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
    ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                       'usage': {'output_tokens': 1}}),
    ('message_stop', {'type': 'message_stop'})])


def _fake_anthropic(log):
    """Anthropic Messages endpoint: KEY1 has no credit left (the real 400 body), KEY2 streams a reply.
    ``log`` gets ``(path, x-api-key)`` per POST; the agent also probes unrelated paths."""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('content-length', 0)))
            key = self.headers.get('x-api-key')
            log.append((self.path, key))
            if key == KEY2:
                status, ctype, data = 200, 'text/event-stream', _OK_STREAM
            else:
                status, ctype = 400, 'application/json'
                data = json.dumps({'type': 'error', 'error': {'type': 'invalid_request_error', 'message': CREDIT}}).encode()
            self.send_response(status)
            self.send_header('content-type', ctype)
            self.send_header('content-length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _messages(log):
    return [key for path, key in log if path.endswith('/messages')]


def _anthropic_frame(tmp_path, monkeypatch, base_url):
    """A key-less (Kanban-shaped) worker frame whose frozen route is ``anthropic`` at ``base_url``."""
    frame, _ = assignment(tmp_path, monkeypatch)
    config = json.loads(frame['policy']['config_json'])
    config['model'] = {'default': MODEL, 'provider': 'anthropic', 'base_url': base_url}
    request = {k: v for k, v in json.loads(frame['policy']['request_json']).items() if k not in ('provider', 'base_url')}
    policy = {**frame['policy'], 'model': MODEL, 'config_json': json.dumps(config), 'request_json': json.dumps(request)}
    return {**frame, 'policy': policy, 'api_key': None}


def _write_pool(home, base_url):
    def entry(i, key, label):
        return {'id': f'k{i}', 'label': label, 'auth_type': 'api_key', 'priority': i - 1,
                'source': f'manual:{label}', 'access_token': key, 'base_url': base_url}
    (home / 'auth.json').write_text(json.dumps({'version': 1, 'credential_pool': {
        'anthropic': [entry(1, KEY1, 'api-konto2'), entry(2, KEY2, 'api-konto3')]}}))


def _run_worker(frame, monkeypatch):
    """The worker's real ``execute`` -> ``_construct_agent`` -> real ``AIAgent`` and its turn; only the
    owner side (RPC, runtime store, control pipe) is stubbed, and the agent persists to no session DB."""
    from agent import managed_worker as worker
    from run_agent import AIAgent

    class Agent(AIAgent):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, 'session_db': None})

    class Store:
        def __init__(self, *args): pass
        def get_messages_as_conversation(self, sid): return []
        def flush_token_counts(self): pass
        def finish(self): pass
        def close(self): pass

    class Controls:
        def __init__(self, *args):
            self.stopped, self.finish = threading.Event(), threading.Event()
            self.finish.set()
        def approval(self, data): pass
        def clarify(self, questions): pass

    monkeypatch.setattr(worker, 'bind_worker_policy', lambda frame: None)
    monkeypatch.setattr(worker, 'discover_profile_mcp', lambda policy: None)
    monkeypatch.setattr(worker, 'retire_agent', lambda agent: None)
    monkeypatch.setattr(worker, 'WorkerControls', Controls)
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', lambda home: lambda *a, **k: {'owner_epoch': 1})
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', Store)
    monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', lambda: None)
    monkeypatch.setattr('run_agent.AIAgent', Agent)
    monkeypatch.setattr('agent.title_generator.wait_for_title_upgrades', lambda: None)
    sent = []
    worker.execute(frame, SimpleNamespace(send=lambda kind, **payload: sent.append((kind, payload))))
    return sent


def _result(sent):
    return next(payload['result'] for kind, payload in sent if kind == 'result')


def test_drained_pool_key_rotates_to_the_next_entry_and_the_next_worker_starts_there(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    # No ambient Claude/Anthropic credential may seed the pool: only the two auth.json rows exist.
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    log = []
    server = _fake_anthropic(log)
    base_url = f'http://127.0.0.1:{server.server_port}'
    try:
        _write_pool(home, base_url)
        first = _result(_run_worker(_anthropic_frame(tmp_path, monkeypatch, base_url), monkeypatch))
        first_keys = _messages(log)
        log.clear()
        second = _result(_run_worker(_anthropic_frame(tmp_path, monkeypatch, base_url), monkeypatch))
        second_keys = _messages(log)
    finally:
        server.shutdown()

    # The same run retried on key 2 after key 1's credit 400, and the turn completed.
    assert first_keys[0] == KEY1 and KEY2 in first_keys, first_keys
    assert not first.get('failed'), first
    # Persisted where the next worker reads it: the profile's auth.json on disk, not the in-memory pool.
    rows = {row['id']: row for row in json.loads((home / 'auth.json').read_text())['credential_pool']['anthropic']}
    assert rows['k1']['last_status'] == 'exhausted' and rows['k1']['last_error_code'] == 400, rows['k1']
    assert rows['k2'].get('last_status') != 'exhausted', rows['k2']
    # A fresh worker process selects from that state and never spends a request on the drained key.
    assert second_keys and second_keys[0] == KEY2, second_keys
    assert not second.get('failed'), second
