"""The credential store guard.

``tokens.json`` holds a refresh token, so on a shared login node an exposed
file lets any local user act as its owner. These tests cover the checks that
make the tool refuse rather than use it.
"""

import os

import pytest

import globus_security as gsec


@pytest.fixture
def store(tmp_path):
    """A credential directory with a token file, correctly locked down.

    :param tmp_path: pytest's temporary directory
    :returns: ``(directory, token_file)`` as strings
    """
    directory = tmp_path / "store"
    directory.mkdir(mode=0o700)
    token = directory / "tokens.json"
    token.write_text('{"refresh_token": "secret"}')
    token.chmod(0o600)
    return str(directory), str(token)


def test_correct_permissions_pass(store):
    """0700 directory and 0600 file are accepted."""
    directory, token = store
    assert gsec.assert_secure(directory, token) == []


def test_missing_file_is_not_an_error(store, tmp_path):
    """A token file that does not exist yet is fine; login creates it."""
    directory, _ = store
    gsec.check_path(str(tmp_path / "store" / "nothing-here.json"))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o777])
def test_any_group_or_other_access_is_refused(store, mode):
    """Group or other access of any kind fails, as ssh does for private keys."""
    _, token = store
    os.chmod(token, mode)
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path(token)
    assert caught.value.reason == "mode"
    assert caught.value.fixable


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o770])
def test_exposed_directory_is_refused(store, mode):
    """A readable directory is refused even when the file itself is 0600."""
    directory, _ = store
    os.chmod(directory, mode)
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path(directory, must_be_dir=True)
    assert caught.value.reason == "mode"


def test_symlinked_token_file_is_refused(store, tmp_path):
    """A link could redirect the write or point at a file someone else owns."""
    directory, token = store
    os.unlink(token)
    os.symlink("/etc/passwd", token)
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path(token)
    assert caught.value.reason == "symlink"


def test_file_owned_by_another_user_is_refused():
    """Never read or write another user's credentials."""
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path("/etc/shadow")
    assert caught.value.reason == "owner"


def test_directory_where_a_file_is_expected_is_refused(store):
    """A directory standing in for the token file is rejected on type."""
    directory, _ = store
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path(directory)
    assert caught.value.reason == "type"


def test_fix_repairs_modes_and_reports_what_changed(store):
    """--fix-permissions tightens what this user owns."""
    directory, token = store
    os.chmod(directory, 0o755)
    os.chmod(token, 0o644)
    repaired = gsec.assert_secure(directory, token, fix=True)
    assert set(repaired) == {directory, token}
    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(token).st_mode & 0o777 == 0o600


def test_fix_never_changes_ownership():
    """Repairing modes must not make another user's store usable."""
    with pytest.raises(gsec.InsecureCredentialStore):
        gsec.assert_secure("/etc", "/etc/shadow", fix=True)


def test_secure_directory_creates_it_locked_down(tmp_path):
    """A fresh store is created 0700 rather than inheriting the umask."""
    directory = str(tmp_path / "fresh")
    gsec.secure_directory(directory)
    assert os.stat(directory).st_mode & 0o777 == 0o700


def test_secure_directory_refuses_an_exposed_existing_one(tmp_path):
    """An existing loose directory is refused, not silently tightened."""
    directory = tmp_path / "loose"
    directory.mkdir(mode=0o755)
    with pytest.raises(gsec.InsecureCredentialStore):
        gsec.secure_directory(str(directory))


def test_secure_directory_tightens_when_fixing(tmp_path):
    """With fix=True it repairs instead, so --fix-permissions can work.

    Without this the fix flag could never repair a loose directory: the
    check ran first and raised before the repair was reached.
    """
    directory = tmp_path / "loose"
    directory.mkdir(mode=0o755)
    gsec.secure_directory(str(directory), fix=True)
    assert os.stat(directory).st_mode & 0o777 == 0o700


def test_banner_names_the_file_and_the_fix(store):
    """The refusal has to tell the user what to do about it."""
    _, token = store
    os.chmod(token, 0o644)
    with pytest.raises(gsec.InsecureCredentialStore) as caught:
        gsec.check_path(token)
    banner = caught.value.banner()
    assert "0644" in banner
    assert token in banner
    assert "chmod 0600" in banner
    assert "Refusing to run" in banner


def test_exception_survives_copy(store):
    """All args go to super() so pickle and copy.copy() work."""
    import copy

    error = gsec.InsecureCredentialStore("/p", "mode", "detail", True)
    clone = copy.copy(error)
    assert str(clone) == "detail"


def test_format_mode():
    """Modes render as four octal digits."""
    assert gsec.format_mode(0o100644) == "0644"
    assert gsec.format_mode(0o40700) == "0700"
