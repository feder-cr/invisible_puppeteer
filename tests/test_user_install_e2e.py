"""The install path a stranger takes, driven end to end: the README's block.

The README's install block for this system, run as written - uv from its own
installer, a project `uv init` made, `uv add invisible-puppeteer` FROM THE INDEX, the
engine fetched from the live release - and then a real browser started from that
project. Slow and network-bound, so marked `e2e` and excluded by default:

    pytest tests/test_user_install_e2e.py -m e2e -o addopts="" -v

Run by `.github/workflows/user-install.yml` on ubuntu AND windows, weekly and on
release. Until 2026-10-17 this repository had neither: nothing executed the
README's install block, so a wrong instruction there passed every gate.

LOCAL ON PURPOSE, like the wrapper's test_release_e2e.py: the job installs
pytest on the runner and nothing else, and the package under test lives only
in the project the block makes, so nothing here may import it or the core.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DIST = "invisible-puppeteer"
PKG = "invisible_puppeteer"
_INSTALLER = {"nt": "astral.sh/uv/install.ps1", "posix": "astral.sh/uv/install.sh"}

#: Nothing that points at a checkout crosses into the project: with
#: PYTHONPATH on this tree the project's python imports the checkout, not the
#: copy the index served (measured on the core's own user path, 2026-10-17).
_LEAKS = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "INVISIBLE_SEAL_FILE")


def _run(cmd, *, timeout: int = 600, env=None, cwd=None):
    env = dict(os.environ if env is None else env)
    for k in _LEAKS:
        env.pop(k, None)
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       timeout=timeout, env=env, cwd=str(cwd) if cwd else None)
    if r.returncode != 0:
        raise AssertionError(
            "{} exited {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                " ".join(str(c) for c in cmd), r.returncode,
                r.stdout[-3000:], r.stderr[-3000:]))
    return r


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def readme_install_block() -> list:
    """The lines of the README's install block for this system: the code block
    that runs uv's own installer for it."""
    marker = _INSTALLER[os.name]
    text = README.read_text(encoding="utf-8")
    for body in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
        if marker in body:
            return [line for line in body.splitlines() if line.strip()]
    raise AssertionError(f"README.md has no install block for this system: no "
                         f"code block runs {marker}")


def _home_bin(home: Path) -> Path:
    return home / ".local" / "bin"


def run_readme_block(lines, *, cwd: Path, home: Path, env: dict):
    """Run the block the way its reader does - PowerShell on Windows, bash on
    Linux - stopping at the first line that fails. `home` stands in for the
    reader's home, so uv's installer lands in a directory this run owns and the
    block's own PATH line finds it there; on Windows `UV_NO_MODIFY_PATH` keeps
    it out of the registry. The lines are the README's, unchanged."""
    env = dict(env)
    env["UV_INSTALL_DIR"] = str(_home_bin(home))
    env["UV_NO_CACHE"] = "1"   # a cached index answer would hide a forgotten publish
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        env["UV_NO_MODIFY_PATH"] = "1"
        script = cwd / "install.ps1"
        script.write_text("".join(f"{line}\nif ($LASTEXITCODE) {{ exit $LASTEXITCODE }}\n"
                                  for line in lines), encoding="utf-8")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    else:
        env["HOME"] = str(home)
        script = cwd / "install.sh"
        script.write_text("set -eo pipefail\n" + "\n".join(lines) + "\n", encoding="utf-8")
        cmd = ["bash", script]
    return _run(cmd, cwd=cwd, env=env, timeout=1800)


@pytest.fixture(scope="module")
def workspace():
    root = Path(tempfile.mkdtemp(prefix="way-in-e2e-"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def cache_env(workspace: Path) -> dict:
    """A private engine cache, so this never reads or fills the machine's one -
    and never passes because something was already there."""
    env = dict(os.environ)
    env["INVISIBLE_PLAYWRIGHT_CACHE_DIR"] = str(workspace / "cache")
    env["XDG_CACHE_HOME"] = str(workspace / "cache")
    return env


@pytest.fixture(scope="module")
def project_python(workspace: Path, cache_env: dict) -> Path:
    """The README's block, run once; the interpreter of the project it made."""
    lines = readme_install_block()
    home = workspace / "home"
    home.mkdir(exist_ok=True)
    run_readme_block(lines, cwd=workspace, home=home, env=cache_env)
    cd = [line.split(maxsplit=1)[1] for line in lines if line.split()[:1] == ["cd"]]
    assert len(cd) == 1, f"the block has to enter its project once: {lines}"
    py = _venv_python(workspace / cd[0] / ".venv")
    assert py.exists(), f"the README's block made no venv at {py}"
    return py


def test_the_readme_block_installs_from_the_index(project_python: Path, workspace: Path):
    """The block ran to its last line, the package imports from the project, and
    the installer's own check finds the environment consistent - which is only
    meaningful because the core is pinned exactly."""
    out = _run([project_python, "-c",
                f"import {PKG}; from importlib.metadata import version; "
                f"print('OK', version({DIST!r}), version('invisible-core'))"])
    assert out.stdout.startswith("OK "), out.stdout
    uv = _home_bin(workspace / "home") / ("uv.exe" if os.name == "nt" else "uv")
    _run([uv, "pip", "check", "--python", project_python], timeout=120)


def test_the_block_asks_for_the_python_the_package_needs():
    """A README asking `uv init` for a Python older than `requires-python` makes
    `uv add` fail on every machine whose default interpreter is that old
    (measured 2026-10-17: a project on 3.10 cannot add these packages)."""
    floor = tomllib.loads((ROOT / "pyproject.toml").read_text(
        encoding="utf-8"))["project"]["requires-python"]
    init = [line for line in readme_install_block() if line.split()[:2] == ["uv", "init"]]
    assert len(init) == 1 and f'--python "{floor}"' in init[0], (init, floor)


def test_the_project_drives_a_browser(project_python: Path, cache_env: dict):
    """What somebody's first script does, from the project the block made: the
    engine the block fetched starts and loads a page."""
    out = _run([project_python, "-c", "import asyncio\nfrom invisible_puppeteer import launch\nasync def main():\n    b = await launch(headless=True)\n    try:\n        p = await b.newPage()\n        await p.goto('data:text/html,<title>way in</title>')\n        print('TITLE', await p.title())\n    finally:\n        await b.close()\nasyncio.run(main())\n"], env=cache_env, timeout=300)
    assert "TITLE way in" in out.stdout, out.stdout + out.stderr
