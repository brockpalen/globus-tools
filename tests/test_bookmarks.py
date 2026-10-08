"""Bookmarks and target resolution.

Bookmarks are the real server-side Globus ones, shared with globus-cli and
the web app, so the command names and argument order match globus-cli.
"""

import pytest
from conftest import COLLECTION

from globus_tools import fs


@pytest.fixture(autouse=True)
def clear_bookmark_cache():
    """Drop the per-run bookmark cache between tests.

    :returns: nothing; the cache is cleared before and after each test
    """
    fs._BOOKMARK_CACHE = None
    yield
    fs._BOOKMARK_CACHE = None


class Target:
    """A stand-in for parsed arguments carrying only a target."""

    def __init__(self, target):
        """Record the target string.

        :param target: the ``COLLECTION:/path`` or bookmark spec
        """
        self.target = target


def test_uuid_target_is_absolute(client):
    """A raw UUID keeps ordinary absolute path semantics."""
    assert fs._resolve_target(Target(f"{COLLECTION}:/data/x"), client) == (
        COLLECTION,
        "/data/x",
    )


def test_bare_uuid_means_the_root(client):
    """No path after the colon is the collection root."""
    assert fs._resolve_target(Target(COLLECTION), client) == (COLLECTION, "/")


def test_bookmark_name_resolves_to_its_endpoint_and_path(client):
    """The whole point: a name instead of a UUID."""
    assert fs._resolve_target(Target("projects"), client) == (COLLECTION, "/data/")


def test_path_after_a_bookmark_is_relative_to_it(client):
    """A bookmark is a starting directory, so a suffix descends from it."""
    collection, path = fs._resolve_target(Target("projects:project-a"), client)
    assert (collection, path) == (COLLECTION, "/data/project-a")


def test_leading_slash_after_a_bookmark_is_still_relative(client):
    """`projects:/sub` means sub *within* the bookmark, not the root."""
    _, path = fs._resolve_target(Target("projects:/project-a"), client)
    assert path == "/data/project-a"


def test_bookmark_id_also_resolves(client):
    """globus-cli accepts an id or a name, so this does too."""
    assert fs._resolve_target(Target("bm-0002"), client)[1] == "/"


def test_unknown_name_lists_what_is_available(client, capsys):
    """A typo should show the names that would have worked."""
    with pytest.raises(SystemExit) as caught:
        fs._resolve_target(Target("nope"), client)
    assert "projects" in str(caught.value)


def test_bookmark_show_prints_the_substitution_form(run, client, capsys):
    """Default output feeds a subshell, matching globus-cli."""
    run(["bookmark", "show", "projects"], client)
    assert capsys.readouterr().out.strip() == f"{COLLECTION}:/data/"


def test_bookmark_show_verbose_lists_every_field(run, client, capsys):
    """-v is the human form."""
    run(["bookmark", "show", "projects", "-v"], client)
    out = capsys.readouterr().out
    assert "Name" in out and "Endpoint ID" in out and "bm-0001" in out


def test_bookmark_list_shows_names_and_ids(run, client, capsys):
    """The listing carries everything needed to use or delete a bookmark."""
    run(["bookmark", "list"], client)
    out = capsys.readouterr().out
    assert "projects" in out and "bm-0001" in out and COLLECTION in out


def test_bookmark_create_normalises_the_path(run, client):
    """The API wants a directory, so a trailing slash is added."""
    run(["bookmark", "create", f"{COLLECTION}:/scratch", "scratch"], client)
    assert client.created[0]["path"] == "/scratch/"
    assert client.created[0]["name"] == "scratch"


def test_bookmark_create_rejects_a_bare_path(run, client):
    """Without a collection there is nothing to bookmark."""
    with pytest.raises(SystemExit):
        run(["bookmark", "create", "/scratch", "scratch"], client)


def test_commands_accept_bookmarks_as_targets(run, client):
    """ls, du and find all take a name wherever they take a target."""
    transfer = run(["ls", "projects"], client)
    assert transfer.ls_calls == ["/data/"]
