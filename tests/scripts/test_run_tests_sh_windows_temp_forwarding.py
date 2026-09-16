"""Regression test: scripts/run_tests.sh must not forward TEMP/TMP on POSIX.

``scripts/run_tests.sh`` launches the test suite hermetically via ``env -i``
and deliberately drops ``TMPDIR`` so Python's ``tempfile.gettempdir()`` falls
back to a hermetic default. CPython's tempfile resolution checks ``TMPDIR``,
then ``TEMP``, then ``TMP`` (see ``tempfile._candidate_tempdir_list``) — so if
the runner forwards ``TEMP``/``TMP`` unconditionally, a POSIX shell that has
them set (many do, inherited alongside ``TMPDIR``) defeats the ``env -i``
isolation through the back door: pytest's ``tmp_path`` fixture can end up
under a real, non-hermetic root instead of the sandboxed one, which is
exactly what ``tests/conftest.py``'s ``_kanban_write_guard`` exists to catch
(see kanban task t_7aece4eb).

``TEMP``/``TMP`` are only genuinely required on *native Windows* (CPython's
tempfile has no ``TMPDIR`` fallback there). This test binds the runner's
env-forwarding logic in isolation (no full pytest run needed) by faking
``uname`` on PATH and a stub ``python`` that just dumps its final
environment, then inspecting whether TEMP/TMP made it through the
``env -i`` hand-off.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run_tests.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_fake_repo(tmp_path: Path, uname_output: str) -> Path:
    """Assemble a throwaway repo checkout that lets run_tests.sh reach its
    `exec env -i ... "$PYTHON" ...` line without doing real venv/test work.
    """
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(RUN_TESTS_SH, scripts_dir / "run_tests.sh")
    (scripts_dir / "run_tests_parallel.py").write_text("# stub\n")

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("# stub activate\n")
    # Fake python: `-c ...` (the `import pytest` probe) succeeds silently;
    # any other invocation (compileall pre-warm, the real test-runner exec)
    # just dumps its environment so the test can inspect what survived
    # `env -i`.
    _write_executable(
        venv_bin / "python",
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "env\n",
    )

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uname",
        "#!/bin/sh\n"
        f'if [ "${{1:-}}" = "-s" ]; then echo "{uname_output}"; else echo "{uname_output}"; fi\n',
    )

    return repo


def _run(tmp_path: Path, uname_output: str, temp_value: str, tmp_value: str) -> str:
    repo = _build_fake_repo(tmp_path, uname_output)
    home = tmp_path / "home"
    home.mkdir()

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path / 'fakebin'}{os.pathsep}{env.get('PATH', os.defpath)}"
    env["HOME"] = str(home)
    env["TEMP"] = temp_value
    env["TMP"] = tmp_value

    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, str(repo / "scripts" / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"run_tests.sh exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return proc.stdout


def test_posix_does_not_forward_temp_tmp(tmp_path: Path) -> None:
    """On a POSIX uname, TEMP/TMP set under a real (non-hermetic) root must
    NOT survive into the `env -i`-wrapped test-runner process."""
    real_root_like = str(tmp_path / "real-root" / "tmp")
    output = _run(tmp_path, uname_output="Linux", temp_value=real_root_like, tmp_value=real_root_like)

    assert "TEMP=" not in output, output
    assert "TMP=" not in output, output


def test_windows_still_forwards_temp_tmp(tmp_path: Path) -> None:
    """Native Windows CPython has no TMPDIR fallback — TEMP/TMP must still
    reach the test-runner process there."""
    value = str(tmp_path / "windows-temp")
    output = _run(tmp_path, uname_output="MINGW64_NT-10.0-19045", temp_value=value, tmp_value=value)

    assert f"TEMP={value}" in output, output
    assert f"TMP={value}" in output, output
