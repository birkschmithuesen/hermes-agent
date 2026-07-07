"""Regression tests for the TG model_badge prepend fix.

Root cause (2026-07-07): model_badge was set in stream metadata but only
prepended on the legacy non-streaming fallback path (already_sent=False).
The normal streaming path (_send_or_edit) never read it, so the badge
silently vanished on nearly every reply. Fixed by prepending once inside
_send_or_edit (covers first-send + every progressive edit) and separately
in the overflow-chunk-split path.
"""
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest

from gateway.stream_consumer import GatewayStreamConsumer


def _make_adapter(send_result=None, edit_result=None, max_length=4096):
    adapter = MagicMock()
    adapter.send = AsyncMock(
        return_value=send_result or SimpleNamespace(success=True, message_id="msg_1")
    )
    adapter.edit_message = AsyncMock(
        return_value=edit_result or SimpleNamespace(success=True)
    )
    adapter.MAX_MESSAGE_LENGTH = max_length
    return adapter


class TestModelBadgePrepend:
    @pytest.mark.asyncio
    async def test_first_send_includes_badge(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": "[🤖 claude-sonnet-4]"},
        )
        await consumer._send_or_edit("Hello world")

        adapter.send.assert_called_once()
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text.startswith("[🤖 claude-sonnet-4]\n"), sent_text
        assert "Hello world" in sent_text

    @pytest.mark.asyncio
    async def test_progressive_edit_keeps_badge(self):
        """A later edit (same message, streamed delta) must still carry the
        badge — otherwise the next edit cycle overwrites the bubble and
        silently strips it back out."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": "[🤖 claude-sonnet-4]"},
        )
        await consumer._send_or_edit("Hello")
        await consumer._send_or_edit("Hello world, continuing")

        adapter.edit_message.assert_called_once()
        edited_text = adapter.edit_message.call_args[1]["content"]
        assert edited_text.startswith("[🤖 claude-sonnet-4]\n"), edited_text

    @pytest.mark.asyncio
    async def test_no_badge_key_no_prepend(self):
        """No model_badge in metadata -> text passes through unchanged."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(adapter, "chat_123", metadata={"thread_id": "t1"})
        await consumer._send_or_edit("Hello world")

        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text == "Hello world"

    @pytest.mark.asyncio
    async def test_no_double_prepend_on_repeated_call(self):
        """Guard against the badge being prepended twice if _send_or_edit is
        ever called again with text that already carries it."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": "[🤖 claude-sonnet-4]"},
        )
        already_badged = "[🤖 claude-sonnet-4]\nHello world"
        await consumer._send_or_edit(already_badged)

        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text.count("[🤖 claude-sonnet-4]") == 1, sent_text
