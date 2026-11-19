"""Naming where to go: bare names to start the shell, hints, display names.

From a tester's feedback: "collection name" read as the Globus name from
the web app, and the shell wanted ``name:`` where ls takes a bare name.
"""

import pytest
from conftest import COLLECTION, FakeTransferClient

from globus_tools import common, fs
from globus_tools.shell import GlobusShell

DATADEN = {
    "id": "ab65757f-00f5-4e5b-aa21-133187732a01",
    "display_name": "UMich ARC Non-Sensitive Data Den Volume Collection",
    "owner_string": "arc@umich.edu",
    "entity_type": "GCSv5_mapped_collection",
}
ENDPOINT = {
    "id": "eeee0000-0000-0000-0000-000000000000",
    "display_name": "Data Den endpoint",
    "entity_type": "GCSv5_endpoint",
}


@pytest.fixture
def client():
    """A fake Transfer client whose search knows Data Den.

    :returns: the client
    """
    fake = FakeTransferClient()
    fake.search_results = [DATADEN, ENDPOINT]
    return fake


@pytest.fixture
def shell(monkeypatch, client):
    """A shell factory over the fake collection.

    :param monkeypatch: pytest's monkeypatch fixture
    :param client: the fake Transfer client
    :returns: a function taking the start target
    """
    monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: client)
    monkeypatch.setattr(fs, "site_shortcuts", lambda: {})
    fs._BOOKMARK_CACHE = None

    def make(start=None):
        """Build a shell.

        :param start: the target given to globus-fs shell
        :returns: the shell
        """
        return GlobusShell(fs.TOOL, start=start)

    yield make
    fs._BOOKMARK_CACHE = None


# ---- the shell's start target -----------------------------------------------
@pytest.mark.parametrize("start", ["projects", "projects:", "projects:/"])
def test_start_with_a_bare_bookmark_name(shell, monkeypatch, start):
    """globus-fs shell projects starts at the bookmark, as ls projects does."""
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    sh = shell(start)
    sh.interactive = False
    assert sh.run() == 0
    assert sh.here[:2] == (COLLECTION, "/data")


def test_start_with_a_bare_uuid(shell, monkeypatch):
    """A UUID alone starts at its root."""
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    sh = shell(COLLECTION)
    sh.interactive = False
    assert sh.run() == 0
    assert sh.here[:2] == (COLLECTION, "/")


def test_unknown_start_name_explains_and_suggests(shell, capsys):
    """A name Globus knows only as a display name is pointed to its UUID."""
    sh = shell("Den")
    assert sh.run() == 1
    err = capsys.readouterr().err
    assert "not a collection UUID, a bookmark or a collection shortcut" in err
    assert DATADEN["id"] in err and "bookmark create" in err
    assert ENDPOINT["id"] not in err  # endpoints themselves are not collections


# ---- cd hints -------------------------------------------------------------------
def test_cd_bare_name_means_a_directory_but_hints_at_the_bookmark(shell, capsys):
    """cd root is a subdirectory, as in any shell; say how to reach the bookmark."""
    sh = shell()
    sh.onecmd("cd projects:")
    sh.onecmd("cd root")
    err = capsys.readouterr().err
    assert "For your bookmark of that name: cd root:" in err
    assert sh.here[1] == "/data"


def test_cd_unknown_name_colon_suggests_display_names(shell, capsys):
    """cd den: names nothing; Globus collections that match are suggested."""
    sh = shell()
    sh.onecmd("cd projects:")
    sh.onecmd("cd Den:")
    err = capsys.readouterr().err
    assert "no bookmark or collection shortcut named Den" in err
    assert DATADEN["id"] in err


def test_cd_missing_plain_directory_has_no_name_hint(shell, capsys, client):
    """An ordinary typo gets the API's message, and no search is made."""
    sh = shell()
    sh.onecmd("cd projects:")
    sh.onecmd("cd nowhere")
    assert "no such path" in capsys.readouterr().err
    assert client.searches == []


# ---- the command line -------------------------------------------------------------
def test_unknown_target_suggests_display_names(run, client):
    """globus-fs ls Den: the error lists matching Globus collections."""
    with pytest.raises(SystemExit) as caught:
        run(["ls", "Den:/x"], client)
    message = caught.value.code
    assert "collection shortcut" in message and DATADEN["id"] in message


def test_unknown_target_with_no_match_still_explains(run):
    """No display-name match: the usual list of names, nothing invented."""
    with pytest.raises(SystemExit) as caught:
        run(["ls", "zzz:/x"])
    assert "Globus collections matching" not in caught.value.code


def test_collections_search(run, client, capsys):
    """collections --search finds a UUID by the name the web app shows."""
    run(["collections", "--search", "data den"], client)
    out = capsys.readouterr().out
    assert DATADEN["id"] in out and "Data Den Volume Collection" in out
    assert ENDPOINT["id"] not in out


def test_collections_search_json(run, client, capsys):
    """--format json gives the documents."""
    import json

    run(["collections", "--search", "data den", "--format", "json"], client)
    assert json.loads(capsys.readouterr().out)[0]["id"] == DATADEN["id"]


def test_collections_search_no_match(run, client):
    """Nothing found is an error, said plainly."""
    with pytest.raises(SystemExit) as caught:
        run(["collections", "--search", "nothing like it"], client)
    assert "no Globus collection matches" in caught.value.code


def test_help_says_shortcut_not_collection_name():
    """The wording that confused a tester is gone."""
    parser = fs.build_parser()
    sub = parser._subparsers._group_actions[0].choices
    text = sub["shell"].format_help() + sub["ls"].format_help()
    assert "collection shortcut" in text
    assert "collection name" not in text.replace("\n", " ").replace("  ", " ")


def test_whole_text_matches_rank_first(run, client):
    """Globus's search is fuzzy; a name holding the whole text comes first."""
    client.search_results = [
        {"id": "denali-0000", "display_name": "RCC Denali Cluster"},
        dict(DATADEN),
    ]
    client.endpoint_search = lambda filter_fulltext=None, **kw: [
        dict(e) for e in client.search_results
    ]
    hint = fs.display_name_hint(client, "Data Den")
    assert hint.index(DATADEN["id"]) < hint.index("denali-0000")
    assert f"bookmark create {DATADEN['id']}:/ <name>" in hint  # "Data Den" has a space


def test_a_one_word_name_is_suggested_as_the_bookmark(client):
    """A valid name is offered as the bookmark name."""
    client.search_results = [dict(DATADEN, display_name="Dataden archive")]
    assert "bookmark create " + DATADEN["id"] + ":/ dataden" in fs.display_name_hint(
        client, "dataden"
    )
