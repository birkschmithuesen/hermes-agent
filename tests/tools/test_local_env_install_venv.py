"""Regression for the 11.11 core-update install layout: PM now gives each environment
generation an ``installs/<id>/environments/<id>/`` directory with two siblings --
``venv`` (the interpreter's own site-packages) and ``workspace`` (a per-generation copy
of the source tree that the editable finder maps ``tools`` etc. onto). The gateway never
runs *as* that venv's own interpreter (it boots the store/toolchain Python and only adds
the venv's site-packages via ``site.addsitedir``, so ``sys.prefix`` never points at it and
``_in_venv`` stays False); ownership detection falls through entirely to
``_validated_runtime_venv``, which anchors on
``Path(__file__).resolve().parents[2]`` to find this install's ``facts.json``. Under the
new layout that anchor lands on the ``workspace`` copy -- not the checkout -- because the
editable finder maps this very module's ``__file__`` there, so ``install_key(root)``
hashes the wrong path and the lookup always misses.
"""

import os
from pathlib import Path

from tools.environments import local
from tools.environments import local_pythonpath as pp


def _build_install_env_layout(tmp_path: Path) -> tuple[Path, Path]:
    """``installs/<key>/environments/<key>/{workspace,venv}``, matching the real layout.
    Returns (fake module __file__ inside workspace, the venv's site-packages dir)."""
    env_dir = tmp_path / "installs" / "f50c1716a335bc71" / "environments" / "9c91c54e56c8c630"
    module_file = env_dir / "workspace" / "tools" / "environments" / "local_pythonpath.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    venv = env_dir / "venv"
    site_packages = venv / "lib" / "python3.14" / "site-packages"
    site_packages.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\nimplementation = CPython\nversion_info = 3.14.0\n"
        "include-system-site-packages = false\n", encoding="utf-8")
    (site_packages / "__editable__.hermes_agent-0.0.0.pth").write_text(
        "import __editable___hermes_agent_0_0_0_finder; __editable___hermes_agent_0_0_0_finder.install()\n",
        encoding="utf-8")
    return module_file, site_packages


def test_running_interpreters_own_install_env_venv_is_runtime_provenance(tmp_path, monkeypatch):
    """The gateway's own ``installs/<id>/environments/<id>/venv`` site-packages must be
    recognized as Hermes-owned and stripped from an inherited PYTHONPATH, even though the
    gateway process never literally boots as that venv's interpreter (store-Python +
    site.addsitedir, so ``_in_venv`` is False here, as in the real deployment)."""
    module_file, site_packages = _build_install_env_layout(tmp_path)
    monkeypatch.setattr(pp, "__file__", str(module_file))
    monkeypatch.setattr(local, "_in_venv", False)
    monkeypatch.setattr(local, "_hermes_site_packages", None)
    monkeypatch.setattr(local, "_hermes_repo_root_aliases", ())
    user_lib = str(tmp_path / "user-lib")
    env = {"PYTHONPATH": os.pathsep.join([str(site_packages), user_lib])}

    pp._strip_hermes_owned_pythonpath(env)

    assert env["PYTHONPATH"] == user_lib
