"""An update drain is a run-state event, not a card failure (card t_bb79440f).

On 2026-11-15 two core updates killed 40 workers with their own final text
``Kanban owner refused: ... Gateway draining: update_paused`` (or an ImportError from a
half-updated install); 31 cards were given up after ONE failure because identical fingerprints
tripped the systemic same-error rule. Spec: docs/superpowers/specs/2026-11-15-update-drain-requeue-design.md
(each test names its D-number).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_update_drain as kud

DRAIN_OUTPUT = (
    "Query: work kanban task {tid}\n"
    "Initializing agent...\n"
    "hermes: a source update is unfinished; run `hermes update` from a shell to finish it\n"
    "Kanban owner refused: Kanban owner silent for 181s: Gateway draining: update_paused\n"
)
PLAIN_CRASH = "Query: work kanban task {tid}\nTraceback (most recent call last):\nValueError: boom\n"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


@pytest.fixture
def install_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake live install; a card's worktree lives elsewhere under tmp_path."""
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(kud, "_install_root", lambda: root)
    return root


def _module_file(base: Path, mtime: float) -> Path:
    mod = base / "hermes_cli" / "_subprocess_compat.py"
    mod.parent.mkdir(parents=True, exist_ok=True)
    mod.write_text("")
    os.utime(mod, (mtime, mtime))
    return mod


def _import_error(tid: str, path: Path) -> str:
    return (
        f"Query: work kanban task {tid}\nTraceback (most recent call last):\n"
        '  File "worker.py", line 1, in <module>\n'
        f"ImportError: cannot import name 'run_quiet' from 'hermes_cli._subprocess_compat' ({path})\n"
    )


# --- D2: the drain text, anchored at the end of the worker's final output ---------------------


@pytest.mark.parametrize("output, kind", [
    ("x Kanban owner refused: Kanban owner silent for 181s: Gateway draining: update_paused", "drain"),
    ("x Kanban owner refused: Gateway draining: update_paused  ", "drain"),
    ("Kanban owner refused: Gateway draining: update_paused Traceback (most recent call last): "
     "ValueError: boom", None),
    ("connection: Gateway draining: update_paused. Your message was not sent. Run `hermes doctor` "
     "to check the setup, or /model to pick a different provider. Goodbye! ☤", None),
    ("Kanban owner refused: Gateway inaccessible: service_identity_unverified", None),
    ("", None),
], ids=["owner-silent", "owner-refused", "drain-not-last", "one-shot-worker-text",
        "service-identity", "no-output"])
def test_the_drain_text_counts_only_as_the_final_output(output, kind):
    """D2 (+ D7 out-of-scope shapes): only our own owner-refused drain text, at the very end."""
    assert kud.update_drain_kind(output, None) == kind


# --- D3: a mixed-version ImportError ----------------------------------------------------------


@pytest.mark.parametrize("where, offset, kind", [
    ("install", +300, "mixed_import"),
    ("install", -300, None),
    ("worktree", +300, None),
    ("missing", +300, None),
], ids=["new-file-in-install", "file-older-than-run", "card-worktree-file", "file-gone"])
def test_a_mixed_import_needs_a_new_file_inside_the_install(install_root, tmp_path, where, offset, kind):
    """D3: path inside the install root AND mtime after the run started; anything else counts."""
    run_start = time.time() - 600
    if where == "missing":
        path = install_root / "hermes_cli" / "gone.py"
    else:
        base = install_root if where == "install" else tmp_path / "worktree"
        path = _module_file(base, run_start + offset)
    output = (
        "Traceback (most recent call last): ImportError: cannot import name 'run_quiet' "
        f"from 'hermes_cli._subprocess_compat' ({path})"
    )
    assert kud.update_drain_kind(output, run_start) == kind


# --- D1: the fence probe ----------------------------------------------------------------------


def test_install_update_fenced_reads_the_real_update_marker(kanban_home):
    """D1: the real gateway_runtime judge over this home. An empty marker is an update's
    exclusive publish in flight and fences for EMPTY_MARKER_GRACE_SECONDS (5 s)."""
    from hermes_cli import update_lock

    assert kud.install_update_fenced() is False
    (kanban_home / update_lock.MARKER_NAME).write_bytes(b"")
    assert kud.install_update_fenced() is True


def test_a_broken_fence_probe_fails_open(kanban_home, monkeypatch):
    """D1: a probe that raises must not stop the factory (failure mode = today's behaviour)."""
    from hermes_cli import gateway_runtime

    def broken(_homes):
        raise OSError("probe broke")

    monkeypatch.setattr(gateway_runtime, "_update_fenced", broken)
    assert kud.install_update_fenced() is False
