"""429 detail line: provider rate-limit headers + error code, never credentials."""
import httpx
import anthropic

from agent.turn_recovery import rate_limit_detail


def _err(headers, body):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "sk-ant-SECRET"})
    resp = httpx.Response(429, headers=headers, request=req, json=body)
    return anthropic.RateLimitError("Error", response=resp, body=body)


def test_names_the_limit_and_error_code_without_the_key():
    e = _err({"retry-after": "12", "anthropic-ratelimit-input-tokens-remaining": "0", "request-id": "req_1",
              "x-api-key": "sk-ant-SECRET"},
             {"type": "error", "error": {"type": "rate_limit_error", "message": "Error",
                                         "details": {"error_code": "enforced_spend_limit_reached"}}})
    out = rate_limit_detail(e)
    assert "retry-after=12" in out
    assert "anthropic-ratelimit-input-tokens-remaining=0" in out
    assert "error_code=enforced_spend_limit_reached" in out
    assert "SECRET" not in out


def test_plain_exception_does_not_raise():
    assert rate_limit_detail(RuntimeError("x")) == "no rate-limit headers"
