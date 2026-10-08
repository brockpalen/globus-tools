"""Documentation completeness.

Options are easy to add and easy to forget to write down. These tests fail
when the reference falls behind the parser, which is the only way a manual
doc stays honest.
"""

import argparse
import pathlib

from globus_console_cli import console, fs

ROOT = pathlib.Path(__file__).resolve().parent.parent
USAGE = (ROOT / "USAGE.md").read_text()
README = (ROOT / "README.md").read_text()


def _options():
    """Walk the parser and yield every command and long option.

    :returns: an iterator of ``(command, flag)`` pairs
    """

    def walk(parser, path=""):
        """Recurse into subparsers.

        :param parser: the parser to walk
        :param path: the command path reached so far
        :returns: an iterator of ``(command, flag)`` pairs
        """
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    yield from walk(sub, f"{path} {name}".strip())
                continue
            if action.dest == "help":
                continue
            for flag in action.option_strings:
                if flag.startswith("--"):
                    yield path, flag

    return [
        (f"{tool.__name__.rsplit('.', 1)[-1]} {cmd}", flag)
        for tool in (fs, console)
        for cmd, flag in walk(tool.build_parser())
    ]


def test_every_option_is_documented():
    """Every long option appears somewhere in USAGE.md.

    Reported together rather than one test per option, so a batch of new
    flags shows up as one readable list instead of 40 failures.
    """
    missing = sorted(
        f"{flag}  ({command or 'top level'})"
        for command, flag in _options()
        if flag not in USAGE
    )
    assert not missing, "not documented in USAGE.md:\n  " + "\n  ".join(missing)


def test_every_command_is_documented():
    """Each subcommand of both tools is named in USAGE.md."""
    for tool in (fs, console):
        for name in tool.build_parser().command_names:
            assert f"`{name}`" in USAGE, f"{name} is not documented in USAGE.md"


def test_readme_documents_the_shortcut_file_format():
    """The format has to be in the README, not only the reference."""
    assert "NAME = COLLECTION_UUID" in README
    assert "collections.conf" in README


def test_readme_documents_every_shortcut_source():
    """All three places shortcuts are read from are named in the README."""
    for source in (
        "/etc/globus-fs/collections.conf",
        "GLOBUS_FS_COLLECTIONS",
        "~/.globus/app/<client-id>/globus-fs/collections.conf",
    ):
        assert source in README, f"{source} is not in the README"


def test_help_summarises_targets_and_shortcuts():
    """--help carries a short version, for people who never open a file."""
    epilog = fs.MAIN_EPILOG
    assert "targets" in epilog
    assert "GLOBUS_FS_COLLECTIONS" in epilog
    assert "NAME = COLLECTION_UUID" in epilog


def test_collections_help_carries_the_format():
    """`collections --help` is where someone will look for the syntax."""
    assert "NAME = COLLECTION_UUID" in fs.COLLECTIONS_EPILOG
    assert "/etc/globus-fs/collections.conf" in fs.COLLECTIONS_EPILOG


def test_readme_points_at_the_reference():
    """A trimmed README must say where the full reference lives."""
    assert "USAGE.md" in README


def test_docs_name_both_tools_and_not_the_old_one():
    """The rename is complete: nothing still says globus-activity."""
    for doc in (README, USAGE):
        assert "globus-fs" in doc and "globus-console" in doc
        assert "globus-activity" not in doc
