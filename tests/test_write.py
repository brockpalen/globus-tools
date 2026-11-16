"""mkdir and mv: the first commands that change a collection.

mv is Globus rename, so it only works within one collection, and it never
replaces anything: on shared or CUI storage a silent overwrite is worse
than an error.
"""

import pytest
from conftest import COLLECTION, FakeTransferClient

from globus_tools import common, fs
from globus_tools.shell import GlobusShell

C = COLLECTION
OTHER = "dddd2222-0000-0000-0000-000000000002"


def names(client, path):
    """Names in one directory of the client's tree.

    :param client: the fake Transfer client
    :param path: the directory
    :returns: the set of names listed there
    """
    return {n for n, _, _ in client.tree[path]}


# ---- mkdir ------------------------------------------------------------------
def test_mkdir_creates_one_directory(run):
    """One API call, and it is there."""
    client = run(["mkdir", f"{C}:/data/new"])
    assert client.mkdir_calls == ["/data/new"]
    assert "new" in names(client, "/data")


def test_mkdir_several(run):
    """Each argument is a directory."""
    client = run(["mkdir", f"{C}:/data/a", f"{C}:/data/b"])
    assert {"a", "b"} <= names(client, "/data")


def test_mkdir_existing_is_an_error(run, capsys):
    """Without -p, as mkdir."""
    with pytest.raises(SystemExit) as caught:
        run(["mkdir", f"{C}:/data/project-a"])
    assert "already exists" in caught.value.code


def test_mkdir_p_creates_parents(run):
    """-p makes each missing level, top down, and skips existing ones."""
    client = run(["mkdir", "-p", f"{C}:/data/project-a/x/y/z"])
    assert client.mkdir_calls == [
        "/data/project-a/x",
        "/data/project-a/x/y",
        "/data/project-a/x/y/z",
    ]


def test_mkdir_p_existing_is_fine(run):
    """-p on a directory that exists does nothing, as mkdir -p."""
    client = run(["mkdir", "-p", f"{C}:/data/project-a"])
    assert client.mkdir_calls == []


def test_mkdir_p_refuses_a_file_in_the_way(run):
    """A file where a parent should be is an error."""
    with pytest.raises(SystemExit) as caught:
        run(["mkdir", "-p", f"{C}:/data/readme.txt/sub"])
    assert "not a directory" in caught.value.code


def test_mkdir_missing_parent_without_p(run):
    """No -p, no parent: the API's error."""
    with pytest.raises(SystemExit):
        run(["mkdir", f"{C}:/data/nope/sub"])


def test_mkdir_verbose(run, capsys):
    """-v names each directory."""
    run(["mkdir", "-v", f"{C}:/data/new"])
    assert "created directory /data/new" in capsys.readouterr().out


# ---- mv -----------------------------------------------------------------------
def test_mv_renames(run):
    """SRC DEST with DEST absent: a rename."""
    client = run(["mv", f"{C}:/data/readme.txt", f"{C}:/data/README"])
    assert client.rename_calls == [("/data/readme.txt", "/data/README")]
    assert "README" in names(client, "/data") and "readme.txt" not in names(
        client, "/data"
    )


def test_mv_into_a_directory(run):
    """DEST an existing directory: SRC moves into it, keeping its name."""
    client = run(["mv", f"{C}:/data/readme.txt", f"{C}:/data/project-b"])
    assert client.rename_calls == [("/data/readme.txt", "/data/project-b/readme.txt")]


def test_mv_several_into_a_directory(run):
    """SRC... DIR, as mv."""
    client = run(
        [
            "mv",
            f"{C}:/data/project-a/file1.bin",
            f"{C}:/data/project-a/file2.bin",
            f"{C}:/data/project-b/empty",
        ]
    )
    assert names(client, "/data/project-b/empty") == {"file1.bin", "file2.bin"}


def test_mv_a_directory_moves_its_contents(run):
    """Renaming a directory takes everything under it along."""
    client = run(["mv", f"{C}:/data/project-a", f"{C}:/data/renamed"])
    assert "x.fastq.gz" in names(client, "/data/renamed/sub1")


def test_mv_several_need_a_directory(run, capsys):
    """Several sources and a file or absent DEST: refused, nothing moved."""
    with pytest.raises(SystemExit) as caught:
        run(["mv", f"{C}:/data/readme.txt", f"{C}:/data/project-a", f"{C}:/data/x"])
    assert "is not a directory" in caught.value.code


