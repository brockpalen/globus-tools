"""globus-fs shell: relative paths over the API, pipes and redirection.

The shell is not a filesystem: these check that each command still turns
into the same API calls globus-fs makes, that output can only go to
read-only local filters or a new private file, and that a mistake never
ends the session.
"""

import io
import os
import stat

import pytest
from conftest import COLLECTION

from globus_tools import common, fs, shell
from globus_tools.shell import GlobusShell, ShellError, check_filter, split_line


@pytest.fixture
def sh(monkeypatch, client):
    """A shell over the fake collection, with no site shortcuts.

    :param monkeypatch: pytest's monkeypatch fixture
    :param client: the fake Transfer client
    :returns: a :class:`GlobusShell`, not yet on a collection
    """
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    fs._BOOKMARK_CACHE = None
    yield GlobusShell(fs.TOOL)
    fs._BOOKMARK_CACHE = None


@pytest.fixture
def at_data(sh):
    """The shell after ``cd projects:``, the bookmark for /data.

    :param sh: the shell fixture
    :returns: the same shell, in /data
    """
    sh.onecmd("cd projects:")
    assert sh.status == 0
    return sh


# ---- moving around ------------------------------------------------------
def test_cd_to_a_bookmark(sh, client):
    """name: goes to the bookmark's directory, checked with one stat."""
    sh.onecmd("cd projects:")
    assert sh.here == (COLLECTION, "/data", "projects")
    assert client.stat_calls == ["/data"]
    assert client.ls_calls == []


def test_cd_relative_parent_previous_and_absolute(at_data):
    """The usual forms all resolve against the current directory."""
    at_data.onecmd("cd project-a/sub1")
    assert at_data.here[1] == "/data/project-a/sub1"
    at_data.onecmd("cd ../..")
    assert at_data.here[1] == "/data"
    at_data.onecmd("cd -")
    assert at_data.here[1] == "/data/project-a/sub1"
    at_data.onecmd("cd /data/project-b")
    assert at_data.here[1] == "/data/project-b"


def test_cd_alone_returns_to_where_the_collection_was_entered(at_data):
    """Like cd with no argument going home."""
    at_data.onecmd("cd project-a/sub1")
    at_data.onecmd("cd")
    assert at_data.here[1] == "/data"


def test_cd_by_uuid(sh):
    """UUID:/path switches collection directly."""
    sh.onecmd(f"cd {COLLECTION}:/data/project-b")
    assert sh.here == (COLLECTION, "/data/project-b", COLLECTION[:8])


def test_cd_to_a_file_is_refused(at_data, capsys):
    """Only directories, and the shell stays where it was."""
    at_data.onecmd("cd readme.txt")
    assert "not a directory" in capsys.readouterr().err
    assert at_data.here[1] == "/data"
    assert at_data.status == 1


def test_cd_to_a_missing_directory_reports_and_carries_on(at_data, capsys):
    """An API error prints; the next command still works."""
    at_data.onecmd("cd nowhere")
    assert "no such path" in capsys.readouterr().err
    at_data.onecmd("pwd")
    assert capsys.readouterr().out == f"{COLLECTION}:/data\n"


def test_never_above_the_root(at_data):
    """.. at / stays at /, as on any filesystem."""
    assert at_data.resolve("../../../..")[1] == "/"


def test_home_is_globus_tilde(at_data):
    """~ is the collection's home directory, which Globus resolves."""
    assert at_data.resolve("~/results")[1] == "/~/results"


def test_colon_in_a_file_name_is_just_a_name(at_data):
    """Only a known bookmark, shortcut or UUID before : names a collection."""
    assert at_data.resolve("run:2.log") == (COLLECTION, "/data/run:2.log", "projects")


def test_paths_need_a_collection_first(sh, client, capsys):
    """Before any cd, a relative path cannot mean anything."""
    sh.onecmd("ls")
    assert "not on a collection yet" in capsys.readouterr().err
    assert client.ls_calls == []


def test_prompt_names_the_collection(monkeypatch, sh):
    """On a terminal the prompt says where you are, never just a path."""
    sh.interactive = True
    sh.onecmd("cd projects:project-a")
    assert sh.prompt == "[projects] /data/project-a> "


# ---- globus-fs commands, relative --------------------------------------
def test_ls_lists_the_current_directory(at_data, client, capsys):
    """ls with no path is ls of here, one listing."""
    at_data.onecmd("ls")
    assert "project-a" in capsys.readouterr().out
    assert client.ls_calls == ["/data"]


def test_relative_paths_reach_the_api_absolute(at_data, client):
    """The API only ever sees absolute paths."""
    at_data.onecmd("ls -l project-a/sub1")
    assert client.ls_calls == ["/data/project-a/sub1"]


def test_find_accepts_single_dash_options(at_data, capsys):
    """find -name, as people type it, means --name."""
    at_data.onecmd("find project-a -name '*.gz'")
    out = capsys.readouterr().out
    assert "x.fastq.gz" in out and "file1.bin" not in out


