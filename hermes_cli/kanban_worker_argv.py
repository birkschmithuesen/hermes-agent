"""Module-form ``hermes`` worker argv: the interpreter-bound invocation ``kanban_db_dispatch``
falls back to, and the predicate that recognizes it (with or without ``-P``).

Split out of ``hermes_cli.kanban_db_dispatch`` (code-health FILE_LINES ratchet; see
``docs/refactoring-guidelines.md`` and ``scripts/code_health/config.py``).
"""

from __future__ import annotations

import sys

_MODULE_ENTRY = ["-m", "hermes_cli.main"]


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package).

    ``-P``: never put the worker's cwd — the task workspace — on ``sys.path``. A workspace that
    is itself a Hermes checkout (a ``.worktrees/<id>`` of hermes-agent) would otherwise shadow
    the tree pinned by ``_propagate_module_import_root``: the worker booted that checkout's code
    against its never-built dependency state and died with "no dependency environment is
    committed" on every spawn (t_740aaada).
    """
    return [sys.executable, "-P", *_MODULE_ENTRY]


def _is_module_hermes_argv(cmd: list[str]) -> bool:
    """True for the module-form worker invocation, with or without ``-P``."""
    return cmd[1:3] == _MODULE_ENTRY or cmd[1:4] == ["-P", *_MODULE_ENTRY]
