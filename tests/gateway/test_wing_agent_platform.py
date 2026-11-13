"""Card t_35b15524: a Wing session's agent runs as platform ``wing`` (its prompt carries the Wing hint,
tests/agent/test_platform_hint_wing.py) while every display/streaming setting reads the desktop surface."""
from types import SimpleNamespace

import pytest

# Import at collection time, not inside the test: a first import of ``gateway.run`` inside a test
# trips tests/home_io_guard.py (hermes_bootstrap's interrupted-pull settling stats the real repo's
# .git dir). Same reasoning as tests/gateway/test_wing_create_surface.py.
import gateway.run  # noqa: F401,E402
import gateway.run_turn_runner  # noqa: F401,E402
import gateway.session_policy  # noqa: F401,E402

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
