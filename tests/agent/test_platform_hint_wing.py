"""Wing (phone client, source ``wing``) gets the conversation-mode hint from config; the desktop app does not.

Card t_9d9bcf31. The hint is pure config (``platform_hints.wing.append``); an unknown platform has an
empty built-in hint and still honours the override (agent/system_prompt.py _resolve_platform_hint). The
surface switch (agent/surface_switch.py) makes the mode follow the CLIENT when a session moves between
the Mac and the phone.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_builder import PLATFORM_HINTS
from agent.surface_switch import stage_surface_switch_note
from agent.system_prompt import build_system_prompt_parts

WING_HINT = (
    "Standard Wing (Handy-App): Gesprächsmodus (Skill voice-conversational-mode, Abschnitt Ausgabekanal je "
    "Client). Wing liest deine Antwort selbst vor: Plain Text im Gesprächsstil, kein Markdown, keine Listen, "
    "keine Pfade, kein text_to_speech."
)
OVERRIDES = {"wing": {"append": WING_HINT}}


def _make_agent(platform: str) -> SimpleNamespace:
    return SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides=OVERRIDES,
        model="",
        provider="",
        pass_session_id=False,
        session_id="",
        platform=platform,
    )


def _stable_prompt(agent) -> str:
    with (
        patch("agent.prompt_builder.load_soul_md", return_value=""),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompt_builder.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def test_wing_prompt_has_the_conversation_hint_and_desktop_prompt_does_not(monkeypatch):
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    wing = _stable_prompt(_make_agent("wing"))
    desktop = _stable_prompt(_make_agent("desktop"))
    assert WING_HINT in wing
    assert PLATFORM_HINTS["desktop"] not in wing
    assert WING_HINT not in desktop
    assert PLATFORM_HINTS["desktop"] in desktop


def _switch(stored: str, current: str) -> tuple[bool, str]:
    agent = SimpleNamespace(
        provider="openrouter", api_mode="chat_completions", platform=current, session_id="s",
        _platform_hint_overrides=OVERRIDES, _surface_switch_note="",
    )
    prompt = f"SYSTEM PROMPT BODY\n\nModel: test-model\nProvider: openrouter\nPlatform: {stored}"
    staged = stage_surface_switch_note(agent, prompt, [{"role": "user", "content": "hi"}])
    return staged, agent._surface_switch_note


def test_mac_session_opened_in_wing_gets_the_wing_hint_as_turn_note():
    staged, note = _switch(stored="desktop", current="wing")
    assert staged
    assert WING_HINT in note


def test_wing_session_opened_on_the_mac_gets_the_desktop_hint_back():
    staged, note = _switch(stored="wing", current="desktop")
    assert staged
    assert WING_HINT not in note
    assert PLATFORM_HINTS["desktop"] in note
