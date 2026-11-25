"""reset_reused_thread_state() must tolerate tools.terminal_tool_sudo mid-import.

Python puts a module into ``sys.modules`` as soon as its import starts, before any of its
module-level names exist. A parked worker (gateway/turn_executor.py) can call
``reset_reused_thread_state()`` while another thread is still importing
``tools.terminal_tool_sudo`` for the first time, so the lookup must not assume the cache
attributes are already there (gateway journal, 2026-11-20 10:00:49, two AttributeErrors).
"""

from __future__ import annotations

import sys
import threading
import types

from gateway.turn_thread_reset import reset_reused_thread_state


def test_partial_import_is_tolerated(monkeypatch):
    stub = types.ModuleType("tools.terminal_tool_sudo")
    monkeypatch.setitem(sys.modules, "tools.terminal_tool_sudo", stub)
    reset_reused_thread_state()  # must not raise AttributeError


def test_existing_behaviour_preserved(monkeypatch):
    stub = types.ModuleType("tools.terminal_tool_sudo")
    stub._sudo_password_cache = {}
    stub._sudo_password_cache_lock = threading.Lock()
    monkeypatch.setitem(sys.modules, "tools.terminal_tool_sudo", stub)

    tid = threading.get_ident()
    stub._sudo_password_cache[f"thread:{tid}"] = "whatever"

    reset_reused_thread_state()

    assert f"thread:{tid}" not in stub._sudo_password_cache
