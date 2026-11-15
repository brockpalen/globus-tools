"""Names from Globus never reach a terminal raw.

Anyone who can name a file on a shared collection, label a transfer or
title a collection chooses those bytes, and a terminal acts on control
characters: clearing the screen, moving the cursor, retitling the window,
faking a prompt. The rule is GNU ls's: escaped on a terminal, exact through
a pipe or into a file, and exact in machine formats (JSON, -0, --batch).
"""

import io
import json

import pytest
from conftest import COLLECTION, TREE, FakeAPIError, FakeTransferClient, make_task

from globus_tools import common, fs
from globus_tools.common import escape_text, terminal_output

#: Clears the screen and retitles the window when printed raw.
EVIL = "report\x1b[2J\x1b]0;owned\x07.txt"
SHOWN = "report\\x1b[2J\\x1b]0;owned\\x07.txt"


@pytest.fixture
def evil_tree(monkeypatch):
    """Add /evil, holding a file and a directory with hostile names.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: nothing; the fake tree is patched for the test
    """
    monkeypatch.setitem(TREE, "/evil", [(EVIL, "file", 10), ("dir\x1b[1m", "dir", 0)])
    monkeypatch.setitem(TREE, "/evil/dir\x1b[1m", [("inner", "file", 5)])


# ---- the escaping itself ------------------------------------------------
@pytest.mark.parametrize(
    "raw, shown",
    [
        ("plain name.txt", "plain name.txt"),
        (EVIL, SHOWN),
        ("two\nlines", "two\\nlines"),
        ("tab\tand\rreturn", "tab\\tand\\rreturn"),
        ("c1\x9bcsi", "c1\\x9bcsi"),
        ("del\x7f", "del\\x7f"),
        ("invoice‮fdp.exe", "invoice\\u202efdp.exe"),
        ("naïve résumé 日本語", "naïve résumé 日本語"),
        ("back\\slash", "back\\slash"),
    ],
)
def test_escape_text(raw, shown):
    """Controls, C1 and direction marks are escaped; other text is not."""
    assert escape_text(raw) == shown


def test_safe_follows_the_terminal():
    """Escaped for a terminal, exact otherwise."""
    with terminal_output(True):
        assert common.safe(EVIL) == SHOWN
    with terminal_output(False):
        assert common.safe(EVIL) == EVIL


# ---- globus-fs commands -------------------------------------------------
def test_ls_escapes_on_a_terminal(run, capsys, evil_tree):
    """The name is shown, not acted on."""
    with terminal_output(True):
        run(["ls", f"{COLLECTION}:/evil"])
    out = capsys.readouterr().out
    assert SHOWN in out and "\x1b" not in out


def test_ls_is_exact_through_a_pipe(run, capsys, evil_tree):
    """Piped output keeps the real name, as ls does."""
    with terminal_output(False):
        run(["ls", f"{COLLECTION}:/evil"])
    assert EVIL in capsys.readouterr().out


def test_ls_long_escapes_owner_and_link(run, capsys, monkeypatch):
    """Owner, group and link targets are names too."""
    monkeypatch.setitem(TREE, "/odd", [("link", "dirlink", 0)])
    client = FakeTransferClient()
    real_ls = client.operation_ls

    def ls(collection, path=None, **kw):
        """Serve /odd with a hostile owner and link target.

        :param collection: passed through
        :param path: passed through
        :param kw: passed through
        :returns: the patched entries
        """
        entries = real_ls(collection, path, **kw)
        for e in entries:
            e.update(user="u\x1b[8m", link_target="/t\x1b[8m")
        return entries

    client.operation_ls = ls
    with terminal_output(True):
        run(["ls", "-l", f"{COLLECTION}:/odd"], client)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "u\\x1b[8m" in out and "/t\\x1b[8m" in out


