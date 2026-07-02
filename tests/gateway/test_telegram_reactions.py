"""Tests for Telegram message reactions tied to processing lifecycle hooks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


def _make_adapter(**extra_env):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="fake-token")
    adapter._bot = AsyncMock()
    adapter._bot.set_message_reaction = AsyncMock()
    return adapter


def _make_event(chat_id: str = "123", message_id: str = "456") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type="private",
            user_id="42",
            user_name="TestUser",
        ),
        message_id=message_id,
    )


# ── _reactions_enabled ───────────────────────────────────────────────


def test_reactions_disabled_by_default(monkeypatch):
    """Telegram reactions should be disabled by default."""
    monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is False


def test_reactions_enabled_when_set_true(monkeypatch):
    """Setting TELEGRAM_REACTIONS=true enables reactions."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    assert adapter._reactions_enabled() is True


# ── _set_reaction ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_reaction_calls_bot_api(monkeypatch):
    """_set_reaction should call bot.set_message_reaction with correct args."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()

    result = await adapter._set_reaction("123", "456", "\U0001f440")

    assert result is True
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction="\U0001f440",
    )


# ── on_processing_start ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_start_handles_missing_ids(monkeypatch):
    """Should handle events without chat_id or message_id gracefully."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(chat_id=None),
        message_id=None,
    )

    await adapter.on_processing_start(event)

    adapter._bot.set_message_reaction.assert_not_awaited()


# ── on_processing_complete ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_clears_reaction(monkeypatch):
    """Cancelled processing should clear the in-progress reaction.

    Without this clear, the 👀 reaction lingers on the user's message
    indefinitely (until another agent run swaps it for 👍/👎). On a
    ``/stop`` that ends a session, that reaction never gets cleaned up.
    """
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    event = _make_event()

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    # set_message_reaction with reaction=None clears all reactions on the
    # message (Bot API documented semantics; equivalent to Bot API 10.0's
    # deleteMessageReaction but works on PTB 22.6 already).
    adapter._bot.set_message_reaction.assert_awaited_once_with(
        chat_id=123,
        message_id=456,
        reaction=None,
    )


@pytest.mark.asyncio
async def test_clear_reactions_handles_api_error_gracefully(monkeypatch):
    """API errors during clear should not propagate."""
    monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    adapter = _make_adapter()
    adapter._bot.set_message_reaction = AsyncMock(side_effect=RuntimeError("no perms"))

    result = await adapter._clear_reactions("123", "456")
    assert result is False


# ── config.py bridging ───────────────────────────────────────────────


def test_config_bridges_telegram_reactions(monkeypatch, tmp_path):
    """gateway/config.py bridges telegram.reactions to TELEGRAM_REACTIONS env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reactions": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Use setenv (not delenv) so monkeypatch registers cleanup even when
    # the var doesn't exist yet — load_gateway_config will overwrite it.
    monkeypatch.setenv("TELEGRAM_REACTIONS", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTIONS") == "true"


def test_config_reactions_env_takes_precedence(monkeypatch, tmp_path):
    """Env var should take precedence over config.yaml for reactions."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reactions": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_REACTIONS", "false")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTIONS") == "false"


# ── Inbound reaction feedback ────────────────────────────────────────
#
# User reacts with an emoji on a BOT-authored message → the adapter routes
# that reaction into the normal pipeline as a synthetic MessageType.REACTION
# event so the agent observes it as a feedback/confirmation signal. Gated OFF
# by default behind telegram.reaction_feedback (TELEGRAM_REACTION_FEEDBACK).


def _make_feedback_adapter(monkeypatch, *, feedback=True, reactions=True, bot_id=99999):
    """Adapter wired for inbound-reaction tests with handle_message mocked."""
    if reactions:
        monkeypatch.setenv("TELEGRAM_REACTIONS", "true")
    else:
        monkeypatch.delenv("TELEGRAM_REACTIONS", raising=False)
    if feedback:
        monkeypatch.setenv("TELEGRAM_REACTION_FEEDBACK", "true")
    else:
        monkeypatch.delenv("TELEGRAM_REACTION_FEEDBACK", raising=False)

    adapter = _make_adapter()
    adapter._bot.id = bot_id
    adapter.handle_message = AsyncMock()
    return adapter


def _emoji(emoji: str):
    return SimpleNamespace(type="emoji", emoji=emoji)


def _custom_emoji(cid: str = "555"):
    return SimpleNamespace(type="custom_emoji", custom_emoji_id=cid)


