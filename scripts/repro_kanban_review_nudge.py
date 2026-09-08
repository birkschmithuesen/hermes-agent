"""Reproduction probe for the kanban stop-guard false alarm (task t_bb005803).

Simulates a worker turn that ended with ``kanban_request_review`` and asks the
guard whether it would nudge. Run with ``HERMES_KANBAN_TASK`` set.
"""
from __future__ import annotations

import os

from agent.kanban_stop import build_kanban_stop_nudge, session_called_kanban_terminal

os.environ.setdefault("HERMES_KANBAN_TASK", "t_bb005803")

MESSAGES = [
    {"role": "user", "content": "work kanban task"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "1",
                "type": "function",
                "function": {"name": "kanban_request_review", "arguments": "{}"},
            }
        ],
    },
    {
        "role": "tool",
        "name": "kanban_request_review",
        "tool_call_id": "1",
        "content": '{"success": true, "status": "review"}',
    },
]

if __name__ == "__main__":
    import agent.kanban_stop as _ks

    print("module loaded from:", _ks.__file__)
    print("terminal tools:", sorted(_ks._TERMINAL_KANBAN_TOOLS))
    print("session_called_kanban_terminal:", session_called_kanban_terminal(MESSAGES))
    nudge = build_kanban_stop_nudge(messages=MESSAGES, attempts=0)
    print("NUDGE FIRED:", nudge is not None)
    if nudge:
        print("---")
        print(nudge[:300])
