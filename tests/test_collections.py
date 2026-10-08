"""Site-wide collection shortcuts.

An administrator ships ``name = uuid`` pairs so that nobody has to look up
a UUID for the collections everyone uses. A personal bookmark of the same
name still wins, the way user config beats ``/etc`` everywhere else.
"""

import os

import pytest
from conftest import COLLECTION

from globus_tools import fs, shortcuts

DATADEN = "ab65757f-00f5-4e5b-aa21-133187732a01"

SITE_FILE = f"""\
# ARC storage services
dataden = {DATADEN}
turbo   = 8e3a1e59-0000-0000-0000-000000000000
locker: 1a2b3c4d-0000-0000-0000-000000000000   # colons work too
greatlakes 5f6e7d8c-0000-0000-0000-000000000000:/scratch

# a blank line, a comment, and a malformed line follow
this line is not a shortcut at all
"""


@pytest.fixture(autouse=True)
def clear_bookmark_cache():
    """Drop the per-run bookmark cache around each test.

    :returns: nothing; the cache is cleared before and after
    """
    fs._BOOKMARK_CACHE = None
    yield
    fs._BOOKMARK_CACHE = None


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Install a site shortcuts file via the environment variable.

    :param tmp_path: pytest's temporary directory
    :param monkeypatch: pytest's monkeypatch fixture
    :returns: the path of the file that was written
    """
    path = tmp_path / "collections.conf"
    path.write_text(SITE_FILE)
    monkeypatch.setenv(shortcuts.ENV_VAR, str(path))
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent.conf"))
    monkeypatch.setattr(
        fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent.conf")
    )
    return str(path)


class Target:
    """A stand-in for parsed arguments carrying only a target."""

    def __init__(self, target):
        """Record the target string.

        :param target: the ``name:/path`` spec
        """
        self.target = target


def test_parses_equals_colon_and_bare_forms():
    """Administrators hand-edit this, so be forgiving about separators."""
    parsed = shortcuts.parse(SITE_FILE)
    assert set(parsed) == {"dataden", "turbo", "locker", "greatlakes"}
    assert parsed["dataden"].collection == DATADEN


def test_optional_base_path_is_kept():
    """A shortcut may point at a subdirectory, not just a collection root."""
    assert shortcuts.parse(SITE_FILE)["greatlakes"].path == "/scratch"


def test_bare_shortcut_defaults_to_the_root():
    """Without a path, the shortcut is the whole collection."""
    assert shortcuts.parse(SITE_FILE)["dataden"].path == "/"


def test_comments_and_junk_are_skipped():
    """One bad line must not break the file for everyone on the machine."""
    assert "this" not in shortcuts.parse(SITE_FILE)


def test_json_is_accepted_too():
    """A file starting with { is parsed as a JSON object of the same pairs."""
    parsed = shortcuts.parse('{"dataden": "%s", "turbo": "abc:/nfs/turbo"}' % DATADEN)
    assert parsed["dataden"].collection == DATADEN
    assert parsed["turbo"].path == "/nfs/turbo"


def test_malformed_json_is_not_fatal():
    """A truncated file yields no shortcuts rather than a traceback."""
    assert shortcuts.parse('{"dataden": ') == {}


@pytest.mark.parametrize("name", ["with space", "has:colon", "-leading", ""])
def test_invalid_names_are_rejected(name):
    """Names go on a command line, so no spaces and no colons."""
    assert not shortcuts.valid_name(name)


@pytest.mark.parametrize("name", ["dataden", "great-lakes", "turbo_2", "a.b+c"])
def test_valid_names_are_accepted(name):
    """Ordinary friendly names work."""
    assert shortcuts.valid_name(name)


def test_later_files_win(tmp_path, monkeypatch):
    """The user's file overrides one shipped by an administrator."""
    site = tmp_path / "site.conf"
    site.write_text("dataden = 11111111-1111-1111-1111-111111111111\n")
    user = tmp_path / "user.conf"
    user.write_text("dataden = 22222222-2222-2222-2222-222222222222\n")
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(site))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    merged = shortcuts.load(str(user))
    assert merged["dataden"].collection.startswith("2222")


def test_env_var_takes_several_files(tmp_path, monkeypatch):
    """Colon separated, so an Lmod module can add to what /etc ships."""
    first = tmp_path / "a.conf"
    first.write_text("a = 11111111-1111-1111-1111-111111111111\n")
    second = tmp_path / "b.conf"
    second.write_text("b = 22222222-2222-2222-2222-222222222222\n")
    monkeypatch.setenv(shortcuts.ENV_VAR, os.pathsep.join([str(first), str(second)]))
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    assert set(shortcuts.load()) == {"a", "b"}


