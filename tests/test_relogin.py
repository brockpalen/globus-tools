"""Logging in again when the API asks, including high assurance.

A high assurance collection reports an expired or wrong-domain session from
a directory listing as an HTTP 502 carrying ``authorization_parameters`` --
which the SDK's own 403-only re-login ignores. These tests use that exact
shape (``conftest.ha_session_error``).
"""

import sys
import threading

import pytest
from conftest import COLLECTION, FakeAPIError, FakeTransferClient, ha_session_error

from globus_tools import common, walk


class FakeApp:
    """Records logins instead of opening a browser flow."""

    def __init__(self):
        """Start with no logins."""
        self.logins = []

    def login(self, auth_params=None):
        """Record one login and what it was asked to satisfy.

        :param auth_params: the GARE's authorization parameters
        """
        self.logins.append(auth_params)


@pytest.fixture
def tool(monkeypatch):
    """A Tool whose app records logins, on a terminal.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: the tool
    """
    t = common.Tool("globus-fs")
    t._app = FakeApp()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    return t


def failing_once(error):
    """An API call that fails with ``error`` the first time only.

    :param error: the error to raise first
    :returns: the callable, with a ``calls`` counter
    """

    def fn():
        """Fail once, then succeed.

        :returns: ``"ok"`` after the first call
        """
        fn.calls += 1
        if fn.calls == 1:
            raise error
        return "ok"

    fn.calls = 0
    return fn


def test_ha_502_logs_in_for_the_required_domain_and_retries(tool):
    """The case the SDK misses: a 502 that is really a login demand."""
    fn = failing_once(ha_session_error())
    assert tool.call(fn) == "ok"
    assert fn.calls == 2
    (params,) = tool.app.logins
    assert params.session_required_single_domain == ["umich.edu"]


def test_consent_403_also_logs_in(tool):
    """GCS v5 consent arrives as the usual 403 GARE."""
    consent = FakeAPIError(
        403,
        "ConsentRequired",
        "Missing required data_access consent",
        raw_json={
            "code": "ConsentRequired",
            "required_scopes": ["urn:globus:auth:scope:transfer.api.globus.org:all"],
        },
    )
    assert tool.call(failing_once(consent)) == "ok"
    assert len(tool.app.logins) == 1


def test_an_ordinary_error_never_triggers_a_login(tool):
    """PermissionDenied is a real answer, not a prompt to log in."""
    denied = FakeAPIError(403, "PermissionDenied", "no")
    with pytest.raises(FakeAPIError):
        tool.call(failing_once(denied))
    assert tool.app.logins == []


def test_without_a_terminal_the_error_is_raised_not_prompted(tool, monkeypatch):
    """Cron and pipes from stdin cannot answer a login prompt."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(FakeAPIError):
        tool.call(failing_once(ha_session_error()))
    assert tool.app.logins == []


def test_parallel_failures_produce_one_login(tool):
    """Eight walker threads hitting an expired session log in once."""
    barrier = threading.Barrier(8)
    expired = threading.Event()
    expired.set()

    def listing():
        """Fail for every thread until a login has happened.

        :returns: ``"ok"`` once logged in
        """
        if expired.is_set():
            barrier.wait(timeout=5)  # all eight fail together
            raise ha_session_error()
        return "ok"

    original_login = tool.app.login

    def login(auth_params=None):
        """Log in, which renews the session.

        :param auth_params: the GARE's authorization parameters
        """
        original_login(auth_params)
        expired.clear()

    tool.app.login = login
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(tool.call(listing)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert results == ["ok"] * 8
    assert len(tool.app.logins) == 1


def test_walker_does_not_retry_a_login_demand(monkeypatch):
    """A 502 GARE is not a flaky server: no sleeps, no three retries."""
    client = FakeTransferClient()
    calls = []

    def ls(collection, path=None, **kwargs):
        """Always demand a login.

        :param collection: ignored
        :param path: recorded
        :param kwargs: ignored
        :raises FakeAPIError: always
        """
        calls.append(path)
        raise ha_session_error()

    client.operation_ls = ls
    monkeypatch.setattr(walk.time, "sleep", lambda s: pytest.fail("retried"))
    walker = walk.Walker(client, COLLECTION)
    assert list(walker.walk("/raeker2")) == []
    assert calls == ["/raeker2"]
    assert walker.errors[0].needs_login


def test_find_without_a_terminal_explains_how_to_log_in(run, capsys, monkeypatch):
    """The non-interactive message says what to do, then exits non-zero."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    client = FakeTransferClient()

    def ls(collection, path=None, **kwargs):
        """Always demand a login.

        :param collection: ignored
        :param path: ignored
        :param kwargs: ignored
        :raises FakeAPIError: always
        """
        raise ha_session_error()

    client.operation_ls = ls
    with pytest.raises(SystemExit) as exc:
        run(["find", f"{COLLECTION}:/raeker2"], client)
    assert exc.value.code == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_relogin_forces_a_fresh_sign_in(tool):
    """prompt=login, as globus-cli: an old browser session must not count.

    The real failure: umich.edu had signed in 11 hours earlier, Globus
    accepted that session without asking, and the collection refused it.
    """
    tool.call(failing_once(ha_session_error()))
    (params,) = tool.app.logins
    assert params.prompt == "login"
    assert params.session_required_single_domain == ["umich.edu"]


def test_refused_after_login_names_the_identity_to_use(tool, capsys):
    """In a terminal, 'run it in a terminal' is the wrong advice."""
    with pytest.raises(SystemExit) as exc:
        tool.api_error(ha_session_error())
    assert "umich.edu" in str(exc.value)
    assert "interactive terminal" not in str(exc.value)


@pytest.mark.parametrize("domain", ["example.edu", "uni.ac.uk"])
def test_required_domain_comes_from_the_collection_not_the_code(tool, domain):
    """Portable: whatever domain a collection's policy names is what we ask for."""
    tool.call(failing_once(ha_session_error(domain)))
    (params,) = tool.app.logins
    assert params.session_required_single_domain == [domain]
    with pytest.raises(SystemExit) as exc:
        tool.api_error(ha_session_error(domain))
    assert domain in str(exc.value)
    assert "umich" not in str(exc.value)
