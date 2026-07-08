"""Regression tests: the fresh-final send path must carry the model badge.

Root cause (2026-07-08): ``_try_fresh_final`` calls ``adapter.send`` directly
with the caller-supplied text and no badge-prepend guard of its own.  Every
production caller currently reaches it through ``_send_or_edit`` (which
prepends the badge at its top), so the badge happens to be present today —
BUT the guard living only in the caller is fragile: any send site that reaches
``_try_fresh_final`` without going through ``_send_or_edit`` first (a future
caller, a refactor, or a direct finalize) would drop the badge on the single
most-visible message of the turn (the final answer).

The fix routes the fresh-final send through the same idempotent badge choke
point as the streaming path, so the guard no longer depends on the caller.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer

BADGE = "[🤖 claude-sonnet-4 · ⚡ high · 🔒 local]"


def _make_adapter():
    adapter = MagicMock()
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="fresh_1")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True)
    )
    adapter.delete_message = AsyncMock(return_value=True)
    adapter.MAX_MESSAGE_LENGTH = 4096
    return adapter


class TestFreshFinalCarriesBadge:
    @pytest.mark.asyncio
    async def test_fresh_final_send_includes_badge(self):
        """The fresh-final send (the turn-final message the user actually
        sees) must be prefixed with the model badge."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        ok = await consumer._try_fresh_final("Hello world")
        assert ok is True
        adapter.send.assert_called_once()
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text.startswith(f"{BADGE}\n"), sent_text
        assert "Hello world" in sent_text

    @pytest.mark.asyncio
    async def test_fresh_final_does_not_double_prepend(self):
        """Idempotent: text that already carries the badge is not re-stamped."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        already_badged = f"{BADGE}\nHello world"
        await consumer._try_fresh_final(already_badged)
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text.count(BADGE) == 1, sent_text

    @pytest.mark.asyncio
    async def test_fresh_final_no_badge_key_passes_through(self):
        """No model_badge in metadata -> text is sent unchanged."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"thread_id": "t1"},
        )
        await consumer._try_fresh_final("Hello world")
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text == "Hello world"


class TestSendDraftFrameCarriesBadge:
    """Audit target from the brief: _send_draft_frame (~L1189) calls
    adapter.send_draft directly with the caller-supplied text. Native draft
    streaming is a real, not-rare delivery path (Telegram DM drafts, etc.) —
    if it bypasses the badge choke point, the badge silently drops on any
    platform/config where draft streaming is active."""

    @pytest.mark.asyncio
    async def test_first_draft_frame_includes_badge(self):
        adapter = _make_adapter()
        adapter.send_draft = AsyncMock(
            return_value=SimpleNamespace(success=True)
        )
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        consumer._draft_id = 1
        ok = await consumer._send_draft_frame("Hello world")
        assert ok is True
        sent_text = adapter.send_draft.call_args[1]["content"]
        assert sent_text.startswith(f"{BADGE}\n"), sent_text

    @pytest.mark.asyncio
    async def test_draft_frame_does_not_double_prepend(self):
        adapter = _make_adapter()
        adapter.send_draft = AsyncMock(
            return_value=SimpleNamespace(success=True)
        )
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        consumer._draft_id = 1
        already_badged = f"{BADGE}\nHello world"
        await consumer._send_draft_frame(already_badged)
        sent_text = adapter.send_draft.call_args[1]["content"]
        assert sent_text.count(BADGE) == 1, sent_text

    @pytest.mark.asyncio
    async def test_subsequent_draft_frame_not_rebadged(self):
        """Once a message is already underway (_already_sent True), later
        frames must not get a second badge prepended."""
        adapter = _make_adapter()
        adapter.send_draft = AsyncMock(
            return_value=SimpleNamespace(success=True)
        )
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        consumer._draft_id = 1
        consumer._already_sent = True
        consumer._message_id = "draft_msg"
        await consumer._send_draft_frame("Hello world continued")
        sent_text = adapter.send_draft.call_args[1]["content"]
        assert sent_text == "Hello world continued"


class TestFlushSegmentTailCarriesBadge:
    """Audit target from the brief: _flush_segment_tail_on_edit_failure
    (~L1229) calls adapter.send directly with the un-prepended tail. Fires
    when an edit fails right at a tool-boundary segment break — a realistic
    path, not a rare corner case."""

    @pytest.mark.asyncio
    async def test_flush_tail_includes_badge_on_first_message(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        consumer._accumulated = "Some partial tail text"
        consumer._fallback_final_send = True  # skip cursor-strip edit attempt
        await consumer._flush_segment_tail_on_edit_failure()
        adapter.send.assert_called_once()
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text.startswith(f"{BADGE}\n"), sent_text

    @pytest.mark.asyncio
    async def test_flush_tail_not_rebadged_when_already_sent(self):
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat_123", metadata={"model_badge": BADGE},
        )
        consumer._accumulated = "Some partial tail text"
        consumer._fallback_final_send = True
        consumer._already_sent = True
        await consumer._flush_segment_tail_on_edit_failure()
        sent_text = adapter.send.call_args[1]["content"]
        assert sent_text == "Some partial tail text"