def test_missing_files_are_ignored(monkeypatch, tmp_path):
    """Nothing configured is a normal state, not an error."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    assert shortcuts.load(str(tmp_path / "also-absent")) == {}


def test_shortcut_resolves_as_a_target(site, client):
    """The point of the feature: dataden:/path instead of a UUID."""
    assert fs._resolve_target(Target("dataden:/lab/x"), client) == (
        DATADEN,
        "/lab/x",
    )


def test_bare_shortcut_is_the_collection_root(site, client):
    """A name with no path lists the whole collection."""
    assert fs._resolve_target(Target("dataden"), client) == (DATADEN, "/")


def test_shortcut_with_a_base_path_resolves_beneath_it(site, client):
    """greatlakes:proj is proj underneath the configured /scratch."""
    _, path = fs._resolve_target(Target("greatlakes:proj"), client)
    assert path == "/scratch/proj"


def test_a_personal_bookmark_wins_over_a_shortcut(site, client, tmp_path, monkeypatch):
    """User config beats site config, as it does everywhere else."""
    path = tmp_path / "shadow.conf"
    path.write_text(f"projects = {DATADEN}\n")
    monkeypatch.setenv(shortcuts.ENV_VAR, str(path))
    collection, resolved = fs._resolve_target(Target("projects"), client)
    assert collection == COLLECTION  # the bookmark's, not the shortcut's
    assert resolved == "/data/"


def test_unknown_name_lists_shortcuts_and_bookmarks(site, client):
    """A typo should show everything that would have worked."""
    with pytest.raises(SystemExit) as caught:
        fs._resolve_target(Target("nope"), client)
    message = str(caught.value)
    assert "dataden" in message
    assert "projects" in message


def test_collections_command_lists_them(site, run, capsys):
    """Users need a way to see what names exist without reading /etc."""
    run(["collections"])
    out = capsys.readouterr().out
    assert "dataden" in out and DATADEN in out


def test_collections_command_explains_when_none_are_configured(
    run, capsys, monkeypatch, tmp_path
):
    """An empty listing should say where to put the file."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    monkeypatch.setattr(fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent"))
    run(["collections"])
    out = capsys.readouterr().out
    assert "No collection shortcuts are configured" in out
    assert "Looked in:" in out


def test_empty_state_lists_every_source_including_an_unset_env_var(
    run, capsys, monkeypatch, tmp_path
):
    """Showing the variable even when unset explains the mechanism exists."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    monkeypatch.setattr(fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent"))
    run(["collections"])
    out = capsys.readouterr().out
    assert shortcuts.ENV_VAR in out
    assert "not set" in out


def test_empty_state_shows_no_realistic_looking_shortcut(
    run, capsys, monkeypatch, tmp_path
):
    """A concrete example here reads as though that shortcut already exists.

    Someone hit exactly this: the empty state showed
    ``dataden = ab65757f-...`` as an example, so they ran
    ``du dataden:/path`` and it failed. The template must be obviously a
    template.
    """
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    monkeypatch.setattr(fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent"))
    run(["collections"])
    out = capsys.readouterr().out
    assert "<name> = <collection-uuid>" in out
    assert DATADEN not in out
    assert "dataden =" not in out


def test_empty_state_points_at_bookmarks_for_a_single_user(
    run, capsys, monkeypatch, tmp_path
):
    """A user wanting one name should be sent to bookmarks, not a config file."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    monkeypatch.setattr(fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent"))
    run(["collections"])
    assert "bookmark create" in capsys.readouterr().out


def test_unknown_name_says_when_no_shortcuts_are_configured(
    client, monkeypatch, tmp_path
):
    """Omitting the line silently leaves the reader guessing why."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv(shortcuts.ENV_VAR, raising=False)
    monkeypatch.setattr(fs, "user_shortcut_file", lambda: str(tmp_path / "user-absent"))
    with pytest.raises(SystemExit) as caught:
        fs._resolve_target(Target("dataden"), client)
    message = str(caught.value)
    assert "none configured on this system" in message
    assert "A collection UUID always works" in message


def test_unknown_name_says_when_you_have_no_bookmarks(site, monkeypatch):
    """The same courtesy for the other source."""
    from conftest import FakeTransferClient

    empty = FakeTransferClient()
    monkeypatch.setattr(empty, "bookmark_list", lambda: [])
    with pytest.raises(SystemExit) as caught:
        fs._resolve_target(Target("nope"), empty)
    assert "none yet" in str(caught.value)


def test_describe_sources_labels_each_one(tmp_path, monkeypatch):
    """The listing says what each location is for, not just its path."""
    monkeypatch.setattr(shortcuts, "DEFAULT_SITE_FILE", "/etc/x.conf")
    monkeypatch.setenv(shortcuts.ENV_VAR, "/from/module.conf")
    rows = shortcuts.describe_sources(
        "/home/u/.globus/app/x/globus-fs/collections.conf"
    )
    assert rows[0] == ("/etc/x.conf", "site-wide, shipped by an administrator")
    assert rows[1][1] == f"from ${shortcuts.ENV_VAR}"
    assert rows[-1][1] == "yours"


def test_user_file_sits_beside_the_token_file(tmp_path, monkeypatch):
    """Everything per-user in one directory, like ~/.ssh."""
    from globus_tools import common

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GLOBUS_FS_CLIENT_ID", raising=False)
    monkeypatch.setattr(fs, "TOOL", common.Tool("globus-fs"))
    expected = tmp_path / ".globus/app" / common.DEFAULT_CLIENT_ID / "globus-fs"
    assert fs.user_shortcut_file() == str(expected / "collections.conf")
    assert os.path.dirname(fs.TOOL.token_file) == str(expected)
