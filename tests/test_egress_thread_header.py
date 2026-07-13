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
or not the proxy, ⇒ header omitted, today's behavior, never a wrong value
sent).

Live-verified 2026-07-13 (real Telegram round-trip, gateway logs +
sessions/request_dump_*.json): ``agent.provider`` is ALWAYS the literal
string ``"custom"`` for any ``custom_providers`` entry —
``resolve_runtime_provider()``/``_try_resolve_from_custom_pool`` hardcode
``provider_label="custom"``; the per-entry name (``anthropic_plan``) only
survives in an internal ``"source"`` field never propagated onto the agent.
A provider-name substring check is therefore always False in production —
the original (broken) version of this fix. Also live-verified: real traffic
through this proxy dispatches via ``chat_completions`` as often as the
native ``anthropic_messages`` path, so the check must not gate on api_mode
either. The proxy is identified by loopback host + a Claude-named model
instead (``_is_claude_model``), which correctly excludes a same-host non-Claude
custom provider (e.g. a local ollama endpoint).
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
                 model="claude-sonnet-5", thread_id=None, chat_id=None):
    """Create an AIAgent pointing at the given base_url/provider/routing ids."""
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda **kw: _tool_defs("web_search"))
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda: {})
    monkeypatch.setattr("run_agent.OpenAI", _FakeOpenAI)
    return AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider=provider,
        api_mode=api_mode,
        model=model,
        max_iterations=4,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        thread_id=thread_id,
        chat_id=chat_id,
    )


def _inject(agent, api_kwargs):
    """Mirror the injection block in agent/conversation_loop.py."""
    if agent._is_local_anthropic_plan_proxy():
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

    def test_real_production_values_match(self, monkeypatch):
        # THE regression case: live production has provider=="custom" (never
        # "custom:anthropic_plan") — verified via gateway/run.py:_resolve_runtime_agent_kwargs()
        # and the actual gateway log line at the 2026-07-13 block. Must still match.
        agent = _make_agent(monkeypatch, "http://127.0.0.1:28764", "custom", model="claude-sonnet-5")
        assert agent._is_local_anthropic_plan_proxy() is True

    def test_anthropic_plan_on_loopback_127(self, monkeypatch):
        agent = _make_agent(monkeypatch, "http://127.0.0.1:28764", "custom", model="claude-sonnet-5")
        assert agent._is_local_anthropic_plan_proxy() is True

    def test_anthropic_plan_on_localhost(self, monkeypatch):
        agent = _make_agent(monkeypatch, "http://localhost:28765/v1", "custom", model="claude-sonnet-5")
        assert agent._is_local_anthropic_plan_proxy() is True

    def test_claude_model_on_remote_host_is_false(self, monkeypatch):
        # Claude-named model, but NOT loopback — must never true-positive here,
        # or Telegram IDs could leak to a non-local endpoint.
        agent = _make_agent(monkeypatch, "https://example.com", "custom", model="claude-sonnet-5")
        assert agent._is_local_anthropic_plan_proxy() is False

    def test_direct_anthropic_cloud_is_false(self, monkeypatch):
        agent = _make_agent(monkeypatch, "https://api.anthropic.com", "anthropic", model="claude-sonnet-5")
        assert agent._is_local_anthropic_plan_proxy() is False

    def test_other_loopback_provider_is_false(self, monkeypatch):
        # Loopback host but a non-Claude model (e.g. local ollama) — not our proxy.
        agent = _make_agent(monkeypatch, "http://127.0.0.1:11434", "custom", model="qwen2.5")
        assert agent._is_local_anthropic_plan_proxy() is False

    def test_empty_model_is_false(self, monkeypatch):
        agent = _make_agent(monkeypatch, "http://127.0.0.1:28764", "custom", model="")
        assert agent._is_local_anthropic_plan_proxy() is False


class TestHeaderInjection:
    """The injection block sets X-Hermes-Thread-Id/Chat-Id only when safe to do so."""

    def test_injects_both_headers_when_present(self, monkeypatch):
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom",
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
            monkeypatch, "http://127.0.0.1:28764", "custom",
            thread_id=None, chat_id=None,
        )
        kwargs = _inject(agent, {})
        assert "extra_headers" not in kwargs

    def test_chat_id_only_still_injects(self, monkeypatch):
        # A DM has no thread_id but does have chat_id — still worth routing.
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom",
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

    def test_injects_under_chat_completions_api_mode_too(self, monkeypatch):
        # Live-verified 2026-07-13: real traffic through this proxy dispatches
        # via chat_completions as often as anthropic_messages. The header must
        # not depend on which SDK/wire format handles the call.
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom", api_mode="chat_completions",
            thread_id="4242", chat_id="570261709",
        )
        kwargs = _inject(agent, {})
        assert kwargs["extra_headers"]["X-Hermes-Thread-Id"] == "4242"

    def test_existing_extra_headers_preserved(self, monkeypatch):
        agent = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom",
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
            monkeypatch, "http://127.0.0.1:28764", "custom",
            thread_id="111", chat_id="570261709",
        )
        agent_b = _make_agent(
            monkeypatch, "http://127.0.0.1:28764", "custom",
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
