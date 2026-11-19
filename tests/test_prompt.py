"""The shell prompt: colour when it is wanted, and high assurance in words."""

import pytest
from conftest import COLLECTION

from globus_tools import common, fs, shell
from globus_tools.shell import GlobusShell


@pytest.fixture
def sh(monkeypatch, client):
    """An interactive shell, its colour decided per test.

    :param monkeypatch: pytest's monkeypatch fixture
    :param client: the fake Transfer client
    :returns: the shell, not yet on a collection
    """
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GLOBUS_FS_COLOR", raising=False)
    fs._BOOKMARK_CACHE = None
    s = GlobusShell(fs.TOOL)
    s.interactive = True
    yield s
    fs._BOOKMARK_CACHE = None


def test_colour_always(sh, monkeypatch):
    """Cyan label, bold path, each wrapped for readline."""
    monkeypatch.setenv("GLOBUS_FS_COLOR", "always")
    sh.onecmd("cd projects:")
    assert sh.prompt == (
        "[\x01\x1b[36m\x02projects\x01\x1b[0m\x02] "
        "\x01\x1b[1m\x02/data\x01\x1b[0m\x02> "
    )


class _Tty:
    """A stream that says it is a terminal."""

    def isatty(self):
        """Claim to be a terminal.

        :returns: True
        """
        return True


def pretend_terminal(monkeypatch):
    """Make stdin and stdout look like a terminal, for use_color.

    :param monkeypatch: pytest's monkeypatch fixture
    """
    monkeypatch.setattr(shell.sys, "stdin", _Tty())
    monkeypatch.setattr(shell.sys, "stdout", _Tty())


def test_auto_colours_a_terminal(monkeypatch):
    """auto on an interactive terminal: colour."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GLOBUS_FS_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    pretend_terminal(monkeypatch)
    assert shell.use_color() is True


@pytest.mark.parametrize(
    "env",
    [{"GLOBUS_FS_COLOR": "never"}, {"NO_COLOR": "1"}, {"TERM": "dumb"}],
)
def test_colour_off_on_a_terminal(monkeypatch, env):
    """never, NO_COLOR or a dumb terminal turn colour off, even on a terminal."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GLOBUS_FS_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    pretend_terminal(monkeypatch)
    assert shell.use_color() is False


def test_colour_off_when_not_a_terminal(sh):
    """auto, piped or scripted: plain text."""
    sh.onecmd("cd projects:")
    assert sh.prompt == "[projects] /data> "


def test_high_assurance_is_marked_in_words(sh, client, monkeypatch):
    """HA in the label, so it never depends on colour alone."""
    monkeypatch.setenv("GLOBUS_FS_COLOR", "never")
    client.high_assurance.add(COLLECTION)
    sh.onecmd("cd projects:")
    assert sh.prompt == "[projects HA] /data> "


def test_high_assurance_colour(sh, client, monkeypatch):
    """With colour, the high assurance label is amber."""
    monkeypatch.setenv("GLOBUS_FS_COLOR", "always")
    client.high_assurance.add(COLLECTION)
    sh.onecmd("cd projects:")
    assert "\x1b[1;33m\x02projects HA" in sh.prompt


def test_pwd_v_reports_high_assurance(sh, client, capsys):
    """pwd -v says whether the collection is high assurance."""
    client.high_assurance.add(COLLECTION)
    sh.onecmd("cd projects:")
    sh.onecmd("pwd -v")
    assert "high assurance: yes" in capsys.readouterr().out


def test_collection_document_fetched_once(sh, client, monkeypatch):
    """One get_endpoint per collection, however often the prompt is drawn."""
    monkeypatch.setenv("GLOBUS_FS_COLOR", "always")
    for path in ("projects:", "projects:project-a", "projects:project-b"):
        sh.onecmd(f"cd {path}")
    sh.onecmd("pwd -v")
    assert client.endpoint_calls == [COLLECTION]


def test_names_in_a_coloured_prompt_are_still_escaped(sh, monkeypatch):
    """Colour codes are ours; a name's control characters are escaped."""
    from conftest import TREE

    monkeypatch.setenv("GLOBUS_FS_COLOR", "always")
    monkeypatch.setitem(TREE, "/data", TREE["/data"] + [("x\x1b[2J", "dir", 0)])
    monkeypatch.setitem(TREE, "/data/x\x1b[2J", [])
    sh.onecmd("cd projects:")
    sh.onecmd("cd x\x1b[2J")
    assert "x\\x1b[2J" in sh.prompt
    assert "\x1b[2J" not in sh.prompt
