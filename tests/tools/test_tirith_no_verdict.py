"""A non-zero tirith exit with no report is a scanner failure, not a finding.

Regression cover for the incident where every terminal command in an
autonomous run — down to ``pwd`` — was refused with "security issue detected".
The installed tirith binary required a newer glibc than the host provided, so
the dynamic linker printed its error on stderr and exited 1. Exit 1 is also
tirith's "blocked" code, so the wrapper read a loader crash as a unanimous
security verdict against every command, and the non-interactive session then
failed closed on all of them.

The distinguishing signal is stdout: a real verdict always carries tirith's
JSON report, a binary that never started carries nothing.
"""

from unittest.mock import MagicMock, patch

import pytest

import tools.tirith_security as _tirith_mod
from tools.approval import _format_tirith_description
from tools.tirith_security import check_command_security

GLIBC_ERROR = (
    "/home/u/.hermes/bin/tirith: /lib/x86_64-linux-gnu/libc.so.6: "
    "version `GLIBC_2.32' not found (required by tirith)"
)


@pytest.fixture(autouse=True)
def _reset_module_state():
    _tirith_mod._resolved_path = "tirith"
    _tirith_mod._install_thread = None
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False
    _tirith_mod._warned_messages.clear()
    yield
    _tirith_mod._resolved_path = None
    _tirith_mod._install_thread = None
    _tirith_mod._crash_count = 0
    _tirith_mod._circuit_open = False
    _tirith_mod._warned_messages.clear()


def _cfg(fail_open=True):
    return {"tirith_enabled": True, "tirith_path": "tirith",
            "tirith_timeout": 5, "tirith_fail_open": fail_open}


def _run(returncode, stdout="", stderr=""):
    cp = MagicMock()
    cp.returncode, cp.stdout, cp.stderr = returncode, stdout, stderr
    return cp


class TestNonZeroExitWithoutReport:
    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_loader_failure_does_not_block_a_harmless_command(self, cfg, run):
        """The exact incident: exit 1 + empty stdout + glibc error on stderr."""
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout="", stderr=GLIBC_ERROR)

        result = check_command_security("pwd")

        assert result["action"] == "allow"
        assert result["findings"] == []

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_summary_says_scanner_failed_not_security_issue(self, cfg, run):
        """The message must not read as a finding against the command."""
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout="", stderr=GLIBC_ERROR)

        summary = check_command_security("pwd")["summary"]

        assert "no verdict" in summary
        assert "GLIBC_2.32" in summary  # the operator gets the real cause
        assert "security issue detected" not in summary

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_counts_toward_circuit_breaker(self, cfg, run):
        """A permanently broken binary must stop being consulted."""
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout="", stderr=GLIBC_ERROR)

        for _ in range(_tirith_mod._CRASH_LIMIT):
            check_command_security("pwd")

        assert _tirith_mod._circuit_open is True

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_fail_closed_operator_still_blocks_but_says_why(self, cfg, run):
        """Opting into fail-closed keeps the block; only the reason changes."""
        cfg.return_value = _cfg(fail_open=False)
        run.return_value = _run(1, stdout="", stderr=GLIBC_ERROR)

        result = check_command_security("pwd")

        assert result["action"] == "block"
        assert "no verdict" in result["summary"]
        assert "fail-closed" in result["summary"]

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_warn_exit_without_report_is_also_a_failure(self, cfg, run):
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(2, stdout="", stderr="")

        assert check_command_security("pwd")["action"] == "allow"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_whitespace_only_stdout_counts_as_no_report(self, cfg, run):
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout="   \n", stderr=GLIBC_ERROR)

        assert check_command_security("pwd")["action"] == "allow"


class TestGenuineVerdictsStillBlock:
    """The security boundary must be intact: a real report still blocks."""

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_real_finding_still_blocks(self, cfg, run):
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout=(
            '{"findings": [{"rule_id": "homograph_url", "severity": "high",'
            ' "title": "Homograph URL"}], "summary": "homograph detected"}'))

        result = check_command_security("curl http://g\u043eogle.com")

        assert result["action"] == "block"
        assert result["summary"] == "homograph detected"
        assert len(result["findings"]) == 1

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_block_with_unparseable_but_present_report_still_blocks(self, cfg, run):
        """Corrupt JSON is a degraded verdict, not an absent one."""
        cfg.return_value = _cfg(fail_open=True)
        run.return_value = _run(1, stdout="{not json", stderr="")

        result = check_command_security("curl http://evil.example")

        assert result["action"] == "block"
        assert result["summary"] == "security issue detected (details unavailable)"

    @patch("tools.tirith_security.subprocess.run")
    @patch("tools.tirith_security._load_security_config")
    def test_clean_scan_still_allows_and_resets_breaker(self, cfg, run):
        cfg.return_value = _cfg(fail_open=True)
        _tirith_mod._crash_count = 2
        run.return_value = _run(0, stdout='{"findings": [], "summary": ""}')

        assert check_command_security("pwd")["action"] == "allow"
        assert _tirith_mod._crash_count == 0


class TestDescriptionWording:
    """Part 2 of the report: 'no answer' must not read as 'security issue'."""

    def test_no_findings_and_no_summary_is_not_called_a_security_issue(self):
        desc = _format_tirith_description(
            {"action": "block", "findings": [], "summary": ""})

        assert "security issue detected" not in desc
        assert "no findings" in desc

    def test_real_summary_is_still_shown_verbatim(self):
        desc = _format_tirith_description(
            {"action": "block", "findings": [], "summary": "homograph detected"})

        assert desc == "Security scan: homograph detected"

    def test_structured_findings_are_still_rendered(self):
        desc = _format_tirith_description({
            "action": "block",
            "findings": [{"severity": "HIGH", "title": "Homograph URL",
                          "description": "cyrillic lookalike"}],
            "summary": ""})

        assert "Homograph URL" in desc
        assert "cyrillic lookalike" in desc