def _make_reaction_update(
    *,
    old=None,
    new=None,
    user_id=42,
    chat_id=123,
    message_id=456,
    chat_type="private",
    update_id=9001,
):
    mr = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title=None, full_name="Test User"),
        message_id=message_id,
        user=SimpleNamespace(id=user_id, full_name="TestUser", is_bot=False),
        old_reaction=old or [],
        new_reaction=new or [],
    )
    return SimpleNamespace(message_reaction=mr, update_id=update_id)


@pytest.mark.asyncio
async def test_reaction_added_builds_event(monkeypatch):
    """A newly-added 👍 reaction → synthetic REACTION event on our message."""
    adapter = _make_feedback_adapter(monkeypatch)
    update = _make_reaction_update(new=[_emoji("\U0001f44d")])

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.text == "reaction:added:\U0001f44d"
    assert event.message_type == MessageType.REACTION
    assert event.reply_to_is_own_message is True
    assert event.reply_to_message_id == "456"


@pytest.mark.asyncio
async def test_reaction_removed_builds_event(monkeypatch):
    """Removing a previously-set 👍 → reaction:removed:👍."""
    adapter = _make_feedback_adapter(monkeypatch)
    update = _make_reaction_update(old=[_emoji("\U0001f44d")], new=[])

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.text == "reaction:removed:\U0001f44d"
    assert event.message_type == MessageType.REACTION


@pytest.mark.asyncio
async def test_reaction_added_and_removed_emits_added_first(monkeypatch):
    """Swapping ❌→✅ reports the added emoji before the removed one."""
    adapter = _make_feedback_adapter(monkeypatch)
    update = _make_reaction_update(old=[_emoji("❌")], new=[_emoji("✅")])

    await adapter._handle_message_reaction(update, None)

    event = adapter.handle_message.call_args.args[0]
    assert event.text == "reaction:added:✅\nreaction:removed:❌"


@pytest.mark.asyncio
async def test_reaction_feedback_disabled_by_default(monkeypatch):
    """With no flags set, inbound reactions must not dispatch."""
    adapter = _make_feedback_adapter(monkeypatch, feedback=False, reactions=False)
    update = _make_reaction_update(new=[_emoji("\U0001f44d")])

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_feedback_requires_subflag(monkeypatch):
    """Outbound reactions on but reaction_feedback off → no inbound dispatch."""
    adapter = _make_feedback_adapter(monkeypatch, feedback=False, reactions=True)
    assert adapter._reaction_feedback_enabled() is False
    update = _make_reaction_update(new=[_emoji("\U0001f44d")])

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_own_reaction_ignored(monkeypatch):
    """The bot's own outbound reaction must not loop back as feedback."""
    adapter = _make_feedback_adapter(monkeypatch, bot_id=42)
    update = _make_reaction_update(new=[_emoji("\U0001f440")], user_id=42)

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_emoji_graceful_placeholder(monkeypatch):
    """Custom/premium emoji reactions degrade to a placeholder, no crash."""
    adapter = _make_feedback_adapter(monkeypatch)
    update = _make_reaction_update(new=[_custom_emoji()])

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.text == "reaction:added:custom"
    assert event.message_type == MessageType.REACTION


@pytest.mark.asyncio
async def test_group_reaction_ignored_when_not_bot_message(monkeypatch):
    """Group reactions on messages we can't attribute to the bot are ignored."""
    adapter = _make_feedback_adapter(monkeypatch)
    update = _make_reaction_update(new=[_emoji("\U0001f44d")], chat_type="supergroup")

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_op_when_reaction_diff_empty(monkeypatch):
    """No added/removed emoji (identical old/new) → nothing dispatched."""
    adapter = _make_feedback_adapter(monkeypatch)
    same = [_emoji("\U0001f44d")]
    update = _make_reaction_update(old=list(same), new=list(same))

    await adapter._handle_message_reaction(update, None)

    adapter.handle_message.assert_not_awaited()


def test_config_bridges_telegram_reaction_feedback(monkeypatch, tmp_path):
    """gateway/config.py bridges telegram.reaction_feedback → env var."""
    import yaml
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump({
        "telegram": {
            "reaction_feedback": True,
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_REACTION_FEEDBACK", "")

    from gateway.config import load_gateway_config
    load_gateway_config()

    import os
    assert os.getenv("TELEGRAM_REACTION_FEEDBACK") == "true"
