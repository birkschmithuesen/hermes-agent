"""Regression tests for _prepend_model_badge — the stream-timeout fallback fix.

Root cause (2026-07-07): when a MarkdownV2 edit times out, the queued-follow-up
path in gateway.run resends the final response via ``adapter.send()``, bypassing
the stream consumer's ``_send_or_edit`` — the only streaming site that prepends
the model badge. So the badge silently vanished on any reply that hit the
plain-text fallback (long messages, image-bearing turns). Live-observed in
session ...1705118 on 2026-07-07: "MarkdownV2 edit failed, falling back to plain
text: Timed out" -> "final stream delivery not confirmed" -> badge-less resend.

The resend site now runs its text through ``_prepend_model_badge`` (idempotent),
reusing the badge the consumer already computed for this turn. These tests pin
the helper's contract: prepend when missing, never double-stamp, pass through
when there's no badge.
"""
from gateway.run import _prepend_model_badge


class TestPrependModelBadge:
    def test_prepends_badge_when_absent(self):
        # This is the actual fix: a raw resend body gains the badge.
        out = _prepend_model_badge("Hello world", "[🤖 claude-sonnet-4]")
        assert out == "[🤖 claude-sonnet-4]\nHello world"

    def test_does_not_double_stamp(self):
        # If the streaming path already badged it, resend must not add a second.
        already = "[🤖 claude-sonnet-4]\nHello world"
        out = _prepend_model_badge(already, "[🤖 claude-sonnet-4]")
        assert out == already
        assert out.count("[🤖 claude-sonnet-4]") == 1

    def test_none_badge_passes_text_through(self):
        # Badge resolution failed -> the reply must still go out, un-badged,
        # never dropped.
        out = _prepend_model_badge("Hello world", None)
        assert out == "Hello world"

    def test_empty_badge_passes_text_through(self):
        out = _prepend_model_badge("Hello world", "")
        assert out == "Hello world"

    def test_preserves_switch_hint_badge(self):
        badge = "[🤖 gpt-5.4 · switched from claude-sonnet-4]"
        out = _prepend_model_badge("Answer", badge)
        assert out == f"{badge}\nAnswer"

    def test_non_str_text_returned_unchanged(self):
        # Defensive: a non-string body must not raise inside the resend path.
        sentinel = object()
        assert _prepend_model_badge(sentinel, "[🤖 x]") is sentinel
