"""The entry points keep a user's PYTHONPATH from replacing our dependencies.

A tester with a globus-cli module loaded got its older globus-sdk instead
of ours: the module sets PYTHONPATH, and a virtualenv does not stop that.
"""

import os
import subprocess  # nosec B404
import sys

import pytest

from globus_tools import _launch

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(_launch.__file__)))

#: Stands in for an old globus-sdk: importing it at all is the failure.
POISON = "raise ImportError('the globus_sdk from PYTHONPATH was imported')\n"


@pytest.fixture
def poisoned(tmp_path):
    """Build a directory holding a globus_sdk that fails on import.

    :param tmp_path: pytest's temporary directory
    :returns: the directory, to put on PYTHONPATH
    """
    (tmp_path / "globus_sdk").mkdir()
    (tmp_path / "globus_sdk" / "__init__.py").write_text(POISON)
    return tmp_path


def entry_point(name, pythonpath, cwd):
    """Run an entry point the way its console script does.

    :param name: ``fs`` or ``console``
    :param pythonpath: the PYTHONPATH to set
    :param cwd: the directory to run in
    :returns: the completed process
    """
    env = dict(os.environ, PYTHONPATH=str(pythonpath))
    code = f"import sys; from globus_tools._launch import {name}; sys.exit({name}())"
    return subprocess.run(  # nosec B603
        [sys.executable, "-c", code, "--help"],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    "name, prog", [("fs", "globus-fs"), ("console", "globus-console")]
)
def test_pythonpath_cannot_replace_globus_sdk(poisoned, tmp_path, name, prog):
    """With a foreign globus_sdk on PYTHONPATH, the tool still starts."""
    result = entry_point(name, poisoned, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"usage: {prog}" in result.stdout


def test_current_directory_cannot_replace_globus_sdk(poisoned):
    """The re-run does not pick up a globus_sdk in the working directory."""
    result = entry_point("fs", "/nonexistent", poisoned)
    assert result.returncode == 0, result.stderr


def test_poison_really_poisons(poisoned):
    """Guard the tests above: without -E, PYTHONPATH wins in this venv."""
    env = dict(os.environ, PYTHONPATH=str(poisoned))
    result = subprocess.run(  # nosec B603
        [sys.executable, "-c", "import globus_sdk"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "from PYTHONPATH was imported" in result.stderr


def test_no_rerun_without_pythonpath():
    """The common case pays nothing."""
    assert not _launch.needs_isolation({}, False, PACKAGE_DIR)
    assert not _launch.needs_isolation({"PYTHONPATH": ""}, False, PACKAGE_DIR)


def test_rerun_when_pythonpath_is_set():
    """Any PYTHONPATH could shadow a dependency."""
    assert _launch.needs_isolation({"PYTHONPATH": "/sw/globus-cli"}, False, PACKAGE_DIR)


def test_child_does_not_rerun_again():
    """-E sets ignore_environment in the child, which ends the chain."""
    assert not _launch.needs_isolation({"PYTHONPATH": "/x"}, True, PACKAGE_DIR)


def test_install_that_relies_on_pythonpath_keeps_it():
    """A --target install found through PYTHONPATH would break under -E."""
    env = {"PYTHONPATH": os.pathsep.join(["/elsewhere", PACKAGE_DIR])}
    assert not _launch.needs_isolation(env, False, PACKAGE_DIR)
