"""Regression test for the image-only multi-message batch buffering leader
path in ``_prepare_inbound_message_text`` (text/vision-analyze routing, i.e.
``_decide_image_input_mode`` returns "text", not "native").

The leader used to call a nonexistent ``self.run_conversation(...)`` (and
reference an out-of-scope ``prior_messages``) instead of assigning the
enriched text and letting the normal ``return message_text`` contract handle
it. That raised AttributeError/NameError for every image-only Telegram
message routed through the text/vision-analyze path, breaking the whole
inbound flow for those events.
"""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "text"

    async def _fake_enrich(user_text, image_paths):
        return f"[vision: {', '.join(image_paths)}]"

    runner._enrich_message_with_vision = _fake_enrich
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _image_only_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=[path],
        media_types=["image/png"],
    )


@pytest.mark.asyncio
async def test_image_only_leader_returns_enriched_text_not_none():
    runner = _make_runner()
    source = _source("chat-a")

    result = await runner._prepare_inbound_message_text(
        event=_image_only_event(source, "/tmp/a.png"),
        source=source,
        history=[],
    )

    assert result is not None
    assert "/tmp/a.png" in result


@pytest.mark.asyncio
async def test_image_only_leader_does_not_raise():
    # Prior to the fix, this raised AttributeError (self.run_conversation
    # doesn't exist) chained with NameError (prior_messages undefined).
    runner = _make_runner()
    source = _source("chat-b")

    await runner._prepare_inbound_message_text(
        event=_image_only_event(source, "/tmp/b.png"),
        source=source,
        history=[],
    )
