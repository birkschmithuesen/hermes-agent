"""Unit tests for gateway.run._model_badge — the always-show-with-switch-hint
model badge logic behind the TG model_badge feature.

Spec (2026-07-07, Birk verbatim): "ich will das das model bei jeder message
steht, aber bei model wechsel mit extra hinweis im badge." -> badge shows on
EVERY message; on a model switch it additionally carries a "switched from X"
hint for that one turn.

Pure function, no GatewayRunner instantiation needed (that's a 20k-line
monolith many test doubles construct via object.__new__, bypassing __init__ —
see test_stream_consumer_model_badge.py for the getattr-defensive fallback
this forced in the two run.py call sites).
"""
from gateway.run import _model_badge


class TestModelBadge:
    def test_first_message_shows_plain_badge(self):
        state = {}
        badge = _model_badge(state, "sess1", "anthropic/claude-sonnet-4")
        assert badge == "[🤖 claude-sonnet-4]"
        assert state["sess1"] == "anthropic/claude-sonnet-4"

    def test_same_model_next_message_still_shows_badge_no_switch_hint(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        badge = _model_badge(state, "sess1", "anthropic/claude-sonnet-4")
        assert badge == "[🤖 claude-sonnet-4]"
        assert "switched" not in badge

    def test_model_switch_adds_hint(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        badge = _model_badge(state, "sess1", "openrouter/openai/gpt-5.4")
        assert badge == "[🤖 gpt-5.4 · switched from claude-sonnet-4]"
        assert state["sess1"] == "openrouter/openai/gpt-5.4"

    def test_switch_hint_only_fires_once_then_reverts_to_plain(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        switched = _model_badge(state, "sess1", "openrouter/openai/gpt-5.4")
        assert "switched" in switched
        steady = _model_badge(state, "sess1", "openrouter/openai/gpt-5.4")
        assert steady == "[🤖 gpt-5.4]"
        assert "switched" not in steady

    def test_sessions_are_independent(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        badge = _model_badge(state, "sess2", "anthropic/claude-sonnet-4")
        assert badge == "[🤖 claude-sonnet-4]"
        assert "switched" not in badge

    def test_empty_or_none_model_returns_none(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        assert _model_badge(state, "sess1", "") is None
        assert _model_badge(state, "sess1", None) is None
        # state untouched on a no-op call
        assert state["sess1"] == "anthropic/claude-sonnet-4"

    def test_strips_provider_prefix_in_both_names(self):
        state = {"sess1": "anthropic/claude-sonnet-4"}
        badge = _model_badge(state, "sess1", "openrouter/openai/gpt-5.4")
        assert "openrouter/" not in badge
        assert "anthropic/" not in badge
