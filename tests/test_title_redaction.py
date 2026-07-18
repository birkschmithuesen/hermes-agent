"""RED->GREEN tests for session-title secret redaction (deferred P0 item,
Task 6 Part B of docs/superpowers/plans/2026-07-12-p0-restore-peace.md).

Incident: a temporary sudo password was copied verbatim into a session title
in state.db (2026-07-08, purged). This patch closes the *future*-leak class:
generate_title() must redact secret-context tokens and key-like strings from
whatever the (LLM-written) title text turns out to be, before it is returned
for persistence — the deterministic filter sits on the sink, not on the LLM.
"""

from unittest.mock import MagicMock, patch

from agent.title_generator import _redact_title, generate_title


def _mock_llm_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


class TestRedactTitleUnit:
    """Direct unit tests against the deterministic redaction filter."""

    def test_password_context_redacted(self):
        assert "12345678" not in _redact_title(
            "gib dir sudo mit dem PW: 12345678 Tailscale Setup"
        )

    def test_password_equals_context_redacted(self):
        title = _redact_title("Setup script password=hunter2plaintext done")
        assert "hunter2plaintext" not in title

    def test_pem_block_redacted(self):
        title = _redact_title(
            "Key -----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY----- saved"
        )
        assert "MIIB" not in title
        assert "-----BEGIN" not in title

    def test_api_key_redacted(self):
        title = _redact_title("added key sk-abc123def456ghi789jkl012 to env")
        assert "sk-abc123def456ghi789jkl012" not in title

    def test_jwt_redacted(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        title = _redact_title(f"Sharing token {jwt} for testing")
        assert jwt not in title

    def test_normal_title_untouched_byte_for_byte(self):
        t = "Tailscale Setup Server Access"
        assert _redact_title(t) == t

    def test_benign_title_with_colon_untouched(self):
        t = "Recipe idea: Berlin-style Currywurst"
        assert _redact_title(t) == t


class TestGenerateTitleAppliesRedaction:
    """Integration: generate_title() must redact the LLM's raw output before
    returning it — the filter wraps the sink, independent of what the LLM
    (which is itself a hot-path caller, so no LLM redaction call here) wrote.
    """

    def test_generate_title_redacts_leaked_password(self):
        with patch(
            "agent.title_generator.call_llm",
            return_value=_mock_llm_response("sudo PW: 12345678 Tailscale Setup"),
        ):
            title = generate_title("set up tailscale", "use sudo with the password")
        assert title is not None
        assert "12345678" not in title

    def test_generate_title_redacts_api_key(self):
        with patch(
            "agent.title_generator.call_llm",
            return_value=_mock_llm_response(
                "Added key sk-abc123def456ghi789jkl012 to env"
            ),
        ):
            title = generate_title("add my key", "done, added it")
        assert title is not None
        assert "sk-abc123def456ghi789jkl012" not in title

    def test_generate_title_benign_unchanged(self):
        with patch(
            "agent.title_generator.call_llm",
            return_value=_mock_llm_response("Debugging Python Import Errors"),
        ):
            title = generate_title("help me fix this import", "Sure, let me check...")
        assert title == "Debugging Python Import Errors"