def test_stat_in_the_shell(at_data, client, capsys):
    """stat costs one call and never lists."""
    at_data.onecmd("stat readme.txt")
    assert "Type: file" in capsys.readouterr().out
    assert client.ls_calls == []


def test_a_bad_option_does_not_end_the_shell(at_data, capsys):
    """argparse's exit is caught like any other error."""
    at_data.onecmd("ls --no-such-option")
    assert at_data.status == 2 or at_data.status == 1
    at_data.onecmd("pwd")
    assert capsys.readouterr().out.endswith(f"{COLLECTION}:/data\n")


def test_help_for_one_command(at_data, capsys):
    """help ls shows ls's options and is not an error."""
    at_data.onecmd("help ls")
    assert "--long" in capsys.readouterr().out
    assert at_data.status == 0


def test_nested_shell_is_refused(at_data, capsys):
    """shell inside the shell would only confuse."""
    at_data.onecmd("shell")
    assert "already in the shell" in capsys.readouterr().err


# ---- pipes ----------------------------------------------------------------
def test_pipe_into_wc(at_data, capfd):
    """Output really goes through a local filter."""
    at_data.onecmd("find --type f | wc -l")
    assert capfd.readouterr().out.strip() == "7"


def test_pipeline_of_filters(at_data, capfd):
    """Several filters chain, as in a shell."""
    at_data.onecmd("find --type f | grep fastq | sort -r | head -1")
    assert capfd.readouterr().out.strip().endswith("y.fastq.gz")


def test_head_closing_the_pipe_early_is_not_an_error(at_data, capfd):
    """| head stops the command quietly."""
    at_data.onecmd("find | head -1")
    assert at_data.status == 0
    assert len(capfd.readouterr().out.splitlines()) == 1


@pytest.mark.parametrize(
    "line",
    [
        "ls | xargs rm",
        "ls | sh",
        "ls | /usr/bin/wc -l",
        "ls | awk '{print}'",
        "ls | tee names.txt",
        "ls | sort -o names.txt",
        "ls | sort -ro names.txt",
        "ls | sort --output=names.txt",
        "ls | sort --compress-program=evil",
        "ls | uniq - names.txt",
    ],
)
def test_filters_that_could_change_local_files_are_refused(
    at_data, client, capsys, monkeypatch, line
):
    """ls | xargs rm would delete local files named like remote ones."""

    def no_popen(*a, **kw):
        """Fail the test if anything was started.

        :param a: ignored
        :param kw: ignored
        """
        raise AssertionError("a refused filter was started")

    monkeypatch.setattr(shell.subprocess, "Popen", no_popen)
    at_data.onecmd(line)
    assert "globus-fs shell:" in capsys.readouterr().err
    assert client.ls_calls == []  # refused before any API call
    assert at_data.status == 1


def test_less_runs_without_its_shell_escapes(at_data, monkeypatch):
    """less can run commands and save files; LESSSECURE turns that off."""
    seen = {}

    class Recorder:
        """Stands in for Popen, recording the environment."""

        def __init__(self, argv, stdin=None, stdout=None, env=None):
            """Record the call and provide a writable stdin.

            :param argv: the filter's words
            :param stdin: ignored
            :param stdout: ignored
            :param env: recorded
            """
            seen["env"] = env
            self.stdin = io.BytesIO()
            self.stdout = None

        def wait(self):
            """Finish at once.

            :returns: 0
            """
            return 0

    monkeypatch.setattr(shell.subprocess, "Popen", Recorder)
    at_data.onecmd("ls | less")
    assert seen["env"]["LESSSECURE"] == "1"


@pytest.mark.parametrize(
    "line, words, filters",
    [
        ("find --name 'a|b'", ["find", "--name", "a|b"], []),
        ('find --name "a>b"', ["find", "--name", "a>b"], []),
        ("ls | wc -l", ["ls"], [["wc", "-l"]]),
    ],
)
def test_quoted_operators_are_literal(line, words, filters):
    """Quotes protect | and > as in a shell."""
    assert split_line(line)[:2] == (words, filters)


@pytest.mark.parametrize(
    "line", ["ls; rm x", "ls && ls", "ls & ", "wc < x", "ls `id`", "ls 'open", "ls |"]
)
def test_unsupported_syntax_is_refused(line):
    """Anything a reader would expect to mean more is an error, not text."""
    with pytest.raises(ShellError):
        words, filters, _ = split_line(line)
        for f in filters:
            check_filter(f)


# ---- redirection -------------------------------------------------------
def test_redirect_creates_a_private_file(at_data, tmp_path, monkeypatch):
    """> writes a new file only its owner can read: names can be PHI."""
    monkeypatch.chdir(tmp_path)
    os.umask(0o022)
    at_data.onecmd("find --type f > files.txt")
    path = tmp_path / "files.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(path.read_text().splitlines()) == 7


