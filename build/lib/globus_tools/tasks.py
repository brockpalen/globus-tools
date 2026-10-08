"""The transfer task table both tools draw.

globus-console fills it from the admin Activity view (``endpoint_manager``,
needs the activity_monitor role); globus-fs fills it from the user's own
``task_list``, which needs no role. Everything after the fetch -- columns,
sorting, filters, output formats -- is identical, so it lives here once.

Which API a command uses is fixed by the tool that registered it
(``mine`` is set as a parser default), never by a user flag.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

import globus_sdk

from .common import add_common, age_seconds, human_bytes, human_dur

MISSING_FILTER = globus_sdk.MISSING


# nice_status values that are not actually a problem
BENIGN_STATUS = {"", "OK", "Queued", "QUEUED", "ACTIVE", "None", "NONE"}


def issue_of(task) -> str:
    """The short reason a task is unhappy, or '' when all is well."""
    if task.get("is_paused"):
        return "PAUSED"
    ns = task.get("nice_status") or ""
    if ns and ns not in BENIGN_STATUS:
        return ns
    if task.get("status") == "INACTIVE":
        return "INACTIVE"
    if task.get("status") == "FAILED":
        return (
            task.get("fatal_error", {}).get("code", "FAILED")
            if isinstance(task.get("fatal_error"), dict)
            else "FAILED"
        )
    return ""


def is_problem(task) -> bool:
    """Whether a task needs an operator's attention.

    True when the task is paused, carries a bad ``nice_status``, or has
    recorded at least one fault.

    :param task: a task document
    :returns: True if the task is unhealthy
    """
    return bool(issue_of(task)) or (task.get("faults") or 0) > 0


# --------------------------------------------------------------------------
# field definitions
# --------------------------------------------------------------------------
class Field:
    """One table column: how to read it, show it and sort by it."""

    __slots__ = ("name", "header", "align", "flex", "minw", "get", "sort")

    def __init__(self, name, header, get, align="l", flex=False, minw=0, sort=None):
        """Define a column.

        :param name: the short name used in ``--fields``
        :param header: the column heading
        :param get: callable taking ``(task, ctx)`` and returning the cell
        :param align: ``"l"`` or ``"r"``
        :param flex: True if the column may be truncated to fit
        :param minw: the narrowest this column may be shaved to
        :param sort: callable for sorting, defaulting to ``get``
        """
        self.name = name
        self.header = header
        self.get = get
        self.align = align
        self.flex = flex
        self.minw = minw
        self.sort = sort or (lambda t, c: get(t, c))


def _files(t, c):
    """Render transferred/total file counts, noting skips.

    :param t: a task document
    :param c: render context, unused
    :returns: e.g. ``"300/1200"``, or ``"300/1200+2s"`` when files were skipped
    """
    done = t.get("files_transferred")
    total = t.get("files")
    if done is None and total is None:
        return "-"
    skipped = t.get("files_skipped") or 0
    s = f"{done or 0}/{total or 0}"
    if skipped:
        s += f"+{skipped}s"
    return s


def _pct(t, c):
    """Percent complete, by file count.

    The API does not report total bytes for a running task, so this counts
    files rather than data.

    :param t: a task document
    :param c: render context, unused
    :returns: e.g. ``"25%"``, or ``"-"`` when the total is unknown
    """
    total = t.get("files") or 0
    done = (t.get("files_transferred") or 0) + (t.get("files_skipped") or 0)
    if not total:
        return "-"
    return f"{min(100.0, 100.0 * done / total):.0f}%"


def _cur_rate(t, c):
    """Instantaneous rate, computed from the previous poll (watch mode)."""
    prev = c.get("prev", {}).get(t["task_id"])
    now_bytes = t.get("bytes_transferred") or 0
    now_t = c.get("now", time.time())
    if not prev:
        return "-"
    dt = now_t - prev[1]
    if dt <= 0:
        return "-"
    return human_bytes(max(0, now_bytes - prev[0]) / dt, "/s")


FIELDS = {
    "id": Field("id", "TASK_ID", lambda t, c: (t.get("task_id") or "")[:8]),
    "fullid": Field("fullid", "TASK_ID", lambda t, c: t.get("task_id") or "-"),
    "status": Field("status", "STATUS", lambda t, c: t.get("status") or "-"),
    # issue is the signal admins grep for, so it is never truncated
    "issue": Field("issue", "ISSUE", lambda t, c: issue_of(t) or "-"),
    "reason": Field(
        "reason",
        "REASON",
        lambda t, c: t.get("nice_status_short_description")
        or (t.get("nice_status_details") or "-"),
        flex=True,
        minw=10,
    ),
    "owner": Field(
        "owner",
        "OWNER",
        lambda t, c: t.get("owner_string")
        or t.get("username")
        or (t.get("owner_id") or "-")[:8],
        flex=True,
        minw=10,
    ),
    "type": Field("type", "TYPE", lambda t, c: t.get("type") or "-"),
    "rate": Field(
        "rate",
        "RATE",
        lambda t, c: human_bytes(t.get("effective_bytes_per_second"), "/s"),
        align="r",
        sort=lambda t, c: t.get("effective_bytes_per_second") or 0,
    ),
    "cur": Field("cur", "CUR", _cur_rate, align="r"),
    "bytes": Field(
        "bytes",
        "BYTES",
        lambda t, c: human_bytes(t.get("bytes_transferred")),
        align="r",
        sort=lambda t, c: t.get("bytes_transferred") or 0,
    ),
    "files": Field(
        "files",
        "FILES",
        _files,
        align="r",
        sort=lambda t, c: t.get("files_transferred") or 0,
    ),
    "pct": Field("pct", "PCT", _pct, align="r"),
    "faults": Field(
        "faults",
        "FLT",
        lambda t, c: str(t.get("faults") or 0),
        align="r",
        sort=lambda t, c: t.get("faults") or 0,
    ),
    "age": Field(
        "age",
        "AGE",
        lambda t, c: human_dur(age_seconds(t.get("request_time"))),
        align="r",
        sort=lambda t, c: age_seconds(t.get("request_time")) or 0,
    ),
    "start": Field(
        "start",
        "START",
        lambda t, c: (t.get("request_time") or "-")[:19].replace("T", " "),
    ),
    "done": Field(
        "done",
        "COMPLETED",
        lambda t, c: (t.get("completion_time") or "-")[:19].replace("T", " "),
    ),
    "deadline": Field(
        "deadline",
        "DEADLINE",
        lambda t, c: (
            human_dur(-(age_seconds(t.get("deadline")) or 0))
            if t.get("deadline")
            else "-"
        ),
        align="r",
    ),
    "src": Field(
        "src",
        "SOURCE",
        lambda t, c: t.get("source_endpoint_display_name")
        or t.get("source_endpoint")
        or "-",
        flex=True,
        minw=10,
    ),
    "dst": Field(
        "dst",
        "DEST",
        lambda t, c: t.get("destination_endpoint_display_name")
        or t.get("destination_endpoint")
        or "-",
        flex=True,
        minw=10,
    ),
    "srcid": Field(
        "srcid", "SOURCE_ID", lambda t, c: t.get("source_endpoint_id") or "-"
    ),
    "dstid": Field(
        "dstid", "DEST_ID", lambda t, c: t.get("destination_endpoint_id") or "-"
    ),
    "srcuser": Field(
        "srcuser",
        "SRC_USER",
        lambda t, c: t.get("source_local_user") or "-",
        flex=True,
        minw=6,
    ),
    "dstuser": Field(
        "dstuser",
        "DST_USER",
        lambda t, c: t.get("destination_local_user") or "-",
        flex=True,
        minw=6,
    ),
    "label": Field(
        "label", "LABEL", lambda t, c: t.get("label") or "-", flex=True, minw=6
    ),
    "cmd": Field(
        "cmd", "COMMAND", lambda t, c: t.get("command") or "-", flex=True, minw=10
    ),
    "paused": Field(
        "paused", "PAUSED", lambda t, c: "yes" if t.get("is_paused") else "no"
    ),
    "sync": Field(
        "sync",
        "SYNC",
        lambda t, c: (
            str(t.get("sync_level")) if t.get("sync_level") is not None else "-"
        ),
    ),
    "enc": Field("enc", "ENC", lambda t, c: "yes" if t.get("encrypt_data") else "no"),
}

DEFAULT_FIELDS = "id,status,issue,owner,rate,files,bytes,faults,age,src,dst"
WIDE_FIELDS = (
    "fullid,status,issue,reason,owner,type,rate,bytes,files,pct,faults,"
    "age,start,src,dst,srcuser,dstuser,label"
)
HISTORY_FIELDS = "id,status,issue,owner,rate,files,bytes,faults,start,done,src,dst"


def resolve_fields(spec: str) -> list[Field]:
    """Turn a comma separated column spec into Field objects.

    :param spec: e.g. ``"id,status,rate"``
    :returns: the matching fields, in the order given
    :raises SystemExit: if a name is not a known field
    """
    out = []
    for name in [s.strip() for s in spec.split(",") if s.strip()]:
        if name not in FIELDS:
            sys.exit(
                f"unknown field '{name}'. Known fields:\n  " + ", ".join(sorted(FIELDS))
            )
        out.append(FIELDS[name])
    if not out:
        sys.exit("no fields selected")
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_table(tasks, fields, ctx, header=True, width=None, truncate=True):
    """Render tasks as an aligned text table.

    Flexible columns are shaved, widest first, until the table fits the
    target width; fixed columns such as ISSUE are never truncated.

    :param tasks: task documents to render
    :param fields: columns to show
    :param ctx: render context, carrying poll timing for live rates
    :param header: whether to emit a header row
    :param width: width to fit, or None for no truncation
    :param truncate: False to keep every value in full
    :returns: the rendered table as a single string
    """
    rows = [[str(f.get(t, ctx)) for f in fields] for t in tasks]
    widths = []
    for i, f in enumerate(fields):
        w = max([len(r[i]) for r in rows] + [len(f.header) if header else 0])
        widths.append(max(w, f.minw))

    if truncate and width:
        overflow = sum(widths) + 2 * (len(fields) - 1) - width
        flex = [i for i, f in enumerate(fields) if f.flex]
        # shave the widest flexible column repeatedly until it fits
        while overflow > 0 and flex:
            i = max(flex, key=lambda j: widths[j])
            if widths[i] <= fields[i].minw:
                flex.remove(i)
                continue
            cut = min(overflow, widths[i] - fields[i].minw)
            widths[i] -= cut
            overflow -= cut

    lines = []
    if header:
        lines.append([f.header for f in fields])
    lines.extend(rows)
    out = []
    for line in lines:
        cells = []
        for i, f in enumerate(fields):
            val = line[i]
            if len(val) > widths[i]:
                val = val[: max(1, widths[i] - 1)] + "~"
            cells.append(
                val.rjust(widths[i]) if f.align == "r" else val.ljust(widths[i])
            )
        out.append("  ".join(cells).rstrip())
    return "\n".join(out)


def emit(tasks, args, ctx, fields_spec):
    """Write tasks to stdout in the requested output format.

    :param tasks: task documents to render
    :param args: parsed arguments, read for format and header options
    :param ctx: render context passed through to the fields
    :param fields_spec: comma separated column names
    """
    fmt = args.format
    if fmt == "json":
        json.dump(list(tasks), sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return
    fields = resolve_fields(fields_spec)
    if fmt in ("tsv", "csv"):
        writer = csv.writer(
            sys.stdout, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n"
        )
        if not args.no_header:
            writer.writerow([f.header for f in fields])
        for t in tasks:
            writer.writerow([f.get(t, ctx) for f in fields])
        return
    width = args.width
    if width is None:
        width = shutil.get_terminal_size((0, 0)).columns if sys.stdout.isatty() else 0
    print(
        render_table(
            tasks,
            fields,
            ctx,
            header=not args.no_header,
            width=width or None,
            truncate=not args.no_truncate,
        )
    )


# --------------------------------------------------------------------------
# data fetching
# --------------------------------------------------------------------------
def resolve_identity(args, value: str) -> str:
    """Accept a Globus identity UUID or a username and return the UUID."""
    if len(value) == 36 and value.count("-") == 4:
        return value
    ac = args.tool.auth_client(args)
    r = ac.get_identities(usernames=value)
    ids = r.get("identities") or []
    if not ids:
        sys.exit(f"no Globus identity found for '{value}'")
    return ids[0]["id"]


def fetch_tasks(args, tc, *, statuses, endpoint=None, completion_time=None):
    """Fetch tasks from the admin Activity view.

    :param args: parsed arguments, read for the server-side filters
    :param tc: an authenticated Transfer client
    :param statuses: task statuses to include
    :param endpoint: restrict to this collection, as source or destination
    :param completion_time: ``(start, end)`` bounds for completed tasks
    :returns: the matching task documents
    """
    kwargs = {"filter_status": statuses}
    if endpoint:
        kwargs["filter_endpoint"] = endpoint
        if getattr(args, "endpoint_use", None):
            kwargs["filter_endpoint_use"] = args.endpoint_use
    if getattr(args, "user", None):
        kwargs["filter_owner_id"] = resolve_identity(args, args.user)
    if getattr(args, "local_user", None):
        kwargs["filter_local_user"] = args.local_user
    if getattr(args, "min_faults", None):
        kwargs["filter_min_faults"] = args.min_faults
    if getattr(args, "paused", None) is not None:
        kwargs["filter_is_paused"] = args.paused
    if completion_time:
        kwargs["filter_completion_time"] = completion_time

    limit = getattr(args, "limit", 0) or 0
    tasks = []
    try:
        for page in tc.paginated.endpoint_manager_task_list(**kwargs):
            for task in page:
                tasks.append(dict(task))
                if limit and len(tasks) >= limit:
                    return tasks
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)
    return tasks


def fetch_my_tasks(args, tc, *, statuses, completion_time=None):
    """The user's own tasks, via GET /task_list.

    This is a different API from the admin view and needs no role at all --
    every Globus user can list their own tasks.  It is also not subject to
    the admin view's rule that completed tasks require an endpoint filter.
    """
    filters = {}
    if statuses:
        filters["status"] = list(statuses)
    if completion_time:
        filters["completion_time"] = completion_time

    limit = getattr(args, "limit", 0) or 0
    tasks = []
    try:
        for page in tc.paginated.task_list(filter=filters or MISSING_FILTER):
            for task in page:
                tasks.append(dict(task))
                if limit and len(tasks) >= limit:
                    return tasks
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)

    # --endpoint has no server-side equivalent here, so match locally
    endpoints = {
        e.strip() for e in (getattr(args, "endpoint", "") or "").split(",") if e.strip()
    }
    if endpoints:
        tasks = [
            t
            for t in tasks
            if t.get("source_endpoint_id") in endpoints
            or t.get("destination_endpoint_id") in endpoints
        ]
    return tasks


def sort_tasks(tasks, spec, ctx):
    """Sort tasks in place by a comma separated field spec.

    :param tasks: the task documents to sort
    :param spec: field names, each optionally prefixed ``-`` for descending
    :param ctx: render context passed to the field accessors
    :returns: the same list, sorted
    """
    if not spec:
        return tasks
    keys = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        rev = part.startswith("-")
        name = part.lstrip("-+")
        if name not in FIELDS:
            sys.exit(f"unknown sort field '{name}'")
        keys.append((FIELDS[name], rev))
    for field, rev in reversed(keys):

        def key(t, f=field):
            """Sort key placing numbers before strings.

            :param t: the task document
            :param f: the field to read
            :returns: a tuple that orders numerics ahead of text
            """
            v = f.sort(t, ctx)
            return (0, v, "") if isinstance(v, (int, float)) else (1, 0, str(v))

        tasks.sort(key=key, reverse=rev)
    return tasks


def _statuses(args):
    """Resolve the task statuses to request.

    :param args: parsed arguments, read for ``--status``
    :returns: status names, defaulting to the in-progress pair
    """
    if args.status:
        return [s.strip().upper() for s in args.status.split(",") if s.strip()]
    return ["ACTIVE", "INACTIVE"]


def _post_filter(tasks, args):
    """Apply the filters the API cannot express server-side.

    :param tasks: task documents to filter
    :param args: parsed arguments, read for the client-side filters
    :returns: the tasks that survive
    """
    if getattr(args, "errors", False):
        tasks = [t for t in tasks if is_problem(t)]
    if getattr(args, "min_rate", None) is not None:
        tasks = [
            t
            for t in tasks
            if (t.get("effective_bytes_per_second") or 0) >= args.min_rate
        ]
    if getattr(args, "max_rate", None) is not None:
        tasks = [
            t
            for t in tasks
            if (t.get("effective_bytes_per_second") or 0) <= args.max_rate
        ]
    if getattr(args, "older_than", None) is not None:
        tasks = [
            t
            for t in tasks
            if (age_seconds(t.get("request_time")) or 0) >= args.older_than * 60
        ]
    return tasks


def cmd_tasks(args, ctx=None):
    """List transfer tasks: the admin Activity view, or your own.

    :param args: parsed arguments
    :param ctx: render context, reused across refreshes by watch mode
    :returns: the task documents that were displayed
    """
    tc = args._tc or args.tool.transfer_client(args)
    endpoints = [e.strip() for e in (args.endpoint or "").split(",") if e.strip()]
    tasks = []
    if args.mine:
        tasks = fetch_my_tasks(args, tc, statuses=_statuses(args))
    elif endpoints:
        seen = set()
        for ep in endpoints:
            for t in fetch_tasks(args, tc, statuses=_statuses(args), endpoint=ep):
                if t["task_id"] not in seen:
                    seen.add(t["task_id"])
                    tasks.append(t)
    else:
        tasks = fetch_tasks(args, tc, statuses=_statuses(args))
    tasks = _post_filter(tasks, args)
    ctx = ctx if ctx is not None else {}
    ctx.setdefault("now", time.time())
    sort_tasks(tasks, args.sort, ctx)
    spec = args.fields or _default_fields(args, DEFAULT_FIELDS)
    emit(tasks, args, ctx, spec)
    if args.summary and args.format == "table":
        print_summary(tasks)
    return tasks


def _default_fields(args, base):
    """Pick the column set, dropping owner when every task is the user's own."""
    if args.wide:
        return WIDE_FIELDS.replace("owner,", "") if args.mine else WIDE_FIELDS
    return base.replace("owner,", "") if args.mine else base


