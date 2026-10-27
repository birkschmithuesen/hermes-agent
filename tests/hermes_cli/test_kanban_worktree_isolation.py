"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kbc.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kbw._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def test_resolve_worktree_reuses_own_worktree_on_respawn(kanban_home, tmp_path, monkeypatch):
    """Dispatcher respawn (e.g. after ``kanban_unblock``) re-resolves a
    worktree task whose ``workspace_path`` already IS that task's own
    finished ``<repo>/.worktrees/<task-id>`` checkout, on its own branch.

    If the linked-worktree probe (``_is_linked_worktree_checkout``) ever
    returns a false negative for that exact path (a stale/slow git-dir
    lookup -- the probe shells out and fails closed to "not linked" on any
    error), ``_resolve_worktree_workspace`` must still recognize the
    canonical ``<repo>/.worktrees/<task-id>`` shape and reuse it, rather than
    treating the worktree itself as a repo root and anchoring a NESTED
    ``<workspace_path>/.worktrees/<task-id>`` worktree on the SAME branch
    that is already checked out at ``workspace_path`` -- which git refuses
    ("already checked out").
    """
    repo = _make_repo(tmp_path)
    tid = None
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="respawn me", workspace_kind="worktree")

    branch = f"wt/{tid}"
    own_worktree = _add_worktree(repo, repo / ".worktrees" / tid, branch)

    with kbc.connect() as conn:
        kbw.set_workspace_path(conn, tid, str(own_worktree))
        kbw.set_branch_name(conn, tid, branch)
        task = kb.get_task(conn, tid)

    # Simulate a false negative from the linked-worktree probe -- the
    # scenario this test targets, whatever its real-world cause.
    monkeypatch.setattr(kbw, "_is_linked_worktree_checkout", lambda path: False)

    workspace, resolved_branch = kbw._resolve_worktree_workspace(task)

    assert workspace == own_worktree.resolve()
    assert resolved_branch == branch
    # The existing checkout is untouched, still on its own branch -- no
    # nested worktree was ever attempted underneath it.
    assert not (own_worktree / ".worktrees").exists()
    head = subprocess.run(
        ["git", "-C", str(own_worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == branch




