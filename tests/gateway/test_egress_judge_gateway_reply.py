"""Regression test: egress_judge block errors must not surface as the generic
"provider failed after retries" reply.

Root cause (2026-07-09): the proxy's HTTP 403 body for an egress_judge block
carries a German ``error.message`` (reason/categories/decision_id), but
``_gateway_provider_error_reply`` only matched English provider-error phrasing
(auth/policy/rate-limit regexes), so the German text fell through to the
catch-all "The model provider failed after retries" reply and the user never
saw why the call was actually stopped.
"""
from gateway.run import _gateway_provider_error_reply


def test_egress_judge_block_ask_produces_security_reply():
    text = (
        "HTTP 403: Hermes egress_judge hat diesen Call gestoppt "
        "(Grund: learning-hard-block, Kategorien: ['gesundheit']). "
        "Prüfe Telegram für die Rückfrage. decision_id=ej1938fa2c1b8"
    )
    reply = _gateway_provider_error_reply(text)
    assert "Aus Sicherheitsgründen nicht durchgeleitet" in reply
    assert "learning-hard-block" in reply
    assert "gesundheit" in reply
    assert "decision_id=ej1938fa2c1b8" in reply
    assert "provider failed after retries" not in reply


def test_egress_judge_ask_reply_without_categories():
    text = (
        "HTTP 403: Hermes egress_judge unsicher (Grund: immature-or-unsure-ask). "
        "Prüfe Telegram. decision_id=ej1938fb0000"
    )
    reply = _gateway_provider_error_reply(text)
    assert "immature-or-unsure-ask" in reply
    assert "decision_id=ej1938fb0000" in reply


def test_egress_judge_truncated_message_still_flagged_as_security_block():
    # Simulates the 300-char truncation in _summarize_api_error cutting off
    # decision_id — must still read as a security block, never generic.
    text = "HTTP 403: Hermes egress_judge hat diesen Call gestoppt (Grund: x"
    reply = _gateway_provider_error_reply(text)
    assert "Sicherheitsgründen" in reply
    assert "provider failed after retries" not in reply


def test_non_egress_provider_error_unaffected():
    reply = _gateway_provider_error_reply("incorrect api key provided")
    assert "Aus Sicherheitsgründen" not in reply
    assert "authentication failed" in reply


def test_egress_judge_reply_is_channel_neutral():
    """Finding M1 (whole-branch review 2026-07-22): the egress-judge block
    reply must not tell the user to check Telegram specifically — the
    approval prompt can equally land on desktop, TUI, an SSE stream, or a
    Telegram topic. The plugin-side 403 text was already made
    channel-neutral; the core's rendering of it must match.
    """
    text = (
        "HTTP 403: Hermes egress_judge hat diesen Call gestoppt "
        "(Grund: learning-hard-block, Kategorien: ['gesundheit']). "
        "Prüfe Telegram für die Rückfrage. decision_id=ej1938fa2c1b8"
    )
    reply = _gateway_provider_error_reply(text)
    assert "Telegram" not in reply

    # Fallback path (detail regex didn't match, e.g. truncated decision_id)
    # must be equally channel-neutral.
    truncated = "HTTP 403: Hermes egress_judge hat diesen Call gestoppt (Grund: x"
    fallback_reply = _gateway_provider_error_reply(truncated)
    assert "Telegram" not in fallback_reply


def test_egress_judge_detail_regex_still_matches_after_wording_change():
    """The parsing regex (_GATEWAY_EGRESS_JUDGE_DETAIL_RE) must keep matching
    the plugin's raw 403 text — only the user-facing rendering may change."""
    from gateway.run import _GATEWAY_EGRESS_JUDGE_DETAIL_RE

    text = (
        "HTTP 403: Hermes egress_judge hat diesen Call gestoppt "
        "(Grund: learning-hard-block, Kategorien: ['gesundheit']). "
        "Prüfe Telegram für die Rückfrage. decision_id=ej1938fa2c1b8"
    )
    m = _GATEWAY_EGRESS_JUDGE_DETAIL_RE.search(text)
    assert m is not None
    assert m.group("reason") == "learning-hard-block"
    assert m.group("categories") == "['gesundheit']"
    assert m.group("decision_id") == "ej1938fa2c1b8"