def test_find_escapes_on_a_terminal(run, capsys, evil_tree):
    """find's plain and -l output."""
    with terminal_output(True):
        run(["find", f"{COLLECTION}:/evil", "-l"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and SHOWN in out


def test_find_print0_is_always_exact(run, capsys, evil_tree):
    """-0 feeds xargs -0, which needs the real bytes."""
    with terminal_output(True):
        run(["find", f"{COLLECTION}:/evil", "--type", "f", "-0"])
    assert f"/evil/{EVIL}\0" in capsys.readouterr().out


def test_find_batch_is_exact(run, capsys, evil_tree):
    """--batch is input for globus delete, so it must be the real path."""
    with terminal_output(True):
        run(["find", f"{COLLECTION}:/evil", "--type", "f", "--batch"])
    assert EVIL in capsys.readouterr().out


def test_du_escapes_paths(run, capsys, evil_tree):
    """du's PATH column."""
    with terminal_output(True):
        run(["du", f"{COLLECTION}:/evil"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and "/evil/dir\\x1b[1m" in out


def test_stat_escapes_the_path(run, capsys, evil_tree):
    """stat prints the path it was given."""
    with terminal_output(True):
        run(["stat", f"{COLLECTION}:/evil/{EVIL}"])
    out = capsys.readouterr().out
    assert "\x1b" not in out and SHOWN in out


def test_json_is_exact_and_still_safe(run, capsys, evil_tree):
    """JSON escapes control characters itself and keeps the real name."""
    with terminal_output(True):
        run(["ls", f"{COLLECTION}:/evil", "--format", "json"])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert EVIL in {e["name"] for e in json.loads(out)}


def test_unlistable_directory_message_is_escaped(run, capsys, evil_tree):
    """Walk errors go to stderr, which is for people: always escaped."""
    client = FakeTransferClient(unlistable=["/evil/dir\x1b[1m"])
    with pytest.raises(SystemExit):
        run(["du", f"{COLLECTION}:/evil"], client)
    err = capsys.readouterr().err
    assert "\x1b" not in err and "dir\\x1b[1m" in err


def test_api_error_message_is_escaped():
    """API messages quote paths."""
    error = FakeAPIError(404, "ClientError.NotFound", "no such path /x\x1b[2J")
    with pytest.raises(SystemExit) as caught:
        fs.TOOL.api_error(error)
    assert "\x1b" not in caught.value.code and "/x\\x1b[2J" in caught.value.code


# ---- tasks and the console ------------------------------------------------
def test_task_table_escapes_other_peoples_text(run, capsys):
    """Owners and labels are chosen by whoever submitted the transfer."""
    task = make_task(1, owner_string="mallory\x1b[2J", label="l\x1b]0;x\x07")
    with terminal_output(True):
        run(["tasks", "--fields", "id,owner,label"], FakeTransferClient(tasks=[task]))
    out = capsys.readouterr().out
    assert "\x1b" not in out and "mallory\\x1b[2J" in out


def test_task_show_works_on_a_real_response(run, capsys):
    """task show read dict(response), which fails on the SDK's response."""
    task = make_task(1, label="nightly backup")
    run(["task", task["task_id"]], FakeTransferClient(tasks=[task]))
    assert "nightly backup" in capsys.readouterr().out


def test_task_show_json(run, capsys):
    """The JSON form, likewise."""
    task = make_task(1)
    run(["task", task["task_id"], "--format", "json"], FakeTransferClient(tasks=[task]))
    assert json.loads(capsys.readouterr().out)["task_id"] == task["task_id"]


def test_task_show_escapes_fields_and_events(run, capsys):
    """Every field and event line."""
    task = make_task(1, label="l\x1b[2J")
    events = [{"time": "2026-01-01", "code": "OK", "description": "d\x1b[8m"}]
    client = FakeTransferClient(tasks=[task], events=events)
    with terminal_output(True):
        run(["task", task["task_id"], "--events"], client)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "l\\x1b[2J" in out and "d\\x1b[8m" in out


def test_console_endpoint_names_are_escaped(run_console, capsys):
    """Collection display names are set by their owners."""
    eps = [{"id": COLLECTION, "display_name": "Lab\x1b[2J", "owner_string": "o"}]
    with terminal_output(True):
        run_console(["endpoints"], FakeTransferClient(endpoints=eps))
    out = capsys.readouterr().out
    assert "\x1b" not in out and "Lab\\x1b[2J" in out


# ---- the shell --------------------------------------------------------------
@pytest.fixture
def sh(monkeypatch, client, evil_tree):
    """A shell in /evil.

    :param monkeypatch: pytest's monkeypatch fixture
    :param client: the fake Transfer client
    :param evil_tree: adds the hostile names
    :returns: a shell, at the collection root
    """
    from globus_tools.shell import GlobusShell

    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    fs._BOOKMARK_CACHE = None
    shell = GlobusShell(fs.TOOL)
    shell.onecmd(f"cd {COLLECTION}:/evil")
    yield shell
    fs._BOOKMARK_CACHE = None


def test_prompt_escapes_the_directory(sh):
    """The prompt is always on the terminal."""
    sh.interactive = True
    sh.onecmd("cd dir\x1b[1m")
    assert "\x1b" not in sh.prompt and "dir\\x1b[1m" in sh.prompt


def test_pipe_to_the_terminal_escapes(sh, capfd, monkeypatch):
    """ls | head on a terminal: the filter would pass raw bytes through."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    sh.onecmd("ls | head -5")
    out = capfd.readouterr().out
    assert "\x1b" not in out and SHOWN in out


def test_redirect_keeps_exact_names(sh, tmp_path, monkeypatch):
    """ls > file writes the real names, as ls does."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    sh.onecmd("ls > names.txt")
    assert EVIL in (tmp_path / "names.txt").read_text()


def test_completion_skips_hostile_names(sh):
    """Tab never puts control characters on the command line."""
    offered = sh.completedefault("", "ls ", 3, 3)
    assert offered == []  # both names in /evil hold escapes


def test_shell_error_messages_are_escaped(sh, capsys):
    """Errors quote what was typed or listed."""
    sh.onecmd("cd nowhere\x1b[2J")
    err = capsys.readouterr().err
    assert "\x1b" not in err


def test_pwd_on_a_terminal_is_escaped(sh, capsys):
    """pwd prints the path."""
    sh.onecmd("cd dir\x1b[1m")
    with terminal_output(True):
        sh.onecmd("pwd -u")
    out = capsys.readouterr().out
    assert "\x1b" not in out and "dir\\x1b[1m" in out


def test_script_output_into_a_pipe_is_exact(sh, capsys):
    """A scripted shell whose stdout is a pipe gets exact names."""
    sh.onecmd("ls")
    assert EVIL in capsys.readouterr().out


def test_stringio_stdout_counts_as_not_a_terminal(monkeypatch):
    """Anything without a working isatty is treated as a pipe."""
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert common.output_is_terminal() is False
