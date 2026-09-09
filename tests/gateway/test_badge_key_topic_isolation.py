"""RED->GREEN test for the badge-key-collision bug.

Bug: `_badge_key_for_source` (gateway/run.py) falls back to
`f"{platform}:{chat_id}"` when `session_key` is falsy. That fallback omits
`source.thread_id`, so two different Telegram forum topics (or threaded DMs)
inside the same chat collapse onto the SAME `_badge_last_model_by_session`
key. When topic A last used Opus and topic B is steadily on Haiku, every
Haiku message in topic B spuriously shows "switched from Opus" because
`_model_badge` sees topic A's leftover `prev` value.

`_model_badge` itself is correct (see test_model_badge.py) — this test
covers the key-construction helper that feeds it.
"""
from types import SimpleNamespace

from gateway.run import _badge_key_for_source, _model_badge


def _source(chat_id="123", thread_id=None):
    return SimpleNamespace(platform="telegram", chat_id=chat_id, thread_id=thread_id)


class TestBadgeKeyForSource:
    def test_session_key_wins_when_present(self):
        key = _badge_key_for_source("agent:main:telegram:dm:123", _source(thread_id="99"))
        assert key == "agent:main:telegram:dm:123"

    def test_falls_back_to_platform_chat_id_without_thread(self):
        key = _badge_key_for_source(None, _source(chat_id="123", thread_id=None))
        assert key == "telegram:123"

    def test_fallback_isolates_by_thread_id(self):
        key_a = _badge_key_for_source(None, _source(chat_id="123", thread_id="topic-a"))
        key_b = _badge_key_for_source(None, _source(chat_id="123", thread_id="topic-b"))
        assert key_a != key_b


class TestTopicIsolationEndToEnd:
    def test_two_topics_in_same_chat_do_not_cross_contaminate_switch_hint(self):
        state = {}
        source_a = _source(chat_id="123", thread_id="topic-a")
        source_b = _source(chat_id="123", thread_id="topic-b")

        # Topic A: Opus, then switches to Haiku (switch hint expected once).
        badge = _model_badge(
            state, _badge_key_for_source(None, source_a), "anthropic/claude-opus-4"
        )
        assert "switched" not in badge
        badge = _model_badge(
            state, _badge_key_for_source(None, source_a), "anthropic/claude-haiku-4-5"
        )
        assert "switched from claude-opus-4" in badge

        # Topic B has never seen any model before — first message must be
        # a plain badge, NOT "switched from Opus" leaked from topic A.
        badge = _model_badge(
            state, _badge_key_for_source(None, source_b), "anthropic/claude-haiku-4-5"
        )
        assert "switched" not in badge, badge

        # Second consecutive Haiku message in topic B must stay plain.
        badge = _model_badge(
            state, _badge_key_for_source(None, source_b), "anthropic/claude-haiku-4-5"
        )
        assert "switched" not in badge, badge
