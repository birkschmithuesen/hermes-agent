"""Tests for ``hermes_cli.kanban_worker_argv`` (the module-form worker argv
``kanban_db_dispatch`` falls back to).
"""

from __future__ import annotations

import os


def test_resolve_hermes_argv_module_actually_runs():
    """The fallback module name must be importable + runnable.

    A unit test that pins the literal string is necessary but not
    sufficient — if `hermes_cli.main` ever loses `if __name__ == "__main__"`
    handling or its argparse setup, `python -m hermes_cli.main --version`
    would fail and so would every dispatcher spawn that hits the fallback.
    Run it as a real subprocess to catch that regression.

    The module-form argv runs with `-P` (never let the worker's cwd shadow
    the dispatcher's own import root, t_740aaada), so the interpreter's
    implicit cwd entry on `sys.path` is gone. The real spawn path always
    pairs `-P` with `_propagate_module_import_root` pinning `PYTHONPATH`
    (`kanban_db_dispatch.py::_propagate_module_import_root`); mirror that
    pin here too, so this proves the actual runtime contract instead of
    relying on an accidental cwd side effect that no longer exists.
    """
    import subprocess
    from pathlib import Path

    from hermes_cli import kanban_db_dispatch as kbd
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    import shutil
    from unittest import mock

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HERMES_BIN", None)
        with mock.patch.object(shutil, "which", return_value=None):
            argv = kbd._resolve_hermes_argv()
    env = dict(os.environ)
    # The test interpreter's venv owns its dependencies, so there is no
    # committed PM generation to restore; asking PM would read the real
    # install-state dir (live-system guard).
    with mock.patch(
        "cron.scheduler_worker_env._committed_dependency_site_packages", return_value=None
    ):
        pin_hermes_tree_on_pythonpath(env, Path(kbd.__file__).resolve().parents[1])
    # `--version` runs a synchronous update check (git fetch / HTTP) whose
    # cache lives in the per-test HERMES_HOME and is therefore always cold;
    # under parallel load it can exceed the timeout. It is irrelevant to
    # "is the module runnable", so opt out via the per-test home's config.
    (Path(env["HERMES_HOME"]) / "config.yaml").write_text(
        "updates:\n  check: false\n", encoding="utf-8"
    )
    r = subprocess.run(
        argv + ["--version"], capture_output=True, text=True, timeout=30, check=False, env=env
    )
    assert r.returncode == 0, (
        f"`{' '.join(argv)} --version` failed (rc={r.returncode}); "
        f"stderr={r.stderr[:200]!r}"
    )
