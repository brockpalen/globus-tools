"""The token and shortcut file guards, and the SDK pieces they wrap.

The rule matches archivetar (brockpalen/archivetar#49): the token file must
be 600 and is tightened with a warning if not; ``~/.globus`` is shared with
globus-cli and created 700 only when missing.
"""

import io
import os
import stat
import sys

import pytest
from globus_sdk.login_flows import CommandLineLoginFlowEOFError
from globus_sdk.token_storage import JSONTokenStorage

from globus_tools import common, security
from globus_tools import shortcuts as _shortcuts
from globus_tools.security import check_shortcut_file


def mode(path):
    """Permission bits of a path.

    :param path: the file or directory
    :returns: e.g. ``0o600``
    """
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def token_file(tmp_path):
    """A token file with the given mode.

    :param tmp_path: pytest's per-test directory
    :returns: a callable taking a mode and returning the path
    """

    def make(file_mode):
        """Write the file and set its mode.

        :param file_mode: the permission bits to apply
        :returns: the file's path
        """
        path = tmp_path / "tokens.json"
        path.write_text("{}")
        os.chmod(path, file_mode)
        return str(path)

    return make


def test_private_file_is_left_alone(token_file):
    """600 is correct: no change, no warning."""
    path = token_file(0o600)
    assert security.check_token_file(path) is None
    assert mode(path) == 0o600


@pytest.mark.parametrize("loose", [0o644, 0o640, 0o664, 0o604, 0o660, 0o777])
def test_any_group_or_other_access_is_tightened_with_a_warning(token_file, loose):
    """Older tools wrote with the user's umask; fix it rather than refuse."""
    path = token_file(loose)
    warning = security.check_token_file(path)
    assert mode(path) == 0o600
    assert stat.filemode(stat.S_IFREG | loose) in warning
    assert "changed to -rw-------" in warning
    assert "revoke" in warning


def test_missing_file_is_not_an_error(tmp_path):
    """Before the first login there is nothing to check."""
    assert security.check_token_file(str(tmp_path / "absent.json")) is None