def print_summary(tasks):
    """Print an aggregate line beneath a task table.

    :param tasks: the task documents that were displayed
    """
    total_rate = sum(t.get("effective_bytes_per_second") or 0 for t in tasks)
    total_bytes = sum(t.get("bytes_transferred") or 0 for t in tasks)
    bad = sum(1 for t in tasks if is_problem(t))
    paused = sum(1 for t in tasks if t.get("is_paused"))
    print(
        f"\n{len(tasks)} task(s)   {human_bytes(total_rate, '/s')} aggregate   "
        f"{human_bytes(total_bytes)} moved   {bad} with issues   {paused} paused"
    )


def cmd_watch(args):
    """Re-render the task table on an interval.

    Carries the previous poll's byte counts forward so the CUR column can
    show an instantaneous rate, which the API itself never reports.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    args._tc = tc
    ctx = {"prev": {}}
    if not args.fields and not args.wide:
        args.fields = DEFAULT_FIELDS.replace("rate,", "rate,cur,")
    try:
        while True:
            ctx["now"] = time.time()
            if sys.stdout.isatty() and not args.no_clear:
                sys.stdout.write("\033[H\033[2J")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"# {args.tool.name}  {stamp}  (refresh {args.interval}s, ctrl-c to quit)"
            )
            tasks = cmd_tasks(args, ctx=ctx)
            ctx["prev"] = {
                t["task_id"]: (t.get("bytes_transferred") or 0, ctx["now"])
                for t in tasks
            }
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def cmd_history(args):
    """List completed tasks.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    endpoints = [e.strip() for e in (args.endpoint or "").split(",") if e.strip()]
    if args.mine:
        # --endpoint is a local filter over the user's own tasks here, applied
        # inside fetch_my_tasks; the admin per-endpoint fetch must not run
        endpoints = []
    elif endpoints == ["all"]:
        try:
            endpoints = [e["id"] for e in tc.endpoint_manager_monitored_endpoints()]
        except globus_sdk.TransferAPIError as e:
            args.tool.api_error(e)
    if not endpoints and not args.mine:
        sys.exit(
            "history needs --endpoint <uuid[,uuid...]> (or --endpoint all).\n"
            "The admin view only returns completed tasks per endpoint.\n"
            "List what you can monitor with:  globus-console endpoints\n"
            "For your own completed tasks:    globus-fs history"
        )
    start = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    statuses = [
        s.strip().upper() for s in (args.status or "SUCCEEDED,FAILED").split(",")
    ]
    tasks, seen = [], set()
    if args.mine:
        tasks = fetch_my_tasks(args, tc, statuses=statuses, completion_time=f"{start},")
    for ep in endpoints:
        for t in fetch_tasks(
            args, tc, statuses=statuses, endpoint=ep, completion_time=(start, "")
        ):
            if t["task_id"] not in seen:
                seen.add(t["task_id"])
                tasks.append(t)
    tasks = _post_filter(tasks, args)
    ctx = {"now": time.time()}
    sort_tasks(tasks, args.sort or "-done", ctx)
    spec = args.fields or _default_fields(args, HISTORY_FIELDS)
    emit(tasks, args, ctx, spec)
    if args.summary and args.format == "table":
        print_summary(tasks)


