#!/usr/bin/env python3
"""globus-activity - a greppable, table-oriented view of the Globus web console
"Activity" tab for subscription / endpoint administrators.

Uses the Transfer API endpoint_manager/* endpoints, which are the same calls the
console makes.  Requires the "activity_monitor" (or "administrator") role on the
endpoints you want to see.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import posixpath
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import globus_sdk
    from globus_sdk import ClientApp, GlobusAppConfig, UserApp
    from globus_sdk.scopes import AuthScopes, TransferScopes
except ImportError as exc:  # pragma: no cover
    sys.exit(f"globus-sdk 3.30+ is required ({exc}). Run:  pip install -U globus-sdk")

try:  # module was renamed between globus-sdk 3.x and 4.x
    from globus_sdk.token_storage import JSONTokenStorage
except ImportError:  # pragma: no cover
    from globus_sdk.tokenstorage import JSONTokenStorage

MISSING_FILTER = globus_sdk.MISSING

import globus_collections  # noqa: E402
import globus_fs  # noqa: E402  (must follow the guarded globus_sdk import)
import globus_security  # noqa: E402

APP_NAME = "globus-activity"
CONFIG_DIR = os.path.expanduser(
    os.environ.get("GLOBUS_ACTIVITY_HOME", "~/.globus-activity")
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
TOKEN_FILE = os.path.join(CONFIG_DIR, "tokens.json")

SCOPES = {
    TransferScopes.resource_server: [TransferScopes.all],
    AuthScopes.resource_server: [
        AuthScopes.openid,
        AuthScopes.profile,
        AuthScopes.email,
        AuthScopes.view_identities,
    ],
}

REGISTER_HELP = """\
No Globus client ID configured.

globus-activity talks to Globus as a registered application.  Create one once
(takes about a minute):

  1. Open https://app.globus.org/settings/developers
  2. "Register a thick client or script that will be installed and run by
     users on their devices"
  3. Pick (or create) a project, name it e.g. "campus globus-activity"
  4. Copy the Client UUID it gives you, then run:

       globus-activity login --client-id <UUID>

For unattended use (cron, dashboards) you can instead register a
"service account or application credential", grant that client's identity the
activity_monitor role on your endpoints, and export:

       export GLOBUS_ACTIVITY_CLIENT_ID=<UUID>
       export GLOBUS_ACTIVITY_CLIENT_SECRET=<secret>
