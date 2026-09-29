"""The task table: fields, sorting, filtering and truncation."""

import pytest
from conftest import make_task

from globus_console_cli import tasks as tasklib


def test_resolve_fields_maps_names():
    """--fields takes the short names listed in the help epilog."""
    fields = tasklib.resolve_fields("id,status,rate")
    assert [f.header for f in fields] == ["TASK_ID", "STATUS", "RATE"]


def test_resolve_fields_rejects_unknown(capsys):
    """An unknown field names the valid ones rather than failing opaquely."""
    with pytest.raises(SystemExit):
        tasklib.resolve_fields("id,nonsense")


def test_sort_by_rate_descending():
    """The default ordering puts the heaviest transfers first."""
    tasks = [make_task(1), make_task(3), make_task(2)]
    tasklib.sort_tasks(tasks, "-rate", {})
    rates = [t["effective_bytes_per_second"] for t in tasks]
    assert rates == sorted(rates, reverse=True)


def test_sort_by_age_ascending_is_newest_first():
    """Small age means recently submitted."""
    tasks = [make_task(3), make_task(1), make_task(2)]
    tasklib.sort_tasks(tasks, "age", {})
    assert [t["files"] for t in tasks] == [1200, 2400, 3600]


def test_sort_rejects_unknown_field():
    """A typo in --sort fails loudly."""
    with pytest.raises(SystemExit):
        tasklib.sort_tasks([make_task(1)], "nonsense", {})


class Args:
    """A stand-in for parsed arguments in the filter tests."""

    def __init__(self, **kwargs):
        """Set each keyword as an attribute.

        :param kwargs: attribute values to expose
        """
        self.__dict__.update(kwargs)


def test_errors_filter_keeps_only_unhappy_tasks():
    """--errors is the filter an operator reaches for first."""
    tasks = [
        make_task(1),
        make_task(2, faults=4),
        make_task(3, nice_status="PERMISSION_DENIED"),
        make_task(4, is_paused=True),
    ]
    kept = tasklib._post_filter(tasks, Args(errors=True))
    assert len(kept) == 3


def test_max_rate_finds_stalled_transfers():
    """--max-rate 0 is how you find transfers that have stopped moving."""
    tasks = [make_task(1), make_task(2, effective_bytes_per_second=0)]
    kept = tasklib._post_filter(tasks, Args(errors=False, min_rate=None, max_rate=0))
    assert len(kept) == 1
    assert kept[0]["effective_bytes_per_second"] == 0


def test_older_than_filters_by_submission_age():
    """--older-than is in minutes, measured from request_time."""
    tasks = [make_task(1), make_task(4)]
    kept = tasklib._post_filter(
        tasks,
        Args(errors=False, min_rate=None, max_rate=None, older_than=60),
    )
    assert len(kept) == 1


def test_render_table_never_truncates_the_issue_column():
    """Operators grep ISSUE, so it must survive a narrow terminal."""
    tasks = [make_task(1, nice_status="PERMISSION_DENIED")]
    fields = tasklib.resolve_fields("id,issue,src,dst")
    table = tasklib.render_table(tasks, fields, {}, width=40)
    assert "PERMISSION_DENIED" in table


def test_render_table_shaves_flexible_columns_to_fit():
    """Long collection names give way before fixed columns do."""
    tasks = [make_task(1, source_endpoint_display_name="A" * 120)]
    fields = tasklib.resolve_fields("id,src")
    table = tasklib.render_table(tasks, fields, {}, width=50)
    assert "~" in table
    assert max(len(line) for line in table.splitlines()) <= 50


def test_render_table_without_width_keeps_everything():
    """Piped output is not truncated, so grep sees the full value."""
    name = "B" * 120
    tasks = [make_task(1, source_endpoint_display_name=name)]
    table = tasklib.render_table(tasks, tasklib.resolve_fields("src"), {}, width=None)
    assert name in table


def test_files_column_notes_skips():
    """Skipped files are visible without a second column."""
    task = make_task(1, files_transferred=300, files=1200, files_skipped=2)
    assert tasklib.FIELDS["files"].get(task, {}) == "300/1200+2s"


def test_admin_filters_reach_the_api(run_console):
    """Server-side filters must be passed through, not applied locally."""
    from conftest import FakeTransferClient

    transfer = FakeTransferClient(tasks=[make_task(1)])
    run_console(["tasks", "-e", "abc", "--min-faults", "2", "--fields", "id"], transfer)
    sent = transfer.paginated.last_kwargs
    assert sent["filter_endpoint"] == "abc"
    assert sent["filter_min_faults"] == 2


def test_globus_fs_tasks_uses_the_user_api_not_the_admin_one(run):
    """globus-fs must never call endpoint_manager, which needs a role."""
    from conftest import FakeTransferClient

    transfer = FakeTransferClient(tasks=[make_task(1)])
    run(["tasks", "--fields", "id"], transfer)
    assert transfer.paginated.calls == ["task_list"]
    assert transfer.paginated.last_kwargs["filter"]["status"] == [
        "ACTIVE",
        "INACTIVE",
    ]


def test_globus_fs_history_with_endpoint_stays_on_the_user_api(run):
    """-e is a local filter for a user, not a reason to call the admin API."""
    from conftest import FakeTransferClient

    transfer = FakeTransferClient(tasks=[make_task(1)])
    run(["history", "-e", "ssss1111-0000-0000-0000-000000000001"], transfer)
    assert transfer.paginated.calls == ["task_list"]


def test_globus_console_reads_the_admin_view(run_console):
    """The bare admin command is the Activity view, never the user's list."""
    from conftest import FakeTransferClient

    transfer = FakeTransferClient(tasks=[make_task(1)])
    run_console(["--fields", "id"], transfer)
    assert transfer.paginated.calls == ["endpoint_manager_task_list"]


@pytest.mark.parametrize("flag", ["--user", "--local-user", "--min-faults"])
def test_globus_fs_does_not_offer_admin_only_filters(run, flag):
    """Filters only endpoint_manager can apply are absent, not ignored."""
    with pytest.raises(SystemExit):
        run(["tasks", flag, "x"])


@pytest.mark.parametrize("command", ["ls", "find", "du", "bookmark", "collections"])
def test_globus_console_has_no_filesystem_commands(run_console, command):
    """The admin tool is the Activity view only."""
    from globus_console_cli import console

    assert command not in console.build_parser().command_names


def test_globus_fs_has_no_admin_commands():
    """Nothing in the user tool needs an admin role."""
    from globus_console_cli import fs

    assert "endpoints" not in fs.build_parser().command_names