def cmd_task(args):
    """Show one task in full, optionally with its event log.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    try:
        task = (
            tc.get_task(args.task_id)
            if args.mine
            else tc.endpoint_manager_get_task(args.task_id)
        )
    except globus_sdk.TransferAPIError as e:
        if e.http_status == 403 and not args.mine:
            # not an activity monitor for this task's endpoints; it may still
            # be the user's own task, which needs no role to read
            try:
                task = tc.get_task(args.task_id)
                args.mine = True
            except globus_sdk.TransferAPIError:
                args.tool.api_error(e)
        else:
            args.tool.api_error(e)
    if args.format == "json":
        json.dump(dict(task), sys.stdout, indent=2, default=str)
        print()
    else:
        data = dict(task)
        width = max(len(k) for k in data)
        for k in sorted(data):
            v = data[k]
            print(
                f"{k.ljust(width)}  {json.dumps(v) if isinstance(v, (dict, list)) else v}"
            )
    if args.events:
        print("\n--- events ---")
        try:
            lister = (
                tc.task_event_list if args.mine else tc.endpoint_manager_task_event_list
            )
            events = list(
                lister(args.task_id, query_params={"limit": args.events_limit})
            )
        except globus_sdk.TransferAPIError as e:
            args.tool.api_error(e)
        if args.format == "json":
            json.dump([dict(e) for e in events], sys.stdout, indent=2, default=str)
            print()
            return
        for ev in events:
            flag = "ERROR" if ev.get("is_error") else "ok   "
            print(
                f"{(ev.get('time') or '')[:19]}  {flag}  {ev.get('code')}  "
                f"{(ev.get('description') or '').strip()}"
            )
            if args.verbose and ev.get("details"):
                print("    " + str(ev["details"]).replace("\n", "\n    ").strip())


def add_list_opts(p, admin):
    """Add the filtering and display options for task listings.

    :param p: the parser to extend
    :param admin: True for the admin view, which adds the filters only
        ``endpoint_manager`` can apply server-side
    """
    p.add_argument(
        "-e",
        "--endpoint",
        help="restrict to endpoint/collection UUID(s), comma separated",
    )
    if admin:
        p.add_argument(
            "--endpoint-use",
            choices=("source", "destination"),
            help="with --endpoint, only count it as source or as destination",
        )
        p.add_argument("-u", "--user", help="owner: Globus username or identity UUID")
        p.add_argument("--local-user", help="filter on the local (POSIX) username used")
        p.add_argument(
            "--min-faults", type=int, help="only tasks with at least N faults"
        )
    p.add_argument(
        "--errors",
        action="store_true",
        help="only tasks that are faulting, paused, or in a bad nice_status",
    )
    p.add_argument(
        "--min-rate",
        type=float,
        metavar="BPS",
        help="only tasks at or above this many bytes/sec",
    )
    p.add_argument(
        "--max-rate",
        type=float,
        metavar="BPS",
        help="only tasks at or below this many bytes/sec (find stalls)",
    )
    p.add_argument(
        "--older-than",
        type=float,
        metavar="MIN",
        help="only tasks submitted at least MIN minutes ago",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop fetching after N tasks, before sorting (0 = all)",
    )
    p.add_argument(
        "--sort",
        default="-rate",
        help="comma separated field names, '-' prefix for descending "
        "(default: -rate)",
    )
    p.add_argument(
        "--fields", help=f"comma separated columns (default: {DEFAULT_FIELDS})"
    )
    p.add_argument("--wide", action="store_true", help="show every useful column")
    p.add_argument("--width", type=int, help="table width to fit (0 = never truncate)")
    p.add_argument(
        "--no-truncate", action="store_true", help="never shorten long values"
    )
    p.add_argument(
        "--summary", action="store_true", help="print an aggregate line after the table"
    )


def glue_negative_values(argv):
    """Allow `--sort -rate`; argparse would otherwise read -rate as a flag."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if (
            a in ("--sort", "--fields")
            and i + 1 < len(argv)
            and argv[i + 1].startswith("-")
            and argv[i + 1].lstrip("-+").split(",")[0] in FIELDS
        ):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def add_task_commands(sub, admin):
    """Register ``tasks``, ``watch``, ``history`` and ``task``.

    :param sub: the object returned by ``add_subparsers``
    :param admin: True to read the admin Activity view (globus-console),
        False to read the user's own tasks (globus-fs)
    """
    whose = "on collections you monitor" if admin else "you submitted"

    def add_status_opts(sp):
        """Add --status, and --paused/--not-paused where the API supports them.

        :param sp: the parser to extend
        """
        sp.add_argument("--status", help="ACTIVE,INACTIVE (default), or any status")
        if admin:
            sp.add_argument(
                "--paused",
                dest="paused",
                action="store_true",
                default=None,
                help="only paused tasks",
            )
            sp.add_argument(
                "--not-paused",
                dest="paused",
                action="store_false",
                help="only tasks that are not paused",
            )

    ls = sub.add_parser(
        "tasks",
        help=f"in-progress transfers {whose}" + (" (default command)" if admin else ""),
    )
    add_status_opts(ls)
    add_list_opts(ls, admin)
    add_common(ls)
    ls.set_defaults(func=cmd_tasks)

    w = sub.add_parser("watch", help="refresh the task table on an interval")
    w.add_argument(
        "-n",
        "--interval",
        type=float,
        default=10.0,
        help="seconds between refreshes (default: 10)",
    )
    w.add_argument("--no-clear", action="store_true", help="append instead of clearing")
    add_status_opts(w)
    add_list_opts(w, admin)
    add_common(w)
    w.set_defaults(func=cmd_watch)

    h = sub.add_parser(
        "history",
        help="completed tasks for an endpoint" if admin else "your completed tasks",
    )
    h.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="look back this many hours (default: 24)",
    )
    h.add_argument("--status", help="SUCCEEDED,FAILED (default)")
    add_list_opts(h, admin)
    add_common(h)
    h.set_defaults(func=cmd_history)

    t = sub.add_parser("task", help="everything about one task")
    t.add_argument("task_id")
    t.add_argument("--events", action="store_true", help="also show the event log")
    t.add_argument("--events-limit", type=int, default=50)
    t.add_argument("-v", "--verbose", action="store_true", help="include event details")
    add_common(t)
    t.set_defaults(func=cmd_task)

    for sp in (ls, w, h, t):
        sp.set_defaults(mine=not admin)
