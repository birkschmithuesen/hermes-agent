"""Tests for the Telegram ej:* callback dispatch (egress_judge feedback).

Root cause (2026-07-09): egress_judge/notify.py sends block_ask/pass_confirm
messages with ej:allow/ej:mask/ej:block/ej:ok/ej:wrong callback_data, but no
handler in the gateway ever reacted to the "ej:" prefix — record_feedback()
was never called, so the counters.json maturity file never got created.
Mirrors test_telegram_clarify_buttons.py's mocking pattern.
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(data, user_id="777"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.text = "🛑 *Egress gestoppt* — heikle Kategorie erkannt."
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _fake_egress_judge_module(fn):
    """Build a fake egress_judge.pipeline module so the handler's dynamic
    `from egress_judge.pipeline import record_feedback_from_callback_using_config`
    resolves to our stub instead of touching real profile files on disk."""
    pipeline_mod = types.ModuleType("egress_judge.pipeline")
    pipeline_mod.record_feedback_from_callback_using_config = fn
    pkg = types.ModuleType("egress_judge")
    pkg.pipeline = pipeline_mod
    return pkg, pipeline_mod


@pytest.mark.asyncio
async def test_ej_allow_records_positive_feedback_and_edits_message():
    fake_fn = MagicMock(return_value=["gesundheit"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()
    query = _make_query("ej:allow:ej1938fa2c1b8")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, context)

    fake_fn.assert_called_once_with("ej1938fa2c1b8", True)
    query.answer.assert_called_once()
    assert "Durchgelassen" in query.answer.call_args[1]["text"]
    query.edit_message_text.assert_called_once()
    assert "Verarbeitet" in query.edit_message_text.call_args[1]["text"]


@pytest.mark.asyncio
async def test_ej_block_records_negative_feedback():
    fake_fn = MagicMock(return_value=["finanzen"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()
    query = _make_query("ej:block:ej1938fb0000")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, context)

    fake_fn.assert_called_once_with("ej1938fb0000", False)


@pytest.mark.asyncio
async def test_ej_mask_treated_as_positive_feedback():
    fake_fn = MagicMock(return_value=["sonstiges"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()
    query = _make_query("ej:mask:ej1938fc0000")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, context)

    fake_fn.assert_called_once_with("ej1938fc0000", True)


@pytest.mark.asyncio
async def test_ej_ok_and_wrong_map_correctly():
    fake_fn = MagicMock(return_value=["person"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()

    for data, expected in (("ej:ok:ejA", True), ("ej:wrong:ejB", False)):
        fake_fn.reset_mock()
        query = _make_query(data)
        update = MagicMock()
        update.callback_query = query
        context = MagicMock()
        with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
             patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)
        decision_id = data.split(":")[2]
        fake_fn.assert_called_once_with(decision_id, expected)


@pytest.mark.asyncio
async def test_ej_unknown_decision_still_answers_gracefully():
    fake_fn = MagicMock(return_value=None)  # decision_id not found in log
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()
    query = _make_query("ej:allow:ej-not-found")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, context)

    query.answer.assert_called_once()
    assert "nicht im Log gefunden" in query.answer.call_args[1]["text"]
    # Message is still edited/marked processed even when lookup failed.
    query.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_ej_unauthorized_user_rejected_and_no_feedback_recorded():
    fake_fn = MagicMock(return_value=["gesundheit"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()

    class _DenyRunner:
        async def _handle_message(self, event):
            return None

        def _is_user_authorized(self, source):
            return False

    adapter._message_handler = _DenyRunner()._handle_message

    query = _make_query("ej:allow:ej1938fa2c1b8", user_id="999")
    query.message.chat.type = "private"
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}):
        await adapter._handle_callback_query(update, context)

    fake_fn.assert_not_called()
    query.answer.assert_called_once()
    assert "not authorized" in query.answer.call_args[1]["text"].lower()


@pytest.mark.asyncio
async def test_ej_invalid_verb_rejected():
    fake_fn = MagicMock(return_value=["gesundheit"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)

    adapter = _make_adapter()
    query = _make_query("ej:bogus:ej1938fa2c1b8")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, context)

    fake_fn.assert_not_called()
    query.answer.assert_called_once()
    assert "invalid" in query.answer.call_args[1]["text"].lower()


def _fake_pending_module(record):
    """egress_judge.pending stub so the handler's dynamic
    `from egress_judge.pending import load_pending` resolves without touching
    real profile files."""
    pending_mod = types.ModuleType("egress_judge.pending")
    pending_mod.load_pending = MagicMock(return_value=record)
    return pending_mod


@pytest.mark.asyncio
async def test_ej_allow_fires_resume_post():
    """R3: tapping Durchlassen must POST to the proxy resume endpoint, reading
    resume_port + thread_id from the pending record (drift-proof, in-topic)."""
    import asyncio

    fake_fn = MagicMock(return_value=["finanzen"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)
    pending_mod = _fake_pending_module({
        "decision_id": "ejR3allow", "chat_id": "570261709",
        "thread_id": "7", "resume_port": 28765,
    })
    pkg.pending = pending_mod

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        m = MagicMock()
        m.read.return_value = b"{}"
        return m

    adapter = _make_adapter()
    query = _make_query("ej:allow:ejR3allow")
    query.message.message_thread_id = 7
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod,
                                  "egress_judge.pending": pending_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
         patch("urllib.request.urlopen", _fake_urlopen):
        await adapter._handle_callback_query(update, context)
        # Drain the fire-and-forget resume task the handler scheduled.
        tasks = list(getattr(adapter, "_ej_resume_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks)

    assert "url" in captured, "resume POST was never fired"
    assert "/_egress_judge/resume/ejR3allow" in captured["url"]
    assert "variant=allow" in captured["url"]
    assert "thread_id=7" in captured["url"]
    assert captured["method"] == "POST"
    pending_mod.load_pending.assert_called_once_with("ejR3allow")


@pytest.mark.asyncio
async def test_ej_block_does_not_fire_resume_post():
    """block/ok/wrong are feedback-only — they must NOT re-send."""
    import asyncio

    fake_fn = MagicMock(return_value=["finanzen"])
    pkg, pipeline_mod = _fake_egress_judge_module(fake_fn)
    pending_mod = _fake_pending_module({"decision_id": "x", "resume_port": 28765})
    pkg.pending = pending_mod

    called = {"urlopen": False}

    def _fake_urlopen(req, timeout=None):
        called["urlopen"] = True
        m = MagicMock()
        m.read.return_value = b"{}"
        return m

    adapter = _make_adapter()
    query = _make_query("ej:block:ejR3block")
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()

    with patch.dict(sys.modules, {"egress_judge": pkg, "egress_judge.pipeline": pipeline_mod,
                                  "egress_judge.pending": pending_mod}), \
         patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
         patch("urllib.request.urlopen", _fake_urlopen):
        await adapter._handle_callback_query(update, context)
        tasks = list(getattr(adapter, "_ej_resume_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks)

    assert called["urlopen"] is False
    pending_mod.load_pending.assert_not_called()