"""


# --------------------------------------------------------------------------
# config / auth
# --------------------------------------------------------------------------
def load_config() -> dict:
    """Read the saved configuration.

    :returns: the config mapping, or an empty dict if it is missing or corrupt
    """
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    """Write the config file atomically, owner-readable only.

    :param cfg: the configuration mapping to serialise
    """
    globus_security.secure_directory(CONFIG_DIR)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


def get_client_id(override: str | None = None) -> str | None:
    """Resolve the Globus client UUID to authenticate as.

    Precedence: explicit argument, then ``GLOBUS_ACTIVITY_CLIENT_ID``, then
    the saved config.

    :param override: a client UUID supplied on the command line
    :returns: the client UUID, or None if none is configured
    """
    return (
        override
        or os.environ.get("GLOBUS_ACTIVITY_CLIENT_ID")
        or load_config().get("client_id")
    )


def build_app(
    client_id: str | None = None, fix_permissions: bool = False
) -> globus_sdk.GlobusApp:
    """Build the GlobusApp that owns login and the token store.

    The credential store is checked before it is touched.  A refresh token
    readable by other local users is treated the way ssh treats an exposed
    private key: refuse, explain, and say how to fix it.

    :param client_id: override for the configured Globus client UUID
    :param fix_permissions: True to chmod paths this user owns rather than
        refusing to run
    :returns: a ``ClientApp`` when a client secret is set, else a ``UserApp``
    """
    cid = get_client_id(client_id)
    if not cid:
        sys.exit(REGISTER_HELP)
    try:
        globus_security.secure_directory(CONFIG_DIR, fix=fix_permissions)
        repaired = globus_security.assert_secure(
            CONFIG_DIR, TOKEN_FILE, CONFIG_FILE, fix=fix_permissions
        )
    except globus_security.InsecureCredentialStore as exc:
        sys.exit(exc.banner())
    for path in repaired:
        print(f"tightened permissions on {path}", file=sys.stderr)
    config = GlobusAppConfig(
        token_storage=JSONTokenStorage(TOKEN_FILE),
        request_refresh_tokens=True,
    )
    secret = os.environ.get("GLOBUS_ACTIVITY_CLIENT_SECRET")
    if secret:
        return ClientApp(
            APP_NAME,
            client_id=cid,
            client_secret=secret,
            scope_requirements=SCOPES,
            config=config,
        )
    return UserApp(APP_NAME, client_id=cid, scope_requirements=SCOPES, config=config)


def transfer_client(args) -> globus_sdk.TransferClient:
    """Build a Transfer API client bound to the credential store.

    :param args: parsed arguments, read for ``--client-id``
    :returns: an authenticated ``TransferClient``
    """
    return globus_sdk.TransferClient(app=build_app(getattr(args, "client_id", None)))


def auth_client(args) -> globus_sdk.AuthClient:
    """Build an Auth API client, used to resolve usernames to identities.

    :param args: parsed arguments, read for ``--client-id``
    :returns: an authenticated ``AuthClient``
    """
    return globus_sdk.AuthClient(app=build_app(getattr(args, "client_id", None)))


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def human_bytes(n, suffix="") -> str:
    """Format a byte count in decimal units.

    Decimal rather than binary because transfer rates are quoted that way.

    :param n: the byte count, or None
    :param suffix: appended to the unit, e.g. ``"/s"`` for a rate
    :returns: a short string such as ``"1.5G"``, or ``"-"`` for None
    """
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1000 or unit == "P":
            if unit == "B":
                return f"{int(n)}{unit}{suffix}"
            return f"{n:.1f}{unit}{suffix}"
        n /= 1000.0
    return f"{n:.1f}P{suffix}"


def human_dur(seconds) -> str:
    """Format a duration as the two most significant units.

    :param seconds: the duration, or None
    :returns: e.g. ``"3d4h"``, ``"2h15m"``, ``"5m12s"``, or ``"-"``
    """
    if seconds is None:
        return "-"
    s = int(seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{sign}{d}d{h}h"
    if h:
        return f"{sign}{h}h{m:02d}m"
    if m:
        return f"{sign}{m}m{s:02d}s"
    return f"{sign}{s}s"


def parse_time(val):
    """Parse a Globus timestamp into an aware datetime.

    Accepts the space- and ``T``-separated forms the API returns, with or
    without a zone, assuming UTC when absent.

    :param val: the timestamp string, or None
    :returns: an aware datetime, or None if it is absent or unparseable
    """
    if not val:
        return None
    try:
        v = val.replace(" ", "T")
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def age_seconds(val):
    """Seconds elapsed since a timestamp.

    :param val: the timestamp string, or None
    :returns: the age in seconds, or None if the timestamp is unusable
    """
    dt = parse_time(val)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


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
    ac = auth_client(args)
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
        handle_api_error(e)
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

    for name, flag in (
        ("user", "--user"),
        ("local_user", "--local-user"),
        ("min_faults", "--min-faults"),
    ):
        if getattr(args, name, None):
            print(
                f"{flag} is an admin-only filter and is ignored with --mine",
                file=sys.stderr,
            )
    if getattr(args, "paused", None) is not None:
        print(
            "--paused/--not-paused are admin-only filters and are ignored "
            "with --mine",
            file=sys.stderr,
        )

    limit = getattr(args, "limit", 0) or 0
    tasks = []
    try:
        for page in tc.paginated.task_list(filter=filters or MISSING_FILTER):
            for task in page:
                tasks.append(dict(task))
                if limit and len(tasks) >= limit:
                    return tasks
    except globus_sdk.TransferAPIError as e:
        handle_api_error(e)

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


def handle_api_error(e) -> None:
    """Turn a Globus API error into an actionable message and exit.

    :param e: the API error to explain
    :raises SystemExit: always
    """
    if e.http_status == 403:
        sys.exit(
            f"Permission denied ({e.code}): {e.message}\n\n"
            "The Activity view requires the 'activity_monitor' or "
            "'activity_manager' role on a *subscribed* collection.  "
            "Without one of those roles on at least one collection, "
            "every admin command returns this error.\n\n"
            "  see what you can monitor:  globus-activity endpoints\n"
            "  list your own transfers:   globus-activity --mine"
        )
    if e.http_status == 401:
        sys.exit(
            f"Not authenticated ({e.code}): {e.message}\n" "Run:  globus-activity login"
        )
    sys.exit(f"Transfer API error {e.http_status} ({e.code}): {e.message}")


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


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_login(args):
    """Authenticate to Globus and store refresh tokens.

    :param args: parsed arguments
    """
    cid = get_client_id(args.client_id)
    if not cid:
        sys.exit(REGISTER_HELP)
    if args.client_id:
        cfg = load_config()
        cfg["client_id"] = args.client_id
        save_config(cfg)
    app = build_app(cid, fix_permissions=args.fix_permissions)
    app.login(force=args.force)
    # the SDK writes new token files under a user-only umask, but an existing
    # one keeps whatever mode it already had
    globus_security.harden(TOKEN_FILE)
    tc = globus_sdk.TransferClient(app=app)
    try:
        who = tc.get("/v0.10/endpoint_manager")
        print(
            f"Logged in. Activity monitor identity: "
            f"{who.get('identity_id') or who.get('username') or 'ok'}"
        )
    except globus_sdk.TransferAPIError as e:
        print(
            f"Logged in, but the endpoint_manager check returned "
            f"{e.http_status} ({e.code}).",
            file=sys.stderr,
        )
    print(f"Tokens stored in {TOKEN_FILE}")


def cmd_logout(args):
    """Delete the stored tokens.

    :param args: parsed arguments, unused
    """
    for path in (TOKEN_FILE,):
        try:
            os.remove(path)
            print(f"removed {path}")
        except FileNotFoundError:
            print(f"no tokens at {path}")


def cmd_whoami(args):
    """Print the identity the stored tokens belong to.

    :param args: parsed arguments
    """
    ac = auth_client(args)
    try:
        info = ac.userinfo()
    except globus_sdk.AuthAPIError as e:
        sys.exit(f"Auth error {e.http_status} ({e.code}): {e.message}")
    print(
        json.dumps(info.data, indent=2)
        if args.format == "json"
        else f"{info.get('preferred_username')}  ({info.get('sub')})"
    )


def cmd_endpoints(args):
    """List the collections this identity may monitor.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    try:
        eps = list(tc.endpoint_manager_monitored_endpoints())
    except globus_sdk.TransferAPIError as e:
        handle_api_error(e)
    if args.format == "json":
        json.dump([dict(e) for e in eps], sys.stdout, indent=2, default=str)
        print()
        return
    rows = [
        (
            e.get("id", "-"),
            e.get("display_name") or e.get("canonical_name") or "-",
            e.get("owner_string") or "-",
            "yes" if e.get("is_paused") else "no",
        )
        for e in eps
    ]
    headers = ("ENDPOINT_ID", "DISPLAY_NAME", "OWNER", "PAUSED")
    widths = [max([len(r[i]) for r in rows] + [len(headers[i])]) for i in range(4)]
    if not args.no_header:
        print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for r in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip())


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
    tc = args._tc or transfer_client(args)
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
    tc = transfer_client(args)
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
                f"# globus-activity  {stamp}  (refresh {args.interval}s, ctrl-c to quit)"
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
    tc = transfer_client(args)
    endpoints = [e.strip() for e in (args.endpoint or "").split(",") if e.strip()]
    if endpoints == ["all"]:
        try:
            endpoints = [e["id"] for e in tc.endpoint_manager_monitored_endpoints()]
        except globus_sdk.TransferAPIError as e:
            handle_api_error(e)
    if not endpoints and not args.mine:
        sys.exit(
            "history needs --endpoint <uuid[,uuid...]> (or --endpoint all).\n"
            "The admin view only returns completed tasks per endpoint.\n"
            "List what you can monitor with:  globus-activity endpoints\n"
            "For your own completed tasks:    globus-activity history --mine"
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
    tc = transfer_client(args)
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
                handle_api_error(e)
        else:
            handle_api_error(e)
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
            handle_api_error(e)
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


# --------------------------------------------------------------------------
# filesystem helpers (collections with no shell access)
# --------------------------------------------------------------------------
_BOOKMARK_CACHE = None


def _is_uuid(value):
    """Cheap check for something shaped like a UUID.

    :param value: the string to test
    :returns: True if it looks like a hyphenated UUID
    """
    return len(value) == 36 and value.count("-") == 4


def bookmark_index(tc, refresh=False):
    """All of the user's bookmarks, fetched once per run.

    These are the same server-side bookmarks as `globus bookmark` and the web
    app, so a name saved in any of them works in all of them.
    """
    global _BOOKMARK_CACHE
    if _BOOKMARK_CACHE is None or refresh:
        try:
            _BOOKMARK_CACHE = [dict(b) for b in tc.bookmark_list()]
        except globus_sdk.TransferAPIError as e:
            handle_api_error(e)
    return _BOOKMARK_CACHE


def resolve_bookmark(tc, name_or_id):
    """Look a bookmark up by name first, then by id, as globus-cli does."""
    for bm in bookmark_index(tc):
        if bm.get("name") == name_or_id:
            return bm
    for bm in bookmark_index(tc):
        if bm.get("id") == name_or_id:
            return bm
    return None


SHORTCUT_FILE = os.path.join(CONFIG_DIR, "collections.conf")


def site_shortcuts():
    """Load the merged site and user collection shortcuts.

    :returns: mapping of name to :class:`globus_collections.Shortcut`
    """
    return globus_collections.load(SHORTCUT_FILE)


def _join_within(base, tail):
    """Resolve a command line path beneath a named location's base path.

    A bookmark or shortcut is a starting directory, so ``projects:alpha``
    means ``alpha`` underneath it. A base of ``/`` makes that identical to
    an absolute path, which is why a bare collection shortcut behaves the
    way people expect.

    :param base: the base path of the bookmark or shortcut
    :param tail: the path typed after the colon, possibly empty
    :returns: the resolved absolute path
    """
    if not tail:
        return base or "/"
    return posixpath.join(base or "/", tail.lstrip("/"))


def _resolve_target(args, tc):
    """'UUID:/path' or 'bookmark[:/sub]' -> (collection_id, path).

    A path given after a bookmark name is taken as relative to the bookmark,
    so `scratch:sub/dir` is `sub/dir` underneath whatever `scratch` points at.
    """
    spec = args.target
    head, sep, tail = spec.partition(":")
    head = head.strip()

    if _is_uuid(head):
        path = tail or "/"
        return head, path if path.startswith("/") else "/" + path

    # A personal bookmark wins over a site shortcut, the same way user
    # config wins over /etc everywhere else.
    bm = resolve_bookmark(tc, head)
    if bm is not None:
        return bm["endpoint_id"], _join_within(bm.get("path"), tail)

    shortcut = site_shortcuts().get(head)
    if shortcut is not None:
        return shortcut.collection, _join_within(shortcut.path, tail)

    sys.exit(
        f"'{head}' is not a collection UUID, a bookmark or a known "
        f"collection name.\n" + _known_names_hint(tc)
    )


def _known_names_hint(tc):
    """Build the "here is what would have worked" text for a bad target.

    Names both sources explicitly, and says when one of them is empty --
    silently omitting it leaves the reader guessing whether the name was
    wrong or the feature is simply unconfigured here.

    :param tc: an authenticated Transfer client
    :returns: a message listing the available names
    """
    lines = []

    shortcuts = sorted(site_shortcuts())
    if shortcuts:
        lines.append("Collection shortcuts: " + ", ".join(shortcuts))
    else:
        lines.append(
            "Collection shortcuts: none configured on this system "
            "(globus-activity collections)"
        )

    names = sorted(n for n in (b.get("name") for b in bookmark_index(tc)) if n)
    if names:
        lines.append("Your bookmarks:       " + ", ".join(names))
    else:
        lines.append(
            "Your bookmarks:       none yet "
            "(globus-activity bookmark create <uuid>:/ <name>)"
        )

    lines.append("")
    lines.append("A collection UUID always works:  globus-activity du <uuid>:/path")
    return "\n".join(lines)


def _walk_or_explain(args, tc, collection, path, maxdepth=None):
    """Walk, turning a ConsentRequired on the first listing into advice."""
    walker = globus_fs.Walker(
        tc,
        collection,
        jobs=args.jobs,
        show_hidden=not args.no_hidden,
        local_user=getattr(args, "local_user", None),
        progress=sys.stderr.isatty() and args.format == "table",
    )
    produced = False
    for item in walker.walk(path, maxdepth=maxdepth):
        produced = True
        yield item
    walker.clear_progress()
    if not produced and walker.errors:
        first = walker.errors[0]
        if first.code in ("ConsentRequired", "PermissionDenied"):
            sys.stderr.write(str(first) + "\n")
            if first.code == "ConsentRequired":
                sys.stderr.write(globus_fs.consent_hint(collection, []))
            sys.exit(1)
    args._walker = walker


def _report_walk_errors(args):
    """Print the directories a walk could not list.

    :param args: parsed arguments, carrying the walker that ran
    """
    walker = getattr(args, "_walker", None)
    if not walker or not walker.errors:
        return
    shown = walker.errors[: args.max_errors]
    print(
        f"\n{len(walker.errors)} director(y/ies) could not be listed:", file=sys.stderr
    )
    for err in shown:
        print(f"  {err}", file=sys.stderr)
    if len(walker.errors) > len(shown):
        print(f"  ... and {len(walker.errors) - len(shown)} more", file=sys.stderr)


def _entry_matches(args, entry, name, is_dir, min_size, max_size, newer, older):
    """Test one directory entry against the find filters.

    :param args: parsed arguments, read for the filter options
    :param entry: the listing entry to test
    :param name: the entry's base name
    :param is_dir: whether the entry is a directory
    :param min_size: smallest size to accept, or None
    :param max_size: largest size to accept, or None
    :param newer: only entries modified after this, or None
    :param older: only entries modified before this, or None
    :returns: True if the entry passes every active filter
    """
    if args.type == "f" and is_dir:
        return False
    if args.type == "d" and not is_dir:
        return False
    if args.name and not fnmatch.fnmatch(name, args.name):
        return False
    if args.iname and not fnmatch.fnmatch(name.lower(), args.iname.lower()):
        return False
    size = entry.get("size") or 0
    if min_size is not None and size < min_size:
        return False
    if max_size is not None and size > max_size:
        return False
    if newer or older:
        mtime = parse_time(entry.get("last_modified"))
        if mtime is None:
            return False
        if newer and mtime < newer:
            return False
        if older and mtime > older:
            return False
    return True


def cmd_find(args):
    """Search a collection recursively and print matching paths.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    collection, root = _resolve_target(args, tc)
    min_size = globus_fs.parse_size(args.min_size) if args.min_size else None
    max_size = globus_fs.parse_size(args.max_size) if args.max_size else None
    newer = globus_fs.parse_age(args.newer) if args.newer else None
    older = globus_fs.parse_age(args.older) if args.older else None

    end = "\0" if args.print0 else "\n"
    count = 0
    total = 0
    # find's --maxdepth counts *entries* below the start, but the walker
    # counts directories it lists, and a directory's entries are one level
    # deeper than the directory.  --maxdepth 1 therefore means "list only the
    # starting directory", which also saves the API calls one level down.
    walk_depth = None if args.maxdepth is None else max(0, args.maxdepth - 1)
    for path, _depth, entries in _walk_or_explain(
        args, tc, collection, root, maxdepth=walk_depth
    ):
        for e in entries:
            name = e.get("name", "")
            is_dir = e.get("type") == "dir"
            if not _entry_matches(
                args, e, name, is_dir, min_size, max_size, newer, older
            ):
                continue
            full = posixpath.join(path, name)
            count += 1
            total += e.get("size") or 0
            if args.long:
                mtime = (e.get("last_modified") or "")[:19]
                kind = "d" if is_dir else "-"
                sys.stdout.write(
                    f"{kind} {human_bytes(e.get('size') or 0):>9}  {mtime:19}  "
                    f"{full}{end}"
                )
            else:
                sys.stdout.write(full + end)
            if args.limit and count >= args.limit:
                sys.stdout.flush()
                _report_walk_errors(args)
                return
    sys.stdout.flush()
    if args.summary:
        print(f"\n{count} match(es), {human_bytes(total)}", file=sys.stderr)
    _report_walk_errors(args)


MODE_BITS = ("---", "--x", "-w-", "-wx", "r--", "r-x", "rw-", "rwx")


def _mode_string(entry):
    """0755 -> drwxr-xr-x, as ls -l shows it."""
    kind = {"dir": "d", "file": "-", "invalid_symlink": "l", "symlink": "l"}.get(
        entry.get("type"), "?"
    )
    perms = str(entry.get("permissions") or "")[-3:]
    if len(perms) != 3 or not perms.isdigit():
        return kind + "?????????"
    return kind + "".join(MODE_BITS[int(c)] for c in perms)


def _ls_sort(entries, args):
    """Order listing entries for display.

    Directories sort first by default, matching ``ls``.

    :param entries: the listing entries to sort
    :param args: parsed arguments, read for the sort flags
    :returns: the same list, sorted
    """
    if args.sort_size:
        entries.sort(key=lambda e: e.get("size") or 0, reverse=True)
    elif args.sort_time:
        entries.sort(key=lambda e: e.get("last_modified") or "", reverse=True)
    else:
        entries.sort(key=lambda e: (e.get("type") != "dir", e.get("name", "")))
    if args.reverse:
        entries.reverse()
    return entries


def _ls_name(entry, args):
    """Render one entry's display name.

    :param entry: the listing entry
    :param args: parsed arguments, read for ``--classify``
    :returns: the name, with a trailing slash on directories under ``-F``
    """
    name = entry.get("name", "")
    if args.classify and entry.get("type") == "dir":
        name += "/"
    return name


def _print_columns(names, width):
    """Multi-column, filling down then across, the way ls does."""
    if not names:
        return
    colw = max(len(n) for n in names) + 2
    cols = max(1, width // colw)
    rows = (len(names) + cols - 1) // cols
    for r in range(rows):
        line = []
        for c in range(cols):
            i = c * rows + r
            if i < len(names):
                line.append(names[i].ljust(colw))
        print("".join(line).rstrip())


def _ls_emit(args, rows):
    """rows is a list of (entry, display_path)."""
    fmt_size = (lambda n: str(n)) if args.bytes else human_bytes

    if args.format == "json":
        json.dump([e for e, _ in rows], sys.stdout, indent=2, default=str)
        print()
        return
    if args.format in ("tsv", "csv"):
        writer = csv.writer(
            sys.stdout,
            delimiter="\t" if args.format == "tsv" else ",",
            lineterminator="\n",
        )
        if not args.no_header:
            writer.writerow(["MODE", "USER", "GROUP", "SIZE", "MODIFIED", "NAME"])
        for e, shown in rows:
            writer.writerow(
                [
                    _mode_string(e),
                    e.get("user") or "",
                    e.get("group") or "",
                    fmt_size(e.get("size") or 0),
                    (e.get("last_modified") or "")[:19],
                    shown,
                ]
            )
        return

    if not args.long:
        names = [shown for _, shown in rows]
        if sys.stdout.isatty() and not args.one:
            width = args.width or shutil.get_terminal_size((80, 24)).columns
            _print_columns(names, width)
        else:
            for n in names:
                print(n)
        return

    # long format
    widths = [0, 0, 0]
    prepared = []
    for e, shown in rows:
        user = str(e.get("user") or "-")
        group = str(e.get("group") or "-")
        size = fmt_size(e.get("size") or 0)
        widths = [
            max(widths[0], len(user)),
            max(widths[1], len(group)),
            max(widths[2], len(size)),
        ]
        prepared.append(
            (
                _mode_string(e),
                user,
                group,
                size,
                (e.get("last_modified") or "")[:19],
                shown,
                e.get("link_target"),
            )
        )
    for mode, user, group, size, mtime, shown, link in prepared:
        target = f" -> {link}" if link else ""
        print(
            f"{mode}  {user.ljust(widths[0])}  {group.ljust(widths[1])}  "
            f"{size.rjust(widths[2])}  {mtime:19}  {shown}{target}"
        )


def cmd_ls_fs(args):
    """List a directory on a collection.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    collection, path = _resolve_target(args, tc)

    if args.recursive:
        rows = []
        for dirpath, _depth, entries in _walk_or_explain(
            args, tc, collection, path, maxdepth=args.maxdepth
        ):
            for e in _ls_sort(list(entries), args):
                rows.append((e, posixpath.join(dirpath, _ls_name(e, args))))
        _ls_emit(args, rows)
        _report_walk_errors(args)
        return

    kwargs = {"path": path, "show_hidden": not args.no_hidden}
    if args.local_user:
        kwargs["local_user"] = args.local_user
    try:
        entries = list(tc.operation_ls(collection, **kwargs))
    except globus_sdk.TransferAPIError as e:
        consent, scopes = globus_fs.needs_consent(e)
        if consent:
            sys.exit(
                f"{e.code}: {e.message}\n" + globus_fs.consent_hint(collection, scopes)
            )
        handle_api_error(e)
    _ls_emit(args, [(e, _ls_name(e, args)) for e in _ls_sort(entries, args)])


def cmd_du(args):
    """Report cumulative size and file counts per directory.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    collection, root = _resolve_target(args, tc)
    root = root.rstrip("/") or "/"

    own = {}  # path -> [bytes, files, dirs]
    for path, _depth, entries in _walk_or_explain(
        args, tc, collection, root, maxdepth=args.maxdepth
    ):
        size = sum(e.get("size") or 0 for e in entries if e.get("type") != "dir")
        files = sum(1 for e in entries if e.get("type") != "dir")
        dirs = sum(1 for e in entries if e.get("type") == "dir")
        own[path] = [size, files, dirs]

    if not own:
        print("nothing listed", file=sys.stderr)
        _report_walk_errors(args)
        return

    # roll each directory's own contents up into all of its ancestors
    totals = {}
    for path, (size, files, dirs) in own.items():
        node = path
        while True:
            acc = totals.setdefault(node, [0, 0, 0])
            acc[0] += size
            acc[1] += files
            acc[2] += dirs
            if node == root:
                break
            parent = posixpath.dirname(node) or "/"
            if parent == node:
                break
            node = parent

    def depth_of(path):
        """Depth of a path below the walk root.

        :param path: an absolute path at or below the root
        :returns: 0 for the root itself, 1 for its children, and so on
        """
        if path == root:
            return 0
        rel = path[len(root) :].strip("/")
        return len(rel.split("/")) if rel else 0

    rows = [
        (p, t)
        for p, t in totals.items()
        if args.depth is None or depth_of(p) <= args.depth
    ]
    if args.sort == "size":
        rows.sort(key=lambda r: r[1][0], reverse=True)
    elif args.sort == "files":
        rows.sort(key=lambda r: r[1][1], reverse=True)
    else:
        rows.sort(key=lambda r: r[0])

    if args.format == "json":
        json.dump(
            [{"path": p, "bytes": t[0], "files": t[1], "dirs": t[2]} for p, t in rows],
            sys.stdout,
            indent=2,
        )
        print()
        _report_walk_errors(args)
        return

    fmt_size = (lambda n: str(n)) if args.bytes else human_bytes
    if args.format in ("tsv", "csv"):
        writer = csv.writer(
            sys.stdout,
            delimiter="\t" if args.format == "tsv" else ",",
            lineterminator="\n",
        )
        if not args.no_header:
            writer.writerow(["SIZE", "FILES", "DIRS", "PATH"])
        for p, t in rows:
            writer.writerow([fmt_size(t[0]), t[1], t[2], p])
    else:
        width = max([len(fmt_size(t[0])) for _, t in rows] + [4])
        fwidth = max([len(str(t[1])) for _, t in rows] + [5])
        dwidth = max([len(str(t[2])) for _, t in rows] + [4])
        if not args.no_header:
            print(
                f"{'SIZE'.rjust(width)}  {'FILES'.rjust(fwidth)}  "
                f"{'DIRS'.rjust(dwidth)}  PATH"
            )
        for p, t in rows:
            print(
                f"{fmt_size(t[0]).rjust(width)}  {str(t[1]).rjust(fwidth)}  "
                f"{str(t[2]).rjust(dwidth)}  {p}"
            )

    walker = getattr(args, "_walker", None)
    if args.summary and walker:
        print(
            f"\n{walker.dirs_scanned} director(ies) scanned in "
            f"{time.time() - walker._t0:.0f}s",
            file=sys.stderr,
        )
    _report_walk_errors(args)


def cmd_consent(args):
    """Grant the extra consent a GCS mapped collection needs for listing."""
    app = build_app(getattr(args, "client_id", None))
    tc = globus_sdk.TransferClient(app=app)
    try:
        tc.add_app_data_access_scope(args.collection)
    except globus_sdk.GlobusSDKUsageError as e:
        sys.exit(f"cannot add the data_access scope: {e}")
    app.login(force=True)
    try:
        tc.operation_ls(args.collection, path="/", query_params={"limit": 1})
        print(f"Consent granted; {args.collection} is listable.")
    except globus_sdk.TransferAPIError as e:
        consent, scopes = globus_fs.needs_consent(e)
        if consent:
            sys.exit(
                "Still ConsentRequired after login.\n"
                + globus_fs.consent_hint(args.collection, scopes)
                + "\nNote: data_access does not apply to guest "
                "collections, GCP, or High Assurance collections -- "
                "those use a different consent path."
            )
        sys.exit(f"Listing still failed: {e.code}: {e.message}")


def _no_shortcuts_message():
    """Explain that nothing is configured, and what to do about it.

    Deliberately avoids a realistic-looking example: a line such as
    ``dataden = ab65757f-...`` here reads as though that shortcut already
    exists, which is exactly the wrong impression to leave.

    :returns: the message to print
    """
    rows = globus_collections.describe_sources(SHORTCUT_FILE)
    width = max(len(location) for location, _ in rows)
    lines = [
        "No collection shortcuts are configured on this system.",
        "",
        "Looked in:",
    ]
    lines += [f"    {location.ljust(width)}   {note}" for location, note in rows]
    lines += [
        "",
        "To give a collection your own short name, a bookmark is usually",
        "easier -- Globus stores it, so globus-cli and the web app see it too:",
        "",
        "    globus-activity bookmark create <collection-uuid>:/ <name>",
        "",
        "To ship names for everyone on this system, write the site file with",
        "one line per collection, substituting your own values:",
        "",
        f"    # {globus_collections.DEFAULT_SITE_FILE}",
        "    <name> = <collection-uuid>",
        "",
        "Full format:  globus-activity collections --help",
    ]
    return "\n".join(lines)


def cmd_collections(args):
    """List the configured collection shortcuts and where they came from.

    :param args: parsed arguments
    """
    shortcuts = site_shortcuts()
    if args.format == "json":
        json.dump(
            [
                {
                    "name": s.name,
                    "collection": s.collection,
                    "path": s.path,
                    "source": s.source,
                }
                for s in sorted(shortcuts.values(), key=lambda s: s.name)
            ],
            sys.stdout,
            indent=2,
        )
        print()
        return

    if not shortcuts:
        print(_no_shortcuts_message())
        return

    shadowed = set()
    if args.check_bookmarks:
        try:
            shadowed = {
                b.get("name") for b in bookmark_index(transfer_client(args))
            } & set(shortcuts)
        except SystemExit:
            shadowed = set()

    rows = [
        (
            s.name,
            s.collection,
            s.path,
            s.source,
            "shadowed by your bookmark" if s.name in shadowed else "",
        )
        for s in sorted(shortcuts.values(), key=lambda s: s.name)
    ]
    headers = ("NAME", "COLLECTION", "PATH", "SOURCE", "")
    widths = [max([len(r[i]) for r in rows] + [len(headers[i])]) for i in range(5)]
    if not args.no_header:
        print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for row in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())


def cmd_bookmark_list(args):
    """List the user's Globus bookmarks.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    bookmarks = bookmark_index(tc)
    if args.format == "json":
        json.dump(bookmarks, sys.stdout, indent=2, default=str)
        print()
        return
    if not bookmarks:
        print(
            "no bookmarks.  Create one with:\n"
            "    globus-activity bookmark create <collection-uuid>:/path/ NAME"
        )
        return
    rows = [
        (
            b.get("name", ""),
            b.get("endpoint_id", ""),
            b.get("id", ""),
            b.get("path", ""),
        )
        for b in sorted(bookmarks, key=lambda b: b.get("name") or "")
    ]
    headers = ("Name", "Endpoint ID", "Bookmark ID", "Path")
    widths = [max([len(str(r[i])) for r in rows] + [len(headers[i])]) for i in range(4)]
    if not args.no_header:
        print(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
        print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def cmd_bookmark_show(args):
    """Show one bookmark, by default as ``ENDPOINT_ID:PATH``.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    bm = resolve_bookmark(tc, args.bookmark)
    if bm is None:
        sys.exit(f"no bookmark named '{args.bookmark}'")
    if args.format == "json":
        json.dump(bm, sys.stdout, indent=2, default=str)
        print()
        return
    if args.verbose:
        width = len("Endpoint ID")
        for label, key in (
            ("ID", "id"),
            ("Name", "name"),
            ("Endpoint ID", "endpoint_id"),
            ("Path", "path"),
        ):
            print(f"{label.ljust(width)}: {bm.get(key, '')}")
        return
    # default is the substitution-friendly form, same as globus-cli
    print(f"{bm.get('endpoint_id', '')}:{bm.get('path', '')}")


def cmd_bookmark_create(args):
    """Create a bookmark from ``ENDPOINT_ID:PATH`` and a name.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    head, sep, path = args.endpoint_plus_path.partition(":")
    if not sep or not _is_uuid(head.strip()):
        sys.exit(
            "expected COLLECTION_UUID:/path/ as the first argument, "
            "e.g.  globus-activity bookmark create "
            "1234abcd-...:/projects/ projects"
        )
    path = path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"  # the API wants a directory
    try:
        res = tc.create_bookmark(
            {"endpoint_id": head.strip(), "path": path, "name": args.bookmark_name}
        )
    except globus_sdk.TransferAPIError as e:
        handle_api_error(e)
    print(
        f"{res.get('name', args.bookmark_name)}  ->  "
        f"{res.get('endpoint_id', head)}:{res.get('path', path)}"
    )


def cmd_bookmark_rename(args):
    """Rename a bookmark.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    bm = resolve_bookmark(tc, args.bookmark)
    if bm is None:
        sys.exit(f"no bookmark named '{args.bookmark}'")
    try:
        tc.update_bookmark(bm["id"], {"name": args.new_name})
    except globus_sdk.TransferAPIError as e:
        handle_api_error(e)
    print(f"{bm.get('name')} renamed to {args.new_name}")


def cmd_bookmark_delete(args):
    """Delete a bookmark.

    :param args: parsed arguments
    """
    tc = transfer_client(args)
    bm = resolve_bookmark(tc, args.bookmark)
    if bm is None:
        sys.exit(f"no bookmark named '{args.bookmark}'")
    try:
        tc.delete_bookmark(bm["id"])
    except globus_sdk.TransferAPIError as e:
        handle_api_error(e)
    print(f"deleted {bm.get('name')}")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def add_common(p):
    """Add the output and credential options shared by most commands.

    :param p: the parser to extend
    """
    p.add_argument(
        "--format",
        choices=("table", "tsv", "csv", "json"),
        default="table",
        help="output format (default: table)",
    )
    p.add_argument("--no-header", action="store_true", help="omit the header row")
    p.add_argument("--client-id", help="Globus client UUID to authenticate as")


def add_list_opts(p):
    """Add the filtering and display options for task listings.

    :param p: the parser to extend
    """
    p.add_argument(
        "-e",
        "--endpoint",
        help="restrict to endpoint/collection UUID(s), " "comma separated",
    )
    p.add_argument(
        "--endpoint-use",
        choices=("source", "destination"),
        help="with --endpoint, only count it as source or as destination",
    )
    p.add_argument("-u", "--user", help="owner: Globus username or identity UUID")
    p.add_argument("--local-user", help="filter on the local (POSIX) username used")
    p.add_argument("--min-faults", type=int, help="only tasks with at least N faults")
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
        "--mine",
        action="store_true",
        help="your own tasks (GET /task_list) instead of the admin "
        "Activity view; needs no role",
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


MAIN_EPILOG = """\
targets (for ls, find, du)
  UUID:/path              a collection UUID and an absolute path
  name:/path              a collection shortcut or bookmark, plus a path
  name                    that shortcut or bookmark's own location
  A path after a name is resolved beneath it. Your own bookmark wins over a
  site shortcut of the same name.

collection shortcuts        (see: globus-activity collections)
  read from, later winning:
    /etc/globus-activity/collections.conf
    $GLOBUS_ACTIVITY_COLLECTIONS      colon separated files, for Lmod
    ~/.globus-activity/collections.conf
  format:
    NAME = COLLECTION_UUID[:/base/path]       # also NAME: UUID, or NAME UUID

fields (for --fields and --sort)
{fields}

Full reference, with examples: USAGE.md
""".format(
    fields="\n".join(
        "  " + ", ".join(sorted(FIELDS)[i : i + 6]) for i in range(0, len(FIELDS), 6)
    )
)

COLLECTIONS_EPILOG = """\
Shortcuts let people type a name instead of a collection UUID. An
administrator ships them for everyone; a user can add their own.

read from, later winning:
  /etc/globus-activity/collections.conf     everyone on this machine
  $GLOBUS_ACTIVITY_COLLECTIONS              colon separated files, for Lmod
  ~/.globus-activity/collections.conf       your own additions

format:
  # NAME = COLLECTION_UUID[:/optional/base/path]
  dataden = ab65757f-00f5-4e5b-aa21-133187732a01
  scratch = 1a2b3c4d-0000-0000-0000-000000000000:/scratch

  NAME: UUID and bare NAME UUID also parse. '#' starts a comment. A file
  beginning with '{' is read as JSON. A malformed line is skipped, not
  fatal. Names take no spaces and no colons.

A bookmark of the same name takes precedence; --check-bookmarks flags those.
"""


def build_parser():
    """Build the full command line parser.

    :returns: the configured top-level parser
    """
    p = argparse.ArgumentParser(
        prog="globus-activity",
        description="Table view of the Globus console Activity tab, one line per task.",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    ls = sub.add_parser(
        "tasks", help="list in-progress transfer tasks (default command)"
    )
    ls.add_argument("--status", help="ACTIVE,INACTIVE (default), or any task status")
    ls.add_argument(
        "--paused",
        dest="paused",
        action="store_true",
        default=None,
        help="only paused tasks",
    )
    ls.add_argument(
        "--not-paused",
        dest="paused",
        action="store_false",
        help="only tasks that are not paused",
    )
    add_list_opts(ls)
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
    w.add_argument("--status", help="ACTIVE,INACTIVE (default), or any task status")
    w.add_argument(
        "--paused",
        dest="paused",
        action="store_true",
        default=None,
        help="only paused tasks",
    )
    w.add_argument(
        "--not-paused",
        dest="paused",
        action="store_false",
        help="only tasks that are not paused",
    )
    add_list_opts(w)
    add_common(w)
    w.set_defaults(func=cmd_watch)

    h = sub.add_parser("history", help="completed tasks for an endpoint")
    h.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="look back this many hours (default: 24)",
    )
    h.add_argument("--status", help="SUCCEEDED,FAILED (default)")
    add_list_opts(h)
    add_common(h)
    h.set_defaults(func=cmd_history)

    t = sub.add_parser("task", help="everything about one task")
    t.add_argument("task_id")
    t.add_argument(
        "--mine", action="store_true", help="read it as your own task (no role needed)"
    )
    t.add_argument("--events", action="store_true", help="also show the event log")
    t.add_argument("--events-limit", type=int, default=50)
    t.add_argument("-v", "--verbose", action="store_true", help="include event details")
    add_common(t)
    t.set_defaults(func=cmd_task)

    e = sub.add_parser("endpoints", help="endpoints you can monitor")
    add_common(e)
    e.set_defaults(func=cmd_endpoints)

    # ---- filesystem helpers -------------------------------------------
    def add_walk_opts(sp):
        """Add the target and traversal options for filesystem commands.

        :param sp: the parser to extend
        """
        sp.add_argument(
            "target",
            help="COLLECTION_UUID:/path, or a bookmark name "
            "(see the 'bookmark' command)",
        )
        sp.add_argument(
            "-j",
            "--jobs",
            type=int,
            default=8,
            help="concurrent directory listings (default: 8)",
        )
        sp.add_argument(
            "--maxdepth", type=int, help="do not descend deeper than N levels"
        )
        sp.add_argument("--no-hidden", action="store_true", help="skip dotfiles")
        sp.add_argument(
            "--local-user",
            help="map to this local account (GCSv5 mapped " "collections only)",
        )
        sp.add_argument(
            "--max-errors",
            type=int,
            default=10,
            help="unlistable directories to print (default: 10)",
        )
        sp.add_argument(
            "--summary", action="store_true", help="print totals to stderr when done"
        )

    lsp = sub.add_parser("ls", help="list a directory on a collection")
    lsp.add_argument("target", help="COLLECTION_UUID:/path, or a bookmark name")
    lsp.add_argument(
        "-l", "--long", action="store_true", help="mode, owner, size and mtime"
    )
    lsp.add_argument(
        "-R", "--recursive", action="store_true", help="descend into subdirectories"
    )
    lsp.add_argument(
        "-1", "--one", action="store_true", help="one name per line even on a terminal"
    )
    lsp.add_argument(
        "-F", "--classify", action="store_true", help="append / to directory names"
    )
    lsp.add_argument(
        "-S", dest="sort_size", action="store_true", help="sort by size, largest first"
    )
    lsp.add_argument(
        "-t",
        dest="sort_time",
        action="store_true",
        help="sort by modification time, newest first",
    )
    lsp.add_argument("-r", "--reverse", action="store_true", help="reverse order")
    lsp.add_argument(
        "-b", "--bytes", action="store_true", help="raw bytes instead of human units"
    )
    lsp.add_argument("--no-hidden", action="store_true", help="skip dotfiles")
    lsp.add_argument(
        "--local-user", help="map to this local account (GCSv5 mapped collections)"
    )
    lsp.add_argument("--width", type=int, help="terminal width for columns")
    lsp.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=8,
        help="concurrent listings, with -R (default: 8)",
    )
    lsp.add_argument("--maxdepth", type=int, help="with -R, depth limit")
    lsp.add_argument(
        "--max-errors",
        type=int,
        default=10,
        help="unlistable directories to print (default: 10)",
    )
    add_common(lsp)
    lsp.set_defaults(func=cmd_ls_fs, summary=False)

    f = sub.add_parser("find", help="recursive search over a collection")
    add_walk_opts(f)
    f.add_argument("--name", help="glob on the file name, e.g. '*.fastq.gz'")
    f.add_argument("--iname", help="like --name, case insensitive")
    f.add_argument("--type", choices=("f", "d"), help="files or directories only")
    f.add_argument(
        "--min-size", metavar="SIZE", help="at least this big, e.g. 100M, 2G"
    )
    f.add_argument("--max-size", metavar="SIZE", help="at most this big")
    f.add_argument(
        "--newer",
        metavar="AGE",
        help="modified more recently than 7d, 12h, or an ISO date",
    )
    f.add_argument("--older", metavar="AGE", help="modified before that")
    f.add_argument(
        "-l", "--long", action="store_true", help="show type, size and mtime"
    )
    f.add_argument(
        "-0", "--print0", action="store_true", help="NUL separate for xargs -0"
    )
    f.add_argument("--limit", type=int, default=0, help="stop after N matches")
    f.add_argument("--client-id", help="Globus client UUID")
    f.set_defaults(func=cmd_find, format="table", no_header=False)

    d = sub.add_parser("du", help="sizes and file counts per directory")
    add_walk_opts(d)
    d.add_argument(
        "-d", "--depth", type=int, help="show totals down to this depth (like du -d)"
    )
    d.add_argument(
        "-b", "--bytes", action="store_true", help="raw bytes instead of human units"
    )
    d.add_argument(
        "--sort",
        choices=("size", "files", "path"),
        default="size",
        help="ordering (default: size)",
    )
    add_common(d)
    d.set_defaults(func=cmd_du)

    c = sub.add_parser(
        "consent",
        help="grant the data_access consent a mapped " "collection needs for listing",
    )
    c.add_argument("collection", help="collection UUID")
    c.add_argument("--client-id", help="Globus client UUID")
    c.set_defaults(func=cmd_consent)

    col = sub.add_parser(
        "collections",
        help="named collection shortcuts configured for this system",
        epilog=COLLECTIONS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    col.add_argument(
        "--check-bookmarks",
        action="store_true",
        help="also flag names your own bookmarks shadow (makes an API call)",
    )
    add_common(col)
    col.set_defaults(func=cmd_collections)

    # Bookmarks are server-side and shared with `globus bookmark` and the web
    # app, so the names and argument order deliberately match globus-cli.
    bm = sub.add_parser(
        "bookmark",
        help="named collection+path shortcuts (shared with "
        "globus-cli and the web app)",
    )
    bmsub = bm.add_subparsers(dest="bookmark_cmd")

    bml = bmsub.add_parser("list", help="list bookmarks")
    add_common(bml)
    bml.set_defaults(func=cmd_bookmark_list)

    bms = bmsub.add_parser("show", help="show one bookmark as ENDPOINT_ID:PATH")
    bms.add_argument("bookmark", metavar="BOOKMARK_ID_OR_NAME")
    bms.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show all fields instead of ENDPOINT_ID:PATH",
    )
    add_common(bms)
    bms.set_defaults(func=cmd_bookmark_show)

    bmc = bmsub.add_parser("create", help="create a bookmark")
    bmc.add_argument("endpoint_plus_path", metavar="ENDPOINT_ID:PATH")
    bmc.add_argument("bookmark_name", metavar="BOOKMARK_NAME")
    add_common(bmc)
    bmc.set_defaults(func=cmd_bookmark_create)

    bmr = bmsub.add_parser("rename", help="rename a bookmark")
    bmr.add_argument("bookmark", metavar="BOOKMARK_ID_OR_NAME")
    bmr.add_argument("new_name", metavar="NEW_BOOKMARK_NAME")
    add_common(bmr)
    bmr.set_defaults(func=cmd_bookmark_rename)

    bmd = bmsub.add_parser("delete", help="delete a bookmark")
    bmd.add_argument("bookmark", metavar="BOOKMARK_ID_OR_NAME")
    add_common(bmd)
    bmd.set_defaults(func=cmd_bookmark_delete)

    bm.set_defaults(func=lambda a: (bm.print_help(), sys.exit(1)))

    lo = sub.add_parser("login", help="authenticate to Globus")
    lo.add_argument("--client-id", help="Globus client UUID (saved for next time)")
    lo.add_argument("--force", action="store_true", help="re-run the login flow")
    lo.add_argument(
        "--fix-permissions",
        action="store_true",
        help="chmod the credential store to owner-only instead of refusing",
    )
    lo.set_defaults(func=cmd_login)

    sub.add_parser("logout", help="forget stored tokens").set_defaults(func=cmd_logout)

    who = sub.add_parser("whoami", help="show the logged-in identity")
    add_common(who)
    who.set_defaults(func=cmd_whoami)

    # Recorded so main() can tell a command from a bare flag without keeping
    # a second list in step with this one.
    p.command_names = set(sub.choices)
    return p


def _glue_negative_values(argv):
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


def main(argv=None):
    """Run the CLI.

    :param argv: argument list, defaulting to ``sys.argv[1:]``
    :returns: the process exit status
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = _glue_negative_values(argv)
    parser = build_parser()
    known = parser.command_names | {"-h", "--help"}
    if not argv or argv[0] not in known:
        argv = ["tasks"] + argv  # bare `globus-activity --errors` works
    args = parser.parse_args(argv)
    args._tc = getattr(args, "_tc", None)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        args.func(args)
    except globus_sdk.GlobusAPIError as e:
        handle_api_error(e)
    except BrokenPipeError:  # piping into head/grep -m
        try:
            sys.stdout.close()
        finally:
            os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
