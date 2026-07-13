"""Regression test: a terminal (non-retryable, generic) API error must not be
delivered to the user twice.

Live-verified 2026-07-13 (real Telegram round-trip): a blocked message
(egress_judge quiet-hard-block, HTTP 403) produced the identical "Aus
Sicherheitsgruenden nicht durchgeleitet" text twice in EVERY chat context
(twice in the originating topic, twice in the main chat) — independent of
topic routing (R2b), so this is a separate bug.

Root cause: agent/conversation_loop.py's generic non-retryable-error branch
calls ``agent._emit_status(f"... Non-retryable error (HTTP {status_code}): "
f"{_nonretryable_summary}")`` (which fans out through ``status_callback`` ->
the gateway's ``_prepare_gateway_status_message`` -> a real Telegram send)
and THEN, with no intervening work, ``return``s a dict whose
``final_response`` is built from that SAME ``_nonretryable_summary`` text --
which the gateway also sends, as the turn's final reply, independently
sanitized via ``_sanitize_gateway_final_response``. Both sanitizers rewrite
the same underlying text into the identical short block notice, so it goes
out over Telegram twice.

Fix: delete the redundant ``_emit_status`` call in the generic branch -- the
``final_response`` return already delivers this text exactly once. (The
content_policy_blocked and ssl_cert_verification sibling branches are left
alone: this bug report is specifically about the generic/egress_judge path.)
"""
import inspect

from agent import conversation_loop


def test_generic_nonretryable_branch_does_not_double_emit_via_status_callback():
    src = inspect.getsource(conversation_loop)

    # Anchor: the generic ("else") non-retryable-error branch's CLI-only
    # _vprint echo, which is NOT part of the duplicate (status_callback is
    # gateway-only; this call stays either way).
    cli_echo_marker = (
        'agent._vprint(f"{agent.log_prefix}❌ Non-retryable client error '
        '(HTTP {status_code}). Aborting."'
    )
    assert cli_echo_marker in src, (
        "The generic non-retryable-error CLI echo has moved or been renamed "
        "-- re-verify this test's anchor against agent/conversation_loop.py."
    )

    # The bug: an _emit_status(...) call with materially the same text,
    # positioned immediately before the CLI echo above and immediately
    # before a `return {"final_response": _nonretryable_summary, ...}` a few
    # lines later -- so the gateway's status_callback delivers this text as
    # a standalone message, and the caller ALSO delivers final_response as
    # the turn's reply. Both get sanitized into the identical short notice
    # by the gateway (gateway/run.py:_prepare_gateway_status_message /
    # _sanitize_gateway_final_response) and sent to Telegram -- twice.
    duplicate_emit_marker = (
        'f"❌ Non-retryable error (HTTP {status_code}): "'
    )
    assert duplicate_emit_marker not in src, (
        "Found the redundant agent._emit_status(...) call that duplicates "
        "the terminal non-retryable-error message. This _emit_status fires "
        "through status_callback (a real Telegram send on the gateway) with "
        "materially the same text that this same code path returns moments "
        "later as `final_response` (also sent, as the turn's reply) -- see "
        "the 2026-07-13 double-notice bug (2x identical block notices per "
        "chat context). Delete the _emit_status call for the generic branch; "
        "the final_response return already delivers this text once."
    )
