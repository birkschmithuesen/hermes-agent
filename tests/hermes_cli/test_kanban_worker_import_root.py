"""A dispatcher-spawned module-form worker imports the DISPATCHER's tree, never a Hermes
checkout that happens to be its workspace.

Regression for kanban cards t_740aaada / t_92ddc0d8: a worker whose workspace was a
`.worktrees/<id>` of hermes-agent booted that worktree's `hermes_bootstrap` (``python -m``
puts the cwd ahead of PYTHONPATH), looked up the worktree's never-built install state and died
with "no dependency environment is committed for this install" on every spawn.
"""
import os
import subprocess
from pathlib import Path


def _fake_hermes_tree(root: Path, marker: str) -> None:
    package = root / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "main.py").write_text(f"print({marker!r})\n")


def test_module_worker_ignores_hermes_checkout_in_its_workspace(tmp_path):
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    from hermes_cli import kanban_db_dispatch as kbd

    dispatcher_tree = tmp_path / "hermes-agent"
    workspace = dispatcher_tree / ".worktrees" / "t_review"
    _fake_hermes_tree(dispatcher_tree, "dispatcher tree")
    _fake_hermes_tree(workspace, "workspace tree")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    pin_hermes_tree_on_pythonpath(env, dispatcher_tree)

    result = subprocess.run(kbd._module_hermes_argv(), cwd=workspace, env=env,
                            capture_output=True, text=True, timeout=30, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dispatcher tree"


def test_module_worker_still_runs_from_an_ordinary_workspace(tmp_path):
    """The pin alone must make the module importable once the cwd no longer is."""
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
    from hermes_cli import kanban_db_dispatch as kbd

    dispatcher_tree = tmp_path / "hermes-agent"
    workspace = tmp_path / "other-project"
    workspace.mkdir()
    _fake_hermes_tree(dispatcher_tree, "dispatcher tree")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    pin_hermes_tree_on_pythonpath(env, dispatcher_tree)

    result = subprocess.run(kbd._module_hermes_argv(), cwd=workspace, env=env,
                            capture_output=True, text=True, timeout=30, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dispatcher tree"
