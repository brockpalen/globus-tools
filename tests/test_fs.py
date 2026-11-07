"""ls, du and find over a collection.

Every directory is one API call, so the tests also assert on how many
listings a command makes -- that is the cost model users feel.
"""

import datetime as dt

import pytest
from conftest import GB, MB, FakeTransferClient

from globus_tools import fs, walk

COLL = "cccc1111-2222-3333-4444-555566667777"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100", 100),
        ("1K", 1000),
        ("100M", 100 * MB),
        ("2G", 2 * GB),
        ("1T", 1000**4),
        ("500Ki", 500 * 1024),
        ("2Mi", 2 * 1024**2),
        ("+1G", GB),
    ],
)
def test_parse_size(text, expected):
    """--min-size takes the suffixes people actually type."""
    assert walk.parse_size(text) == expected


def test_parse_size_rejects_junk():
    """A bad size is an error, not a silent zero."""
    with pytest.raises(ValueError):
        walk.parse_size("many")


@pytest.mark.parametrize("text,days", [("7", 7), ("7d", 7), ("1w", 7), ("24h", 1)])
def test_parse_age(text, days):
    """--older takes days, hours or weeks."""
    parsed = walk.parse_age(text)
    expected = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    assert abs((parsed - expected).total_seconds()) < 5


def test_parse_age_accepts_an_iso_date():
    """An absolute date works too."""
    assert walk.parse_age("2026-01-01").year == 2026


#: Everything under /data in the fake tree, in bytes.
DATA_BYTES = 1024 + 100 * MB + 2 * GB + 500 * MB + 1500 * MB + 10 + 100 * GB


def du_sizes(capsys):
    """Map each printed path to its size column.

    :param capsys: pytest's capsys fixture
    :returns: ``{path: size}``
    """
    lines = capsys.readouterr().out.strip().splitlines()
    return {line.split()[-1]: line.split()[0] for line in lines}


