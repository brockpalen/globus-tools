"""Client ID resolution, including the built-in default."""

import pytest

from globus_console_cli import common


@pytest.fixture
def tool(tmp_path, monkeypatch):
    """A tool with an empty private config dir and no client env vars.

    :param tmp_path: pytest's per-test directory
    :param monkeypatch: pytest's monkeypatch fixture
    :returns: a :class:`common.Tool`
    """
    for var in ("HOME", "CLIENT_ID", "CLIENT_SECRET"):
        monkeypatch.delenv(f"GLOBUS_FS_{var}", raising=False)
    monkeypatch.setenv("GLOBUS_FS_HOME", str(tmp_path / "cfg"))
    return common.Tool("globus-fs")


def test_nothing_configured_uses_the_built_in_id(tool):
    """A user who configures nothing can still log in."""
    assert tool.get_client_id() == common.DEFAULT_CLIENT_ID


def test_every_override_beats_the_default(tool, monkeypatch):
    """Saved config, then environment, then the command line win in turn."""
    tool.save_config({"client_id": "saved"})
    assert tool.get_client_id() == "saved"
    monkeypatch.setenv("GLOBUS_FS_CLIENT_ID", "from-env")
    assert tool.get_client_id() == "from-env"
    assert tool.get_client_id("from-flag") == "from-flag"


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
