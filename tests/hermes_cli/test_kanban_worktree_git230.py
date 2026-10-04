"""Regression: ``_git_abs_path`` must work against git < 2.31.

``git rev-parse --path-format=absolute <flag>`` only understands
``--path-format`` from git 2.31 onward. On git 2.30.x, rev-parse doesn't
recognize the flag and echoes it back verbatim as a leading output line,
with the real (possibly relative) answer on the line after. The buggy
implementation did ``Path(out).expanduser().resolve(strict=False)`` on the
*whole* stripped stdout (both lines glued by an embedded newline), which
resolved against the process CWD instead of the directory git was invoked
against — breaking ``_git_common_dir``/``_git_dir`` and, downstream,
``_ensure_git_worktree``'s "this target is already our worktree, reuse it"
check (the dispatcher then re-ran ``git worktree add`` on an existing
worktree and failed with "already exists").
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-b", "main", str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    return project


# ---------------------------------------------------------------------------
# Test 1: stubbed git 2.30 echo-back behavior
# ---------------------------------------------------------------------------


def test_git_abs_path_resolves_under_stubbed_git_2_30(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git 2.30 echoes ``--path-format=absolute`` back as a literal line and
    answers ``--git-common-dir`` with a path relative to the invocation dir
    (``.git`` here). ``_git_abs_path`` must still return the correct absolute
    path, resolved against the ``path`` argument — not the process CWD."""

    def fake_git_out(cwd: Path, *args: str, timeout: int = 30):
        assert cwd == repo
        assert args == ("rev-parse", "--path-format=absolute", "--git-common-dir")
        # Simulate git 2.30: unknown flag echoed back, then the real (relative) answer.
        return "--path-format=absolute\n.git"

    monkeypatch.setattr(kb, "_git_out", fake_git_out)

    result = kbw._git_abs_path(repo, "--git-common-dir")

    assert result == (repo / ".git").resolve(strict=False)


def test_git_abs_path_passthrough_for_already_absolute_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git >= 2.31 answers with a single absolute line; behavior is unchanged."""

    def fake_git_out(cwd: Path, *args: str, timeout: int = 30):
        return str(repo / ".git")

    monkeypatch.setattr(kb, "_git_out", fake_git_out)

    result = kbw._git_abs_path(repo, "--git-common-dir")

    assert result == (repo / ".git").resolve(strict=False)


# ---------------------------------------------------------------------------
# Test 2: worktree reuse is idempotent
# ---------------------------------------------------------------------------


def test_ensure_git_worktree_reuses_existing_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling ``_ensure_git_worktree`` twice for the same target must not
    attempt ``git worktree add`` a second time — the existing linked worktree
    is recognized via ``_git_common_dir`` and reused."""
    target = repo / ".worktrees" / "t_reuse0001"
    branch = "wt/t_reuse0001"

    kbw._ensure_git_worktree(repo, target, branch)
    assert target.is_dir()

    real_git = kbw._git
    add_calls = []

    def spying_git(repo_root: Path, *args: str, timeout: int):
        if args[:2] == ("worktree", "add"):
            add_calls.append(args)
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", spying_git)

    kbw._ensure_git_worktree(repo, target, branch)

    assert add_calls == []
    assert target.is_dir()