def test_du_default_is_1024_byte_units_like_du(run, capsys):
    """No -h: whole KiB, rounded up, as du prints by default."""
    run(["du", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys)["/data"] == str(-(-DATA_BYTES // 1024))


def test_du_h_is_human_readable_in_powers_of_1024(run, capsys):
    """-h means what it means for du, not help."""
    run(["du", "-h", f"{COLL}:/data", "--no-header"])
    sizes = du_sizes(capsys)
    assert sizes["/data"] == "97G"
    assert sizes["/data/project-a"] == "3.9G"
    assert sizes["/data/project-a/sub1"] == "1.9G"


def test_du_si_is_powers_of_1000(run, capsys):
    """--si, as for du."""
    run(["du", "--si", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys)["/data"] == "105G"


def test_du_sh_prints_only_the_total(run, capsys):
    """du -sh: one line for the path itself."""
    run(["du", "-sh", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys) == {"/data": "97G"}


def test_du_max_depth_with_a_total(run, capsys):
    """du -h --max-depth 1 -c: one level, then a grand total."""
    run(["du", "-h", "--max-depth", "1", "-c", f"{COLL}:/data", "--no-header"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1].split()[0] == "97G" and lines[-1].split()[-1] == "total"
    paths = [line.split()[-1] for line in lines]
    assert "/data/project-a" in paths and "/data/project-a/sub1" not in paths


def test_du_max_depth_still_counts_everything(run, capsys):
    """Like du: --max-depth limits printing, never the totals."""
    run(["du", "-b", "--max-depth", "0", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys)["/data"] == str(DATA_BYTES)


def test_du_block_size(run, capsys):
    """-B 1M, as for du."""
    run(["du", "-B", "1M", "-s", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys)["/data"] == str(-(-DATA_BYTES // 1024**2))


def test_du_apparent_size_is_accepted(run, capsys):
    """Globus only reports apparent sizes, so the flag changes nothing."""
    run(["du", "--apparent-size", "-b", "-s", f"{COLL}:/data", "--no-header"])
    assert du_sizes(capsys)["/data"] == str(DATA_BYTES)


def test_du_walk_limit_warns_that_totals_are_partial(run, capsys):
    """--maxdepth stops the walk; say so, since --max-depth does not."""
    run(["du", "--maxdepth", "0", "-s", f"{COLL}:/data", "--no-header"])
    assert "totals leave out anything deeper" in capsys.readouterr().err


def test_du_help_is_long_only(run, capsys):
    """-h is taken by human-readable, so help is --help."""
    with pytest.raises(SystemExit):
        run(["du", "--help"])
    assert "--human-readable" in capsys.readouterr().out


def test_du_unit_options_are_exclusive(run, capsys):
    """-h and -b together is an error, not a guess."""
    with pytest.raises(SystemExit):
        run(["du", "-h", "-b", f"{COLL}:/data"])


def test_du_rolls_totals_up_into_ancestors(run, capsys):
    """Each directory reports its own contents plus everything beneath it."""
    run(["du", f"{COLL}:/data", "-b", "--no-header"])
    rows = {
        line.split()[-1]: line.split()
        for line in capsys.readouterr().out.strip().splitlines()
    }
    assert rows["/data"][0] == str(DATA_BYTES)
    assert rows["/data"][1] == "7"  # every file in the tree
    assert rows["/data"][2] == "4"  # every directory below it
    assert rows["/data/project-a"][0] == str(4100 * MB + 10)
    assert rows["/data/project-a/sub1"][1] == "3"


def test_du_depth_limits_what_is_printed(run, capsys):
    """-d 1 prints one level, like du -d 1."""
    run(["du", f"{COLL}:/data", "-d", "1", "--no-header"])
    paths = [line.split()[-1] for line in capsys.readouterr().out.strip().splitlines()]
    assert "/data/project-a" in paths
    assert "/data/project-a/sub1" not in paths


def test_du_visits_every_directory_once(run):
    """One API call per directory, and no directory twice."""
    transfer = run(["du", f"{COLL}:/data", "--no-header"])
    assert len(transfer.ls_calls) == len(set(transfer.ls_calls)) == 5


def test_du_continues_past_an_unlistable_directory(run, capsys):
    """A 40 minute walk must not die on its last directory."""
    transfer = FakeTransferClient(unlistable=["/data/project-b/empty"])
    run(["du", f"{COLL}:/data", "-d", "1", "--no-header"], transfer)
    captured = capsys.readouterr()
    assert "/data" in captured.out
    assert "could not be listed" in captured.err


def test_find_matches_a_name_glob(run, capsys):
    """--name is the common case."""
    run(["find", f"{COLL}:/data", "--name", "*.fastq.gz"])
    out = capsys.readouterr().out
    assert "/data/project-a/sub1/x.fastq.gz" in out
    assert "/data/readme.txt" not in out


def test_find_filters_by_size(run, capsys):
    """--min-size is the filter globus-cli cannot express."""
    run(["find", f"{COLL}:/data", "--type", "f", "--min-size", "1G"])
    found = set(capsys.readouterr().out.split())
    assert found == {
        "/data/project-a/file2.bin",
        "/data/project-b/big.tar",
        "/data/project-a/sub1/y.fastq.gz",
    }


def test_find_type_d_lists_only_directories(run, capsys):
    """--type d is how you enumerate structure."""
    run(["find", f"{COLL}:/data", "--type", "d"])
    found = capsys.readouterr().out.split()
    assert all(not name.endswith(".bin") for name in found)
    assert "/data/project-a" in found


def test_find_maxdepth_one_makes_a_single_call(run):
    """find counts entries, the walker counts directories: off by one.

    This was a real bug -- --maxdepth 1 descended two levels.
    """
    transfer = run(["find", f"{COLL}:/data", "--maxdepth", "1"], None)
    assert len(transfer.ls_calls) == 1


def test_find_maxdepth_one_lists_only_direct_children(run, capsys):
    """Nothing below the starting directory appears."""
    run(["find", f"{COLL}:/data", "--maxdepth", "1"])
    found = capsys.readouterr().out.split()
    assert "/data/readme.txt" in found
    assert "/data/project-a/file1.bin" not in found


def test_find_print0_separates_with_nul(run, capsys):
    """-0 feeds xargs -0 safely, whatever is in the names."""
    run(["find", f"{COLL}:/data", "--name", "*.bin", "-0"])
    out = capsys.readouterr().out
    assert "\0" in out
    assert "\n" not in out


def test_ls_is_one_call(run):
    """A plain listing costs exactly one API call."""
    transfer = run(["ls", f"{COLL}:/data"], None)
    assert transfer.ls_calls == ["/data"]


def test_ls_long_renders_a_mode_string(run, capsys):
    """-l builds drwxr-xr-x from the API's octal permissions."""
    run(["ls", f"{COLL}:/data", "-l"])
    out = capsys.readouterr().out
    assert "drwxr-xr-x" in out
    assert "-rw-r--r--" in out


def test_ls_classify_marks_directories(run, capsys):
    """-F appends a slash, as ls does."""
    run(["ls", f"{COLL}:/data", "-F"])
    assert "project-a/" in capsys.readouterr().out


def test_ls_sorts_directories_first_by_default(run, capsys):
    """Matching ls, directories lead."""
    run(["ls", f"{COLL}:/data"])
    names = capsys.readouterr().out.split()
    assert names == ["project-a", "project-b", "readme.txt"]


def test_ls_sort_by_size(run, capsys):
    """-S puts the biggest first, directories included."""
    run(["ls", f"{COLL}:/data/project-a", "-S"])
    assert capsys.readouterr().out.split()[0] == "file2.bin"


def test_ls_recursive_walks_the_tree(run):
    """-R costs one call per directory, like find."""
    transfer = run(["ls", f"{COLL}:/data", "-R"], None)
    assert len(transfer.ls_calls) == 5


def test_mode_string_for_a_symlink():
    """An unreadable permission set degrades rather than raising."""
    assert fs._mode_string({"type": "file", "permissions": None}).startswith("-?")
