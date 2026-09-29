"""globus-console - the Globus web console's Activity tab, as a greppable table.

For anyone holding an activity role. Every command here reads the Transfer
API's ``endpoint_manager`` resources, which require an effective
``activity_monitor`` or ``activity_manager`` role on a *subscribed*
collection; without one anywhere, Globus answers every call with 403
PermissionDenied.

That is not only subscription administrators. A collection's owner always
holds an effective ``administrator`` role, which implies both activity
roles, so a user who created a guest collection sees every transfer to and
from it -- and can grant ``activity_monitor`` on it to a lab manager.
Roles also inherit downward: one on an endpoint or mapped collection
covers its guest collections.

For browsing storage and your own transfers, use globus-fs, which needs no
role at all.
"""

from __future__ import annotations

import json
import sys

import globus_sdk

from . import tasks
from .common import Tool, add_common, new_parser

FORBIDDEN_HINT = """

The Activity view requires the 'activity_monitor' or 'activity_manager'
role on a *subscribed* collection. You hold both automatically on a guest
collection you created; otherwise a collection administrator must grant
one. Without a role on at least one collection, every globus-console
command returns this error.

  see what you can monitor:  globus-console endpoints
  list your own transfers:   globus-fs tasks"""


def _login_check(tool, app):
    """Report, right after login, whether this identity can see the console.

    :param tool: the tool that logged in
    :param app: the freshly authenticated GlobusApp
    """
    tc = globus_sdk.TransferClient(app=app)
    try:
        monitored = list(tc.endpoint_manager_monitored_endpoints())
    except globus_sdk.TransferAPIError as e:
        print(
            f"Logged in, but this identity has no activity monitor role "
            f"({e.http_status} {e.code}).\n"
            "Ask a subscription administrator to grant activity_monitor on "
            "your collections.",
            file=sys.stderr,
        )
        return
    print(f"Logged in. You can monitor {len(monitored)} collection(s).")


TOOL = Tool(
    "globus-console",
    forbidden_hint=FORBIDDEN_HINT,
    unattended=True,
    login_check=_login_check,
)


def cmd_endpoints(args):
    """List the collections this identity may monitor.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    try:
        eps = list(tc.endpoint_manager_monitored_endpoints())
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)
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


EPILOG = """\
Requires the activity_monitor or activity_manager role on a subscribed
collection -- which the owner of a guest collection holds automatically.
For your own transfers, and ls/find/du, use globus-fs.

fields (for --fields and --sort)
{fields}

Full reference, with examples: USAGE.md
""".format(
    fields="\n".join(
        "  " + ", ".join(sorted(tasks.FIELDS)[i : i + 6])
        for i in range(0, len(tasks.FIELDS), 6)
    )
)


def build_parser():
    """Build the globus-console command line parser.

    :returns: the configured top-level parser
    """
    p = new_parser(
        "globus-console",
        "Table view of the Globus console Activity tab, one line per task.",
        EPILOG,
    )
    sub = p.add_subparsers(dest="cmd")
    tasks.add_task_commands(sub, admin=True)

    e = sub.add_parser("endpoints", help="collections you can monitor")
    add_common(e)
    e.set_defaults(func=cmd_endpoints)

    TOOL.add_auth_commands(sub)
    # Recorded so the runner can tell a command from a bare flag without
    # keeping a second list in step with this one.
    p.command_names = set(sub.choices)
    return p


def main(argv=None):
    """Run globus-console.

    :param argv: argument list, defaulting to ``sys.argv[1:]``
    :returns: the process exit status
    """
    return TOOL.run(
        build_parser(),
        sys.argv[1:] if argv is None else argv,
        default_command="tasks",
        preprocess=tasks.glue_negative_values,
    )


if __name__ == "__main__":
    sys.exit(main())
