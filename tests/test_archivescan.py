"""``archivescan``: how much of a tree is big enough for Data Den."""

import json

import pytest
from conftest import COLLECTION, GB, MB, TREE, FakeTransferClient

from globus_console_cli import fs

MIB = 1024 * 1024


@pytest.fixture
def scan_tree(monkeypatch):
    """A tree with files either side of 100 MiB, a snapshot and symlinks.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: the tree's root path
    """
    monkeypatch.setitem(
        TREE,
        "/lab",
        [
            ("big.tar", "file", 5 * GB),
            ("exactly.bin", "file", 100 * MIB),  # at the threshold: cache
            ("just-over.bin", "file", 100 * MIB + 1),
            ("small.txt", "file", 10),
            ("link-to-big", "filelink", 5 * GB),
            ("linked-dir", "dirlink", 0),
            (".snapshot", "dir", 0),
            ("sub", "dir", 0),
        ],
    )
    monkeypatch.setitem(TREE, "/lab/sub", [("a.fastq", "file", 50 * MB)])
    monkeypatch.setitem(TREE, "/lab/.snapshot", [("big.tar", "file", 5 * GB)])
    monkeypatch.setitem(TREE, "/lab/linked-dir", [("x", "file", 9 * GB)])
    monkeypatch.delenv(fs.ARCHIVESCAN_SIZE_VAR, raising=False)
    return "/lab"


def scan(run, capsys, *extra):
    """Run archivescan over the scan tree and return its JSON result.

    :param run: the globus-fs runner fixture
    :param capsys: pytest's output capture
    :param extra: additional command line arguments
    :returns: ``(result, client)``
    """
    client = FakeTransferClient()
    run(["archivescan", f"{COLLECTION}:/lab", "--format", "json", *extra], client)
    return json.loads(capsys.readouterr().out), client


def test_splits_at_100_mib_with_equal_going_to_cache(run, capsys, scan_tree):
    """Larger than the threshold is Data Den; equal to it is cache."""
    result, _ = scan(run, capsys)
    assert result["threshold_bytes"] == 100 * MIB
    assert result["dataden_files"] == 2  # big.tar, just-over.bin
    assert result["dataden_bytes"] == 5 * GB + 100 * MIB + 1
    assert result["cache_files"] == 3  # exactly.bin, small.txt, sub/a.fastq
    assert result["cache_bytes"] == 100 * MIB + 10 + 50 * MB


def test_skips_snapshots_and_symlinks_without_listing_them(run, capsys, scan_tree):
    """A snapshot is a second copy; links point at data counted elsewhere."""
    _, client = scan(run, capsys)
    assert "/lab/.snapshot" not in client.ls_calls
    assert "/lab/linked-dir" not in client.ls_calls
    assert client.ls_calls.count("/lab/sub") == 1


def test_fraction_offline_is_by_bytes(run, capsys, scan_tree):
    """Matches archivescan: Data Den bytes over all bytes."""
    result, _ = scan(run, capsys)
    total = result["dataden_bytes"] + result["cache_bytes"]
    assert result["fraction_offline"] == round(100 * result["dataden_bytes"] / total, 5)


@pytest.mark.parametrize(
    "size, expected",
    [("1G", 1024**3), ("10M", 10 * MIB), ("100MB", 100 * MIB), ("500", 500)],
)
def test_size_flag_takes_humanfriendly_binary_units(
    run, capsys, scan_tree, size, expected
):
    """--size uses binary units, so 100MB is still archivescan's 100 MiB."""
    result, _ = scan(run, capsys, "--size", size)
    assert result["threshold_bytes"] == expected


def test_env_var_sets_the_default_and_the_flag_wins(
    run, capsys, scan_tree, monkeypatch
):
    """GLOBUS_FS_ARCHIVESCAN_SIZE is a default, not a override."""
    monkeypatch.setenv(fs.ARCHIVESCAN_SIZE_VAR, "1G")
    result, _ = scan(run, capsys)
    assert result["threshold_bytes"] == 1024**3
    assert result["dataden_files"] == 1
    result, _ = scan(run, capsys, "--size", "10M")
    assert result["threshold_bytes"] == 10 * MIB


def test_bad_size_is_a_clear_error(run, scan_tree):
    """A typo in the size, or in the environment, says what was wrong."""
    with pytest.raises(SystemExit) as exc:
        run(["archivescan", f"{COLLECTION}:/lab", "--size", "lots"])
    assert "bad size 'lots'" in str(exc.value)


def test_filter_matches_the_full_path(run, capsys, scan_tree):
    """--filter is a glob over the whole path, as in archivescan."""
    result, _ = scan(run, capsys, "--filter", "*/sub/*")
    assert result["dataden_files"] == 0
    assert result["cache_files"] == 1


def test_report_keeps_archivescans_layout(run, capsys, scan_tree):
    """Users comparing with archivescan see the same labels."""
    run(["archivescan", f"{COLLECTION}:/lab"])
    out = capsys.readouterr().out
    for label in (
        "----- Results ------",
        "Data Den Candidates:",
        "Cache (Locker) Candidates:",
        "Fraction Offline:",
        "Size: 4.75 GiB",  # 5 GB (decimal) + 100 MiB + 1
    ):
        assert label in out


def test_tsv_is_one_row_for_looping_over_directories(run, capsys, scan_tree):
    """Header plus one row, so scans of many directories stack into a table."""
    run(["archivescan", f"{COLLECTION}:/lab", "--format", "tsv"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].split("\t")[:3] == ["PATH", "THRESHOLD_BYTES", "DATADEN_FILES"]
    assert lines[1].split("\t")[0] == "/lab"


def test_empty_tree_does_not_divide_by_zero(run, capsys, monkeypatch):
    """No files is a result, not a crash."""
    monkeypatch.setitem(TREE, "/void", [])
    run(["archivescan", f"{COLLECTION}:/void"])
    assert "Fraction Offline: n/a (no files)" in capsys.readouterr().out


def test_du_no_longer_follows_directory_symlinks(run, capsys, scan_tree):
    """The walker fix applies to du and find too: links are not descended."""
    client = run(["du", f"{COLLECTION}:/lab", "--format", "json"])
    assert "/lab/linked-dir" not in client.ls_calls
    rows = {r["path"]: r for r in json.loads(capsys.readouterr().out)}
    assert "/lab/linked-dir" not in rows
