"""Shell globs, as bash expands them, and commands that take several paths.

The fake tree under /data::

    project-a/  file1.bin  file2.bin  sub1/ (x.fastq.gz y.fastq.gz old.log)
    project-b/  big.tar    empty/
    readme.txt
"""

import json

import pytest
from conftest import COLLECTION, TREE, FakeTransferClient

from globus_tools import common, fs
from globus_tools.shell import GlobusShell, parse_words

C = COLLECTION


@pytest.fixture
def sh(monkeypatch, client):
    """A shell in /data, via the projects bookmark.

    :param monkeypatch: pytest's monkeypatch fixture
    :param client: the fake Transfer client
    :returns: the shell
    """
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    fs._BOOKMARK_CACHE = None
    shell = GlobusShell(fs.TOOL)
    shell.onecmd("cd projects:")
    yield shell
    fs._BOOKMARK_CACHE = None


def expand(shell, line):
    """Expand a line's words the way the shell will before running it.

    :param shell: the shell
    :param line: e.g. ``"ls *.txt"``
    :returns: the expanded arguments after the command name
    """
    words = parse_words(line)
    return shell.expand_words(words[0], words[1:])


# ---- what expands ---------------------------------------------------------
@pytest.mark.parametrize(
    "line, expected",
    [
        ("ls *.txt", ["readme.txt"]),
        ("ls project-*", ["project-a", "project-b"]),
        ("ls project-?", ["project-a", "project-b"]),
        ("ls project-[a]", ["project-a"]),
        ("ls project-[!a]", ["project-b"]),
        ("ls project-a/*.bin", ["project-a/file1.bin", "project-a/file2.bin"]),
        ("ls */*.bin", ["project-a/file1.bin", "project-a/file2.bin"]),
        ("ls */", ["project-a/", "project-b/"]),
        ("ls /data/*.txt", ["/data/readme.txt"]),
        ("ls projects:*.txt", ["projects:readme.txt"]),
        ("ls {readme.txt,project-b}", ["readme.txt", "project-b"]),
        (
            "ls project-{a,b}/*",
            [
                "project-a/file1.bin",
                "project-a/file2.bin",
                "project-a/sub1",
                "project-b/big.tar",
                "project-b/empty",
            ],
        ),
    ],
)
def test_expansion(sh, line, expected):
    """*, ?, [...], [!...], several levels, a name: prefix, and braces."""
    assert expand(sh, line) == expected


def test_matches_are_sorted(sh):
    """As bash sorts them."""
    assert expand(sh, "ls project-*/*") == sorted(expand(sh, "ls project-*/*"))


def test_star_skips_dotfiles(sh, monkeypatch):
    """* does not match a leading dot; .* does."""
    monkeypatch.setitem(TREE, "/data", TREE["/data"] + [(".hidden", "file", 1)])
    assert ".hidden" not in expand(sh, "ls *")
    assert expand(sh, "ls .*") == [".hidden"]


@pytest.mark.parametrize("line", ["ls '*.txt'", 'ls "*.txt"', "ls \\*.txt"])
def test_quoted_wildcards_stay_literal(sh, line):
    """Quoting keeps a * for a file that really has one in its name."""
    assert expand(sh, line) == ["*.txt"]


def test_options_are_not_expanded(sh):
    """A word starting with - is an option, even with a * in it."""
    assert expand(sh, "ls -l *.txt") == ["-l", "readme.txt"]


# ---- running it --------------------------------------------------------------
def test_ls_star(sh, capsys):
    """The everyday case."""
    sh.onecmd("ls -l *.txt")
    assert "/data/readme.txt" in capsys.readouterr().out
    assert sh.status == 0


def test_ls_several_directories_get_headings(sh, capsys):
    """As ls: each directory under a path: heading."""
    sh.onecmd("ls project-*")
    out = capsys.readouterr().out
    assert "/data/project-a:" in out and "/data/project-b:" in out
    assert "file1.bin" in out and "big.tar" in out