def test_mv_never_overwrites_a_file(run, capsys):
    """An existing DEST file is refused, not replaced."""
    client = FakeTransferClient()
    with pytest.raises(SystemExit) as caught:
        run(
            ["mv", f"{C}:/data/project-a/file1.bin", f"{C}:/data/project-a/file2.bin"],
            client,
        )
    assert caught.value.code == 1
    assert "never overwrites" in capsys.readouterr().err
    assert client.rename_calls == []


def test_mv_never_overwrites_inside_a_directory(run, capsys):
    """DIR/name already there is refused too."""
    client = FakeTransferClient()
    client.operation_mkdir(C, "/data/project-b/readme.txt")
    with pytest.raises(SystemExit):
        run(["mv", f"{C}:/data/readme.txt", f"{C}:/data/project-b"], client)
    assert "never overwrites" in capsys.readouterr().err
    assert client.rename_calls == []


def test_mv_refuses_another_collection(run, capsys):
    """Between collections is a transfer, not a rename."""
    client = FakeTransferClient()
    with pytest.raises(SystemExit) as caught:
        run(["mv", f"{C}:/data/readme.txt", f"{OTHER}:/x"], client)
    assert "different collections" in caught.value.code
    assert client.rename_calls == []


def test_mv_refuses_into_itself(run, capsys):
    """A directory cannot go inside itself."""
    client = FakeTransferClient()
    with pytest.raises(SystemExit):
        run(["mv", f"{C}:/data/project-a", f"{C}:/data/project-a/sub1"], client)
    assert "into itself" in capsys.readouterr().err
    assert client.rename_calls == []


def test_mv_refuses_the_root(run, capsys):
    """/ is not movable."""
    with pytest.raises(SystemExit):
        run(["mv", f"{C}:/", f"{C}:/data/x"])
    assert "collection root" in capsys.readouterr().err


def test_mv_cross_filesystem_is_explained_not_copied(run, capsys):
    """A cross-device rename fails with advice; nothing is copied."""
    client = FakeTransferClient(exdev=["/data/project-b"])
    with pytest.raises(SystemExit) as caught:
        run(["mv", f"{C}:/data/readme.txt", f"{C}:/data/project-b/readme.txt"], client)
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "different filesystems" in err and "globus transfer" in err
    assert "readme.txt" in names(client, "/data")


def test_mv_one_failure_does_not_stop_the_rest(run, capsys):
    """As mv: report the bad one, move the others, exit 1."""
    client = FakeTransferClient()
    client.operation_mkdir(C, "/data/project-b/empty/file1.bin")
    with pytest.raises(SystemExit) as caught:
        run(
            [
                "mv",
                f"{C}:/data/project-a/file1.bin",
                f"{C}:/data/project-a/file2.bin",
                f"{C}:/data/project-b/empty",
            ],
            client,
        )
    assert caught.value.code == 1
    assert "file2.bin" in names(client, "/data/project-b/empty")


def test_mv_needs_two_paths(run):
    """One path is not enough."""
    with pytest.raises(SystemExit) as caught:
        run(["mv", f"{C}:/data/readme.txt"])
    assert "source and a destination" in caught.value.code


def test_mv_verbose(run, capsys):
    """-v names each rename."""
    run(["mv", "-v", f"{C}:/data/readme.txt", f"{C}:/data/README"])
    assert "renamed /data/readme.txt -> /data/README" in capsys.readouterr().out


# ---- in the shell -------------------------------------------------------------
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


def test_shell_mkdir_and_mv_take_relative_paths(sh, client):
    """Relative to the current directory, like every other command."""
    sh.onecmd("mkdir -p project-a/out/2026")
    sh.onecmd("mv readme.txt project-a/out")
    assert client.mkdir_calls == ["/data/project-a/out", "/data/project-a/out/2026"]
    assert client.rename_calls == [
        ("/data/readme.txt", "/data/project-a/out/readme.txt")
    ]
    assert sh.status == 0


def test_shell_completion_sees_a_new_directory(sh, client):
    """The completion cache is dropped after a write."""
    sh.completedefault("", "cd ", 3, 3)
    sh.onecmd("mkdir fresh")
    assert "fresh/" in sh.completedefault("f", "cd f", 3, 4)


def test_shell_notices_its_directory_moved(sh, client, capsys):
    """mv of the current directory: say where things stand."""
    sh.onecmd("cd project-a")
    sh.onecmd("mv /data/project-a /data/moved")
    assert "was moved" in capsys.readouterr().err


def test_shell_failed_mv_sets_status(sh, capsys):
    """A refused move is status 1, the shell carries on."""
    sh.onecmd("mv project-a/file1.bin project-a/file2.bin")
    assert sh.status == 1
    sh.onecmd("pwd")
    assert sh.status == 0
