"""Client ID resolution, including the built-in default."""

import pytest

from globus_console_cli import common


@pytest.fixture
def tool(monkeypatch):
    """A tool with no client environment variables set.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: a :class:`common.Tool`
    """
    for var in ("CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"GLOBUS_FS_{var}", raising=False)
    return common.Tool("globus-fs")


def test_nothing_configured_uses_the_built_in_id(tool):
    """A user who configures nothing can still log in."""
    assert tool.get_client_id() == common.DEFAULT_CLIENT_ID


def test_environment_overrides_the_default(tool, monkeypatch):
    """A site can use its own registration."""
    monkeypatch.setenv("GLOBUS_FS_CLIENT_ID", "from-env")
    assert tool.get_client_id() == "from-env"


def test_a_secret_is_never_paired_with_the_published_id(tool, monkeypatch):
    """A service account secret needs its own ID; refuse rather than guess."""
    monkeypatch.setenv("GLOBUS_FS_CLIENT_SECRET", "s3cret")
    with pytest.raises(SystemExit) as exc:
        tool.get_client_id()
    assert "GLOBUS_FS_CLIENT_ID" in str(exc.value)
    assert common.DEFAULT_CLIENT_ID not in str(exc.value)


def test_a_secret_with_its_own_id_is_accepted(tool, monkeypatch):
    """The unattended setup keeps working."""
    monkeypatch.setenv("GLOBUS_FS_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("GLOBUS_FS_CLIENT_ID", "service-account")
    assert tool.get_client_id() == "service-account"
