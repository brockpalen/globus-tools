"""``find --batch``: output that ``globus delete --batch`` reads back exactly.

globus-cli reads batch input with ``stream.read().splitlines()`` and then
``shlex.split(line, comments=True)`` per line. :func:`read_like_globus_cli`
repeats that, so these tests check the round trip rather than the quoting.
"""

import shlex

import pytest
from conftest import COLLECTION, TREE, FakeTransferClient

from globus_console_cli import fs

AWKWARD = [
    "with space.txt",
    "it's quoted.txt",
    'double "quoted".txt',
    "#not-a-comment.txt",
    "star*.txt",
    "-rf",
    "tab\there.txt",
    "ünïcødé.txt",
    "$(touch pwned)",
]


def read_like_globus_cli(text):
    """Parse batch input the way ``globus delete --batch`` does.

    :param text: the batch file contents
    :returns: the path from each non-empty, non-comment line
    """
    paths = []
    for line in text.splitlines():
        argv = shlex.split(line, comments=True)
        if argv:
            assert len(argv) == 1, f"line split into several arguments: {line!r}"
            paths.append(argv[0])
    return paths


@pytest.fixture
def awkward_tree(monkeypatch):
    """Add a directory of hostile file names to the fake collection.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: the directory's path
    """
    names = AWKWARD + ["line\nbreak.txt", "carriage\rreturn.txt"]
    monkeypatch.setitem(TREE, "/odd", [(n, "file", 10) for n in names])
    return "/odd"


@pytest.mark.parametrize("name", AWKWARD)
def test_batch_line_round_trips(name):
    """Each awkward name comes back out of globus-cli's parser unchanged."""
    path = f"/data/{name}"
    assert read_like_globus_cli(fs.batch_line(path)) == [path]


@pytest.mark.parametrize("name", ["a\nb", "a\rb", "a\x0bb", "a\x0cb", "a\x85b", "a b"])
def test_batch_line_refuses_names_splitlines_would_break(name):
    """A name that splitlines() would cut in two cannot be written safely."""
    assert fs.batch_line(f"/data/{name}") is None


def test_find_batch_output_is_exactly_the_matches(run, capsys, awkward_tree):
    """Every representable match, and nothing else, reaches globus-cli."""
    run(["find", f"{COLLECTION}:{awkward_tree}", "--batch"])
    out, err = capsys.readouterr()
    assert sorted(read_like_globus_cli(out)) == sorted(f"/odd/{n}" for n in AWKWARD)
    assert "2 path(s) with line breaks" in err
    assert "'/odd/line\\nbreak.txt'" in err


def test_find_batch_header_names_the_collection_and_command(run, capsys):
    """A saved batch file says where its paths live and how to submit it."""
    run(["find", "projects:", "--type", "f", "--name", "*.log", "--batch"])
    out = capsys.readouterr().out
    header = [line for line in out.splitlines() if line.startswith("#")]
    assert f"#   globus delete --no-enable-globs --batch FILE {COLLECTION}" in header
    assert any("projects:" in line for line in header)
    # --type f lists no directories, so no --recursive warning
    assert not any("--recursive" in line for line in header)
    assert read_like_globus_cli(out) == ["/data/project-a/sub1/old.log"]


def test_find_batch_warns_about_directories_without_type_f(run, capsys):
    """Deleting a listed directory needs --recursive; say what that means."""
    run(["find", f"{COLLECTION}:/data", "--batch"])
    out = capsys.readouterr().out
    assert "--recursive" in out
    assert "/data/project-b/empty" in read_like_globus_cli(out)


@pytest.mark.parametrize("flag", ["--long", "--print0"])
def test_find_batch_rejects_other_output_forms(run, flag):
    """Mixing formats would corrupt the batch file."""
    with pytest.raises(SystemExit):
        run(["find", f"{COLLECTION}:/data", "--batch", flag], FakeTransferClient())