def test_no_match_is_an_error(sh, capsys, client):
    """failglob: nothing is run, and the shell says so."""
    sh.onecmd("ls *.nothing")
    assert "no match: *.nothing" in capsys.readouterr().err
    assert sh.status == 1


def test_mv_with_a_glob(sh, client):
    """mv *.bin dir/, the other everyday case."""
    sh.onecmd("mv project-a/*.bin project-b")
    assert client.rename_calls == [
        ("/data/project-a/file1.bin", "/data/project-b/file1.bin"),
        ("/data/project-a/file2.bin", "/data/project-b/file2.bin"),
    ]


def test_du_several_with_a_total(sh, capsys):
    """du -s -c project-*: one line each, then their sum."""
    sh.onecmd("du -s -b -c --no-header project-*")
    lines = capsys.readouterr().out.strip().splitlines()
    sizes = {line.split()[-1]: int(line.split()[0]) for line in lines}
    assert sizes["total"] == sizes["/data/project-a"] + sizes["/data/project-b"]


def test_stat_with_a_glob(sh, capsys):
    """stat *.txt."""
    sh.onecmd("stat *.txt")
    assert "Type: file" in capsys.readouterr().out


def test_cd_with_one_match(sh):
    """cd project-a/s* works when it is unambiguous."""
    sh.onecmd("cd project-a/s*")
    assert sh.here[1] == "/data/project-a/sub1"


def test_cd_with_several_matches_is_refused(sh, capsys):
    """cd proj* is ambiguous."""
    sh.onecmd("cd proj*")
    assert "matches 2" in capsys.readouterr().err
    assert sh.here[1] == "/data"


def test_archivescan_takes_one_path(sh, capsys):
    """archivescan project-* would be two scans; say so."""
    sh.onecmd("archivescan project-*")
    assert "takes one path" in capsys.readouterr().err


def test_find_patterns_are_not_expanded(sh, capsys):
    """find --name *.gz, unquoted, still reaches find as a pattern."""
    sh.onecmd("find --name *.gz")
    out = capsys.readouterr().out
    assert "x.fastq.gz" in out and "y.fastq.gz" in out


def test_glob_into_an_unlistable_directory(sh, monkeypatch, capsys):
    """An unlistable directory matches nothing; no traceback."""
    locked = FakeTransferClient(unlistable=["/data/project-a"])
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: locked)
    sh._tc = locked
    sh.onecmd("ls project-a/*")
    assert "no match" in capsys.readouterr().err


# ---- several paths on the command line ------------------------------------
def test_globus_fs_ls_several(run, capsys):
    """Files first, then each directory with a heading."""
    run(["ls", f"{C}:/data/readme.txt", f"{C}:/data/project-b"])
    out = capsys.readouterr().out
    assert out.index("/data/readme.txt") < out.index("/data/project-b:")


def test_globus_fs_ls_several_json(run, capsys):
    """One JSON list, each entry with its path."""
    run(["ls", f"{C}:/data/readme.txt", f"{C}:/data/project-b", "--format", "json"])
    paths = {e["path"] for e in json.loads(capsys.readouterr().out)}
    assert {"/data/readme.txt", "/data/project-b/big.tar"} <= paths


def test_globus_fs_ls_one_bad_path_does_not_stop_the_rest(run, capsys):
    """As ls: report it, list the others, exit 1."""
    with pytest.raises(SystemExit) as caught:
        run(["ls", f"{C}:/data/nope", f"{C}:/data/project-b"])
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert "big.tar" in captured.out and "nope" in captured.err


def test_globus_fs_stat_several(run, capsys):
    """One block each."""
    run(["stat", f"{C}:/data/readme.txt", f"{C}:/data/project-a"])
    out = capsys.readouterr().out
    assert out.count("Path:") == 2


def test_globus_fs_du_several_total(run, capsys):
    """du -c sums every path given."""
    run(
        [
            "du",
            "-s",
            "-b",
            "-c",
            "--no-header",
            f"{C}:/data/project-a",
            f"{C}:/data/project-b",
        ]
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].split()[-1] == "total"
    assert len(lines) == 3
