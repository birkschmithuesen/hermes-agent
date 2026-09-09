"""Regression tests: the model badge must survive EVERY adapter send path.

Prior badge fixes covered `_send_or_edit` (first send + progressive edits) and
the overflow-split first chunk. But several other Telegram delivery methods call
`self.adapter.send(...)` / `adapter.send_draft(...)` directly, and each was an
independent chance to drop the badge:

  * `_try_fresh_final`  — the "fresh final" path (send a new completed message +
    delete the stale streaming preview). This is the FINAL message of a turn on
    Telegram (prefers_fresh_final_streaming), i.e. the single most common
    message a user sees.
  * `_send_draft_frame` — native draft-frame streaming.
  * `_flush_segment_tail_on_edit_failure` — flush of un-sent tail content at a
    tool boundary after an edit failure. Builds its text straight from
    `self._accumulated`, bypassing `_send_or_edit`'s prepend entirely.

These tests pin the invariant "the first user-visible message of a turn carries
the badge on whichever send path delivered it" so the bug can never regress via
a newly-added send call site.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

BADGE = "[🤖 test-model · ⚡ high · 🔒 local]"


def _make_adapter(*, supports_delete: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.REQUIRES_EDIT_FINALIZE = False
    adapter.MAX_MESSAGE_LENGTH = 4096
    adapter.send = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="msg_1")
    )
    adapter.edit_message = AsyncMock(
        return_value=SimpleNamespace(success=True, message_id="msg_1")
    )
    adapter.send_draft = AsyncMock(return_value=SimpleNamespace(success=True))
    if supports_delete:
        adapter.delete_message = AsyncMock(return_value=True)
    return adapter


class TestFreshFinalCarriesBadge:
    @pytest.mark.asyncio
    async def test_prefers_fresh_final_send_includes_badge(self):
        """Telegram's fresh-final path (finalize + prefers_fresh_final_streaming)
        must deliver the completed reply WITH the badge."""
        adapter = _make_adapter()
        adapter.send.side_effect = [
            SimpleNamespace(success=True, message_id="preview"),
            SimpleNamespace(success=True, message_id="fresh_final"),
        ]
        # Instance-dict hook so _adapter_prefers_fresh_final detects it and the
        # fresh-final branch fires on finalize.
        adapter.prefers_fresh_final_streaming = MagicMock(return_value=True)
        consumer = GatewayStreamConsumer(
            adapter, "chat", metadata={"model_badge": BADGE},
        )
        await consumer._send_or_edit("Hello")  # establishes preview + message_id
        await consumer._send_or_edit("Hello world", finalize=True)

        # Second send == the fresh-final delivery.
        assert adapter.send.call_count == 2, adapter.send.call_args_list
        fresh_content = adapter.send.call_args_list[1].kwargs["content"]
        assert fresh_content.startswith(f"{BADGE}\n"), fresh_content
        assert "Hello world" in fresh_content


class TestDraftFrameCarriesBadge:
    @pytest.mark.asyncio
    async def test_draft_frame_includes_badge(self):
        """Native draft-frame streaming must carry the badge on screen."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat", metadata={"model_badge": BADGE},
        )
        consumer._use_draft_streaming = True
        consumer._draft_id = 42
        # Draft path only taken when no edit message is established + not final.
        await consumer._send_or_edit("Drafting an answer")

        adapter.send_draft.assert_awaited_once()
        draft_content = adapter.send_draft.call_args.kwargs["content"]
        assert draft_content.startswith(f"{BADGE}\n"), draft_content
        assert "Drafting an answer" in draft_content


class TestSegmentTailFlushCarriesBadge:
    @pytest.mark.asyncio
    async def test_segment_tail_flush_includes_badge_when_first_visible(self):
        """When an edit failure forces a tool-boundary tail flush and nothing
        has been shown yet, that first message must carry the badge."""
        adapter = _make_adapter()
        consumer = GatewayStreamConsumer(
            adapter, "chat", metadata={"model_badge": BADGE},
        )
        consumer._accumulated = "Un-sent tail content after an edit failure."
        consumer._fallback_final_send = True  # skip the cursor-strip edit
        await consumer._flush_segment_tail_on_edit_failure()

        adapter.send.assert_awaited_once()
        tail_content = adapter.send.call_args.kwargs["content"]
        assert tail_content.startswith(f"{BADGE}\n"), tail_content
        assert "Un-sent tail content" in tail_content
