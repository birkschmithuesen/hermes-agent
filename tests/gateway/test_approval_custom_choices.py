"""Tests for a custom `choices` list flowing through the approval-notify
transports (Task 3 of the egress-judge session-rail migration).

The egress judge needs a three-way allow/mask/deny "Rueckfrage" instead of
the rail's default once/session/always/deny prompt. `approval_data` may now
carry an explicit `choices` list. Two transports must honor it:

- gateway/run.py's `_approval_notify_sync` -> send_exec_approval (Telegram
  and other chat-platform adapters).
- gateway/platforms/api_server.py's `_approval_notify` (SSE/API stream) --
  this one previously overwrote any caller-supplied `choices` key
  unconditionally via `_approval_event_choices(...)`, silently discarding a
  custom list. tui_gateway/server.py's `_emit_approval_request` already got
  this right (`if "choices" not in payload: ...`); this test file locks the
  same precedence into the API-server transport.

Both surfaces are exercised via AST inspection (like
tests/gateway/test_approval_prompt_redaction.py's TestApprovalCommandWiring)
because the send call sites are nested closures inside deeply-wired async
methods that are impractical to drive end-to-end in a unit test.
"""

import ast
import inspect


class TestApprovalEventChoicesFor:
    """Unit coverage for the new api_server.py helper."""

    def test_honors_explicit_choices_key(self):
        from gateway.platforms.api_server import _approval_event_choices_for

        payload = {"choices": ["allow", "mask", "deny"], "smart_denied": True}
        assert _approval_event_choices_for(payload) == ["allow", "mask", "deny"]

    def test_falls_back_to_capability_derived_default_when_absent(self):
        from gateway.platforms.api_server import _approval_event_choices_for

        payload = {"smart_denied": False, "allow_permanent": True}
        assert _approval_event_choices_for(payload) == [
            "once", "session", "always", "deny"
        ]

    def test_falls_back_respects_smart_denied(self):
        from gateway.platforms.api_server import _approval_event_choices_for

        payload = {"smart_denied": True}
        assert _approval_event_choices_for(payload) == ["once", "deny"]

    def test_falls_back_respects_allow_permanent_false(self):
        from gateway.platforms.api_server import _approval_event_choices_for

        payload = {"allow_permanent": False}
        assert _approval_event_choices_for(payload) == ["once", "session", "deny"]


class TestApprovalNotifyWiringApiServer:
    """AST check: _approval_notify must route through the new precedence
    helper instead of unconditionally overwriting `choices`."""

    def test_approval_notify_uses_choices_for_helper(self):
        from gateway.platforms import api_server

        source = inspect.getsource(api_server)
        tree = ast.parse(source)
        target_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_approval_notify":
                target_fn = node
                break
        assert target_fn is not None, "_approval_notify not found in api_server.py"

        seg = ast.get_source_segment(source, target_fn)
        assert "_approval_event_choices_for(" in seg, (
            "_approval_notify must resolve choices via _approval_event_choices_for(...) "
            "so a caller-supplied approval_data['choices'] is not silently discarded"
        )


class TestApprovalNotifySyncWiringGatewayRun:
    """AST check mirroring TestApprovalCommandWiring in
    test_approval_prompt_redaction.py: send_exec_approval must be called
    with choices=approval_data.get('choices')."""

    def test_chat_platform_path_threads_choices_to_adapter(self):
        import gateway.run as run

        tree = ast.parse(inspect.getsource(run))
        notify = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_approval_notify_sync"
        )
        call = next(
            node for node in ast.walk(notify)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_exec_approval"
        )
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "choices" in keywords, "send_exec_approval call must pass choices=..."
        value = keywords["choices"]
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Attribute) and value.func.attr == "get"
        assert isinstance(value.args[0], ast.Constant) and value.args[0].value == "choices"


class TestFormatExecApprovalFallbackChoices:
    """The plain-text fallback (used by adapters without button support)
    must list a custom choices set as typable commands instead of the
    default once/session/always/deny wording."""

    def test_default_wording_unchanged_when_choices_absent(self):
        from gateway.run import _format_exec_approval_fallback

        text = _format_exec_approval_fallback(
            "rm -rf /", "dangerous deletion", "/",
            allow_permanent=True, smart_denied=False,
        )
        assert "`/approve session`" in text
        assert "`/approve always`" in text
        assert "`/deny`" in text

    def test_custom_choices_rendered_as_typable_commands(self):
        from gateway.run import _format_exec_approval_fallback

        text = _format_exec_approval_fallback(
            "egress to claude", "Kategorien: zugangsdaten", "/",
            choices=["allow", "mask", "deny"],
        )
        assert "`/approve allow`" in text
        assert "`/approve mask`" in text
        assert "`/approve deny`" in text
        # default-only wording must not leak in when a custom list is given
        assert "session" not in text
        assert "always" not in text
