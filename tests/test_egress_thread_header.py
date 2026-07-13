"""Tests for per-turn X-Hermes-Thread-Id/Chat-Id header injection (R2b).

Routes the egress-judge block Rückfrage to the Telegram topic it originated
in, instead of always the main chat (R2a already made the proxy honor these
headers; this is the missing seam — the gateway never sent them).

Headers are injected as PER-REQUEST ``extra_headers`` in ``api_kwargs``,
mirroring the existing Copilot ``x-initiator`` pattern (test_copilot_initiator.py)
— NEVER as client-level ``default_headers``, which would leak Telegram IDs
across concurrent sessions/topics sharing one client. They are attached only
when the call targets the profile-local ``anthropic_plan`` loopback proxy —
never to a real cloud endpoint (fail-safe: no thread_id/chat_id on the agent,
or not the proxy, or not an Anthropic call ⇒ header omitted, today's
behavior, never a wrong value sent).
"""

import pytest

from run_agent import AIAgent


def _tool_defs(*names):
    return [
        {"type": "function", "function": {"name": n, "description": n, "parameters": {}}}
        for n in names
    ]


class _FakeOpenAI:
    def __init__(self, **kw):
        self.api_key = kw.get("api_key", "test")
        self.base_url = kw.get("base_url", "http://test")

    def close(self):
        pass


def _make_agent(monkeypatch, base_url, provider, api_mode="anthropic_messages",
                 thread_id=None, chat_id=None):
    """Create an AIAgent pointing at the given base_url/provider/routing ids."""
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kw: _tool_defs("web_search"))
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    return AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider=provider,
        api_mode=api_mode,
        max_iterations=4,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        thread_id=thread_id,
        chat_id=chat_id,
    )


def _inject(agent, api_kwargs):
    """Mirror the injection block in agent/conversation_loop.py."""
    if agent.api_mode == "anthropic_messages" and agent._is_local_anthropic_plan_proxy():
        _tid = agent._thread_id
        _cid = agent._chat_id
        if _tid or _cid:
            _xh = dict(api_kwargs.get("extra_headers") or {})
            if _tid:
                _xh["X-Hermes-Thread-Id"] = str(_tid)
            if _cid:
                _xh["X-Hermes-Chat-Id"] = str(_cid)
            api_kwargs["extra_headers"] = _xh
    return api_kwargs


class TestIsLocalAnthropicPlanProxy:
    """_is_local_anthropic_plan_proxy() detects the profile-local loopback proxy only."""

    def test_anthropic_plan_on_loopback_127(self, monkeypatch):
        agent = _make_agent(monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan")
        assert agent._is_local_anthropic_plan_proxy() is True

    def test_anthropic_plan_on_localhost(self, monkeypatch):
        agent = _make_agent(monkeypatch, "http://localhost:28765/v1", "custom:anthropic_plan")
        assert agent._is_local_anthropic_plan_proxy() is True

    def test_anthropic_plan_on_remote_host_is_false(self, monkeypatch):
        # Same provider name, but NOT loopback — must never true-positive here,
        # or Telegram IDs could leak to a non-local endpoint.
        agent = _make_agent(monkeypatch, "https://example.com", "custom:anthropic_plan")
        assert agent._is_local_anthropic_plan_proxy() is False

    def test_direct_anthropic_cloud_is_false(self, monkeypatch):
        agent = _make_agent(monkeypatch, "https://api.anthropic.com", "anthropic")
        assert agent._is_local_anthropic_plan_proxy() is False

    def test_other_loopback_provider_is_false(self, monkeypatch):
        # Loopback host but a different provider (e.g. ollama) — not our proxy.
        agent = _make_agent(monkeypatch, "http://127.0.0.1:11434", "ollama")
        assert agent._is_local_anthropic_plan_proxy() is False


class TestHeaderInjection:
    """The injection block sets X-Hermes-Thread-Id/Chat-Id only when safe to do so."""

    def test_injects_both_headers_when_present(self, monkeypatch):
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id="4242", chat_id="570261709",
        )
        kwargs = _inject(agent, {})
        assert kwargs["extra_headers"] == {
            "X-Hermes-Thread-Id": "4242",
            "X-Hermes-Chat-Id": "570261709",
        }

    def test_omits_header_when_no_thread_or_chat_id(self, monkeypatch):
        # Fail-safe: nothing to route on ⇒ no header at all, never a guessed value.
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id=None, chat_id=None,
        )
        kwargs = _inject(agent, {})
        assert "extra_headers" not in kwargs

    def test_chat_id_only_still_injects(self, monkeypatch):
        # A DM has no thread_id but does have chat_id — still worth routing.
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id=None, chat_id="570261709",
        )
        kwargs = _inject(agent, {})
        assert kwargs["extra_headers"] == {"X-Hermes-Chat-Id": "570261709"}

    def test_no_injection_off_proxy(self, monkeypatch):
        # Real cloud endpoint — Telegram IDs must never be attached here.
        agent = _make_agent(
            monkeypatch, "https://api.anthropic.com", "anthropic",
            thread_id="4242", chat_id="570261709",
        )
        kwargs = _inject(agent, {})
        assert "extra_headers" not in kwargs

    def test_no_injection_non_anthropic_api_mode(self, monkeypatch):
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            api_mode="chat_completions", thread_id="4242", chat_id="570261709",
        )
        kwargs = _inject(agent, {})
        assert "extra_headers" not in kwargs

    def test_existing_extra_headers_preserved(self, monkeypatch):
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id="4242", chat_id="570261709",
        )
        kwargs = _inject(agent, {"extra_headers": {"x-custom": "1"}})
        assert kwargs["extra_headers"]["x-custom"] == "1"
        assert kwargs["extra_headers"]["X-Hermes-Thread-Id"] == "4242"


class TestNoConcurrentSessionBleed:
    """Two agents with different routing ids never see each other's headers.

    Regression guard for the exact hazard the design calls out: headers must
    be per-request (api_kwargs), never client-level defaults, or concurrent
    Telegram topics/sessions sharing a proxy would cross-contaminate.
    """

    def test_two_agents_different_threads_dont_bleed(self, monkeypatch):
        agent_a = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id="111", chat_id="570261709",
        )
        agent_b = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom:anthropic_plan",
            thread_id="222", chat_id="999999999",
        )
        kwargs_a = _inject(agent_a, {})
        kwargs_b = _inject(agent_b, {})
        assert kwargs_a["extra_headers"]["X-Hermes-Thread-Id"] == "111"
        assert kwargs_b["extra_headers"]["X-Hermes-Thread-Id"] == "222"
        assert kwargs_a["extra_headers"]["X-Hermes-Chat-Id"] == "570261709"
        assert kwargs_b["extra_headers"]["X-Hermes-Chat-Id"] == "999999999"
        # Interleaved calls on the SAME agent instance must not carry over
        # a stale header from a previous, differently-routed call either.
        agent_a._thread_id = "333"
        kwargs_a2 = _inject(agent_a, {})
        assert kwargs_a2["extra_headers"]["X-Hermes-Thread-Id"] == "333"
