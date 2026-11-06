"""Console entry points that keep a user's Python environment out.

``uv tool install`` and pip give the tools a virtualenv of their own, but
Python still obeys ``PYTHONPATH``, and HPC modules set it: a globus-cli
module, for one, puts an older globus-sdk ahead of ours, and the tool fails
to import. So when ``PYTHONPATH`` is set, re-run the same interpreter with
``-E``, which ignores it, before anything outside the standard library is
imported. The standalone build gets the same from its ``python -I``
launchers.

Nothing here may import globus_sdk or any other dependency.
"""

from __future__ import annotations

import os
import sys

__all__ = ["console", "fs"]

#: Run in the child: drop sys.path[0], which ``-c`` sets to the current
#: directory, so a ``globus_sdk`` there cannot shadow ours either.
_CHILD = (
    "import sys; del sys.path[0]; from globus_tools.{0} import main; sys.exit(main())"
)


def _found_through_pythonpath(pythonpath: str, package_dir: str) -> bool:
    """Check whether this package was itself imported from ``PYTHONPATH``.

    An install that only works because of ``PYTHONPATH`` (a ``--target``
    install behind a module, a source checkout) must keep it.

    :param pythonpath: the value of ``PYTHONPATH``
    :param package_dir: the directory containing the ``globus_tools`` package
    :returns: True if one of its entries is that directory
    """
    here = os.path.realpath(package_dir)
    return any(
        os.path.realpath(entry) == here
        for entry in pythonpath.split(os.pathsep)
        if entry
    )


def needs_isolation(environ, ignore_environment: bool, package_dir: str) -> bool:
    """Decide whether to re-run without the user's ``PYTHONPATH``.

    :param environ: the process environment
    :param ignore_environment: ``sys.flags.ignore_environment``; already set
        in the re-run child, which stops it re-running again
    :param package_dir: the directory containing the ``globus_tools`` package
    :returns: True if ``PYTHONPATH`` could put foreign packages ahead of ours
    """
    pythonpath = environ.get("PYTHONPATH", "")
    if ignore_environment or not pythonpath:
        return False
    return not _found_through_pythonpath(pythonpath, package_dir)


def _run(module: str) -> int:
    """Start one tool, first shedding ``PYTHONPATH`` if it is set.

    :param module: ``fs`` or ``console``
    :returns: the tool's exit status; does not return after a re-run
    """
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.executable and needs_isolation(
        os.environ, bool(sys.flags.ignore_environment), package_dir
    ):
        argv = [sys.executable, "-E", "-c", _CHILD.format(module), *sys.argv[1:]]
        os.execv(sys.executable, argv)  # nosec B606 - our own interpreter
    if module == "fs":
        from .fs import main
    else:
        from .console import main
    return main()


def fs() -> int:
    """Entry point for ``globus-fs``.

    :returns: the exit status
    """
    return _run("fs")


def console() -> int:
    """Entry point for ``globus-console``.

    :returns: the exit status
    """
    return _run("console")
