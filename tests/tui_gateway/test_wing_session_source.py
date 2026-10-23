"""Source ``wing`` (Wing phone client) behaves like ``desktop`` everywhere except the platform hint and the
desktop-only client tools (spec docs/superpowers/specs/2026-10-13-wing-client-source-design.md, section 4 in
the Wing repo; card t_9d9bcf31)."""

import inspect
import re
from pathlib import Path

import pytest

from tui_gateway import server  # noqa: F401  (binds the method_ctx modules below)
from tui_gateway import session_reaper, session_workdir

import hermes_state_maintenance
from agent import coding_context, tool_guardrails
from agent.session_source import ATTENDED_SOURCES, DESKTOP_LIKE_SOURCES, UI_TRANSPORT_SOURCES, WING_SOURCE
from gateway.session_context import NON_MESSAGING_SESSION_SURFACES

ROOT = Path(__file__).resolve().parents[2]


def test_vocabulary():
    assert WING_SOURCE == "wing"
    assert DESKTOP_LIKE_SOURCES == frozenset({"desktop", "wing"})


@pytest.mark.parametrize("source", ["desktop", "wing"])
def test_wing_shares_desktop_membership(source):
    assert source in UI_TRANSPORT_SOURCES
    assert source in ATTENDED_SOURCES
    assert source in tool_guardrails._ATTENDED_PLATFORMS
    assert source in coding_context.INTERACTIVE_CODING_PLATFORMS
    assert source in NON_MESSAGING_SESSION_SURFACES
    assert source in session_reaper._ORPHAN_SWEEP_SOURCES
    assert source in session_workdir._LAUNCH_CWD_NOT_A_WORKSPACE
    sweep_default = inspect.signature(
        hermes_state_maintenance.SessionMaintenanceMixin.sweep_orphaned_sessions).parameters["sources"].default
    assert source in sweep_default


def test_wing_launch_cwd_is_not_a_workspace():
    assert session_workdir._context_cwd_is_launch_artifact({"source": "wing"}) is True
    assert session_workdir._context_cwd_is_launch_artifact({"source": "telegram"}) is False


# Sites whose desktop check must accept wing (spec section 4, rows 1, 2, 6, 7, 9, 13). They read a live value,
# so a bare string comparison against "desktop" would silently exclude wing.
_PARITY_FILES = (
    "tui_gateway/session_lifecycle.py",
    "tui_gateway/session_workdir.py",
    "tui_gateway/methods_session.py",
    "agent/conversation_loop.py",
    "agent/terminal_approval_batch.py",
)
_BARE_DESKTOP = re.compile(r'[!=]= "desktop"|not in \("desktop", "tui"\)')


@pytest.mark.parametrize("rel", _PARITY_FILES)
def test_no_bare_desktop_comparison_left(rel):
    hits = [line for line in (ROOT / rel).read_text(encoding="utf-8").splitlines() if _BARE_DESKTOP.search(line)]
    assert hits == []


def test_desktop_only_surfaces_stay_desktop_only():
    # Wing cannot serve desktop_ui/catalog client tools (spec rows 14-16).
    assert 'platform == "desktop"' in (ROOT / "tui_gateway/agent_factory.py").read_text(encoding="utf-8")


import asyncio
import json

from tui_gateway import ws as ws_mod
from tui_gateway.contracts.events import GatewayReadyPayload


def test_ws_ready_advertises_wing_source(monkeypatch):
    sent = []
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))
    ready = sent[0]["params"]
    assert ready["type"] == "gateway.ready"
    assert ready["payload"]["session_sources"] == ["wing"]


def test_ready_contract_declares_session_sources():
    assert "session_sources" in GatewayReadyPayload.model_fields
