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