def test_redirect_never_overwrites(at_data, tmp_path, monkeypatch, capsys):
    """> refuses an existing file; >> appends to it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keep.txt").write_text("precious\n")
    at_data.onecmd("ls > keep.txt")
    assert "never overwrites" in capsys.readouterr().err
    assert (tmp_path / "keep.txt").read_text() == "precious\n"
    at_data.onecmd("pwd >> keep.txt")
    assert (tmp_path / "keep.txt").read_text() == f"precious\n{COLLECTION}:/data\n"


def test_pipe_then_redirect(at_data, tmp_path, monkeypatch):
    """The last filter's output goes to the file."""
    monkeypatch.chdir(tmp_path)
    at_data.onecmd("find --type f | wc -l > count.txt")
    assert (tmp_path / "count.txt").read_text().strip() == "7"
    assert stat.S_IMODE((tmp_path / "count.txt").stat().st_mode) == 0o600


def test_lcd_moves_where_redirects_land(at_data, tmp_path, capsys):
    """Redirects are local, relative to lpwd."""
    at_data.onecmd(f"lcd {tmp_path}")
    at_data.onecmd("lpwd")
    assert capsys.readouterr().out.strip() == str(tmp_path)


# ---- local commands and scripts ----------------------------------------
def test_bang_runs_a_local_command(at_data, monkeypatch, capfd):
    """!command is the explicit way to do anything local."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    at_data.onecmd("!echo local")
    assert capfd.readouterr().out == "local\n"
    at_data.onecmd("!false")
    assert at_data.status == 1


def test_script_on_stdin(sh, monkeypatch, capsys):
    """Without a terminal, lines run in turn with no prompt."""
    monkeypatch.setattr("sys.stdin", io.StringIO("cd projects:\nls\n"))
    sh.interactive = False
    sh._set_prompt()
    assert sh.run() == 0
    assert "project-a" in capsys.readouterr().out


def test_script_exit_status_is_the_last_commands(sh, monkeypatch):
    """As in sh: a failing last line makes the script fail."""
    monkeypatch.setattr("sys.stdin", io.StringIO("cd projects:\ncd nowhere\n"))
    sh.interactive = False
    assert sh.run() == 1


def test_bad_start_location_exits(monkeypatch, client):
    """globus-fs shell nowhere: fails at once, with a status."""
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    fs._BOOKMARK_CACHE = None
    with pytest.raises(SystemExit) as caught:
        fs.main(["shell", f"{COLLECTION}:/data/nowhere"])
    assert caught.value.code == 1


# ---- completion ----------------------------------------------------------
def test_completion_lists_matching_names(at_data, client):
    """Tab completes from one listing, with / on directories."""
    assert at_data.completedefault("proj", "ls proj", 3, 7) == [
        "project-a/",
        "project-b/",
    ]
    assert at_data.completedefault("project-a/s", "ls project-a/s", 3, 14) == [
        "project-a/sub1/"
    ]


def test_completion_reuses_a_recent_listing(at_data, client):
    """Pressing Tab twice costs one API call."""
    at_data.completedefault("p", "ls p", 3, 4)
    at_data.completedefault("pr", "ls pr", 3, 5)
    assert client.ls_calls == ["/data"]


def test_cd_completes_directories_only(at_data):
    """cd never offers a file."""
    assert "readme.txt" not in at_data.completedefault("", "cd ", 3, 3)


def test_completion_never_raises(sh):
    """Before any cd there is nothing to complete, and no traceback."""
    assert sh.completedefault("x", "ls x", 3, 4) == []


def test_command_names_complete(sh):
    """Builtins and globus-fs commands, never 'shell'."""
    names = sh.completenames("")
    assert {"cd", "pwd", "ls", "find", "exit"} <= set(names)
    assert "shell" not in names


# ---- globus-fs stat, outside the shell ----------------------------------
def test_globus_fs_stat(run, capsys):
    """The new stat command works from the command line too."""
    run(["stat", f"{COLLECTION}:/data/readme.txt"])
    out = capsys.readouterr().out
    assert "Type: file" in out and "Size: 1024" in out and "-rw-r--r--" in out


def test_globus_fs_stat_json(run, capsys):
    """--format json carries the raw entry and where it is."""
    import json

    run(["stat", f"{COLLECTION}:/data/project-a", "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["type"] == "dir" and doc["path"] == "/data/project-a"


def test_an_unexpected_error_does_not_end_the_shell(at_data, monkeypatch, capsys):
    """A bug is reported, and the session survives it."""

    def broken(*a, **kw):
        """Fail as a bug would.

        :param a: ignored
        :param kw: ignored
        """
        raise KeyError(0)

    monkeypatch.setattr(at_data, "_run", broken)
    at_data.onecmd("ls")
    assert "internal error, please report" in capsys.readouterr().err
    assert at_data.status == 1