def test_file_owned_by_someone_else_is_refused(token_file, monkeypatch):
    """Another user's tokens cannot be fixed, and must not be used."""
    path = token_file(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(security.UnsafeTokenFile, match="not you"):
        security.check_token_file(path)


def test_missing_globus_dir_is_created_private(tmp_path):
    """A fresh ~/.globus is 700."""
    path = tmp_path / ".globus"
    security.ensure_globus_dir(str(path))
    assert mode(path) == 0o700


def test_existing_globus_dir_is_not_touched(tmp_path):
    """globus-cli makes ~/.globus 775; that must not block us (archivetar#49)."""
    path = tmp_path / ".globus"
    path.mkdir()
    os.chmod(path, 0o775)
    security.ensure_globus_dir(str(path))
    assert mode(path) == 0o775


# ---- wired into the app ---------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty HOME, so the SDK's token path lands in tmp_path.

    :param tmp_path: pytest's per-test directory
    :param monkeypatch: pytest's monkeypatch fixture
    :returns: the new HOME
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GLOBUS_FS_CLIENT_ID", raising=False)
    monkeypatch.delenv("GLOBUS_FS_CLIENT_SECRET", raising=False)
    return tmp_path


def sdk_token_path(home):
    """Where globus-sdk keeps globus-fs tokens under a given HOME.

    :param home: the HOME directory
    :returns: the token file path
    """
    return (
        home
        / ".globus"
        / "app"
        / common.DEFAULT_CLIENT_ID
        / "globus-fs"
        / "tokens.json"
    )


def test_tokens_live_where_the_sdk_puts_them(home):
    """The SDK's default location, next to globus-cli's, not a private dir."""
    tool = common.Tool("globus-fs")
    assert tool.token_file == str(sdk_token_path(home))
    assert mode(home / ".globus") == 0o700


def test_loose_token_file_is_tightened_before_the_app_uses_it(home, capsys):
    """An old 644 file is fixed, with a warning, as the app starts."""
    path = sdk_token_path(home)
    path.parent.mkdir(parents=True)
    # a real, empty token file in the SDK's own format, then loosened the
    # way an older tool writing under umask 022 would have left it
    JSONTokenStorage(str(path)).store_token_data_by_resource_server({})
    os.chmod(path, 0o644)
    common.Tool("globus-fs").app
    assert mode(path) == 0o600
    assert "were too open" in capsys.readouterr().err


def test_the_two_tools_keep_separate_tokens(home):
    """Logging out of one must not log you out of the other."""
    fs_file = common.Tool("globus-fs").token_file
    console_file = common.Tool("globus-console").token_file
    assert fs_file != console_file


def test_sdk_403_only_redrive_is_off(home):
    """Re-login is ours: the SDK's only fires on 403, HA sends 502."""
    assert common.Tool("globus-fs").app.config.auto_redrive_gares is False


# ---- login prompt on stderr -------------------------------------------------
@pytest.fixture
def flow():
    """The stderr login flow, on a native client that makes no requests.

    :returns: a :class:`common.StderrLoginFlowManager`
    """
    import globus_sdk

    client = globus_sdk.NativeAppAuthClient(common.DEFAULT_CLIENT_ID)
    return common.StderrLoginFlowManager(client)


def test_login_prompt_never_touches_stdout(flow, capsys, monkeypatch):
    """A login mid-`find | xargs` must not write into the pipe."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("the-code\n"))
    flow.print_authorize_url("https://auth.globus.org/example")
    assert flow.prompt_for_code() == "the-code"
    out, err = capsys.readouterr()
    assert out == ""
    assert "https://auth.globus.org/example" in err
    assert "Authorization Code" in err


def test_login_prompt_without_input_explains_itself(flow, monkeypatch):
    """No stdin gets the SDK's 'are you in a terminal?' error, not a hang."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(CommandLineLoginFlowEOFError):
        flow.prompt_for_code()


# ---- collection shortcut files ------------------------------------------------
LINE = "lab = 11111111-1111-1111-1111-111111111111\n"


def _file(tmp_path, mode, name="collections.conf"):
    """Write a shortcuts file with a given mode.

    :param tmp_path: pytest's temporary directory
    :param mode: the permission bits
    :param name: the file name
    :returns: its path, as a string
    """
    path = tmp_path / name
    path.write_text(LINE)
    os.chmod(path, mode)
    return str(path)


def test_personal_file_private_is_fine(tmp_path):
    """600 or 644: yours, nobody else can change it."""
    assert check_shortcut_file(_file(tmp_path, 0o644), personal=True) == (True, None)


def test_personal_file_group_writable_is_tightened(tmp_path):
    """Like the token file: write access for others is removed, with a warning."""
    path = _file(tmp_path, 0o664)
    usable, warning = check_shortcut_file(path, personal=True)
    assert usable and "removed group and other write" in warning
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644


def test_personal_file_owned_by_someone_else_is_ignored(tmp_path, monkeypatch):
    """Another user's file is not yours to trust or to fix."""
    path = _file(tmp_path, 0o644)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    usable, warning = check_shortcut_file(path, personal=True)
    assert not usable and "not you" in warning


def test_shared_file_group_writable_is_fine(tmp_path):
    """A lab's file, reached through its own module, may be group-writable."""
    assert check_shortcut_file(_file(tmp_path, 0o664), personal=False) == (True, None)


def test_shared_file_world_writable_is_ignored(tmp_path):
    """Anyone could redirect its shortcuts."""
    usable, warning = check_shortcut_file(_file(tmp_path, 0o666), personal=False)
    assert not usable and "anyone can write it" in warning


def test_file_in_a_world_writable_directory_is_ignored(tmp_path):
    """Without the sticky bit, anyone could replace the file."""
    shared = tmp_path / "open"
    shared.mkdir()
    path = _file(shared, 0o644)
    os.chmod(shared, 0o777)
    usable, warning = check_shortcut_file(path, personal=False)
    assert not usable and "could be replaced" in warning


def test_sticky_world_writable_directory_is_fine(tmp_path):
    """/tmp-style: the sticky bit stops others replacing your file."""
    shared = tmp_path / "sticky"
    shared.mkdir()
    path = _file(shared, 0o644)
    os.chmod(shared, 0o1777)
    assert check_shortcut_file(path, personal=False) == (True, None)


def test_missing_file_is_fine(tmp_path):
    """Nothing to read, nothing to check."""
    assert check_shortcut_file(str(tmp_path / "absent"), personal=True) == (True, None)


def test_ignored_file_loses_its_shortcuts_only(tmp_path, monkeypatch, capsys):
    """A world-writable module file is skipped; the user's file still works."""
    _shortcuts._WARNED.clear()
    shared = _file(tmp_path, 0o666, "lab.conf")
    mine = tmp_path / "mine.conf"
    mine.write_text("own = 22222222-2222-2222-2222-222222222222\n")
    monkeypatch.setenv(_shortcuts.ENV_VAR, shared)
    monkeypatch.delenv(_shortcuts.SHORTCUTS_VAR, raising=False)
    monkeypatch.setattr(_shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    loaded = _shortcuts.load(str(mine))
    assert "own" in loaded and "lab" not in loaded
    assert "anyone can write it" in capsys.readouterr().err


def test_warning_is_shown_once_per_run(tmp_path, monkeypatch, capsys):
    """The shell loads shortcuts for every prompt; warn once."""
    _shortcuts._WARNED.clear()
    shared = _file(tmp_path, 0o666, "lab.conf")
    monkeypatch.setenv(_shortcuts.ENV_VAR, shared)
    monkeypatch.setattr(_shortcuts, "DEFAULT_SITE_FILE", str(tmp_path / "absent"))
    for _ in range(3):
        _shortcuts.load()
    assert capsys.readouterr().err.count("anyone can write it") == 1
