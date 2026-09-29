"""What globus-console and globus-fs share: identity, credentials, formatting.

Each command is a :class:`Tool`. A tool owns its own configuration
directory, token file and environment variable prefix, so logging out of
one never logs you out of the other, and a user who only ever runs
globus-fs never sees anything named after the admin console.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import globus_sdk
    from globus_sdk import ClientApp, GlobusAppConfig, UserApp
    from globus_sdk.scopes import AuthScopes, TransferScopes
except ImportError as exc:  # pragma: no cover
    sys.exit(f"globus-sdk 4 is required ({exc}). Run:  pip install -U globus-sdk")

try:  # module was renamed between globus-sdk 3.x and 4.x
    from globus_sdk.token_storage import JSONTokenStorage
except ImportError:  # pragma: no cover
    from globus_sdk.tokenstorage import JSONTokenStorage

from . import security

SCOPES = {
    TransferScopes.resource_server: [TransferScopes.all],
    AuthScopes.resource_server: [
        AuthScopes.openid,
        AuthScopes.profile,
        AuthScopes.email,
        AuthScopes.view_identities,
    ],
}

#: The published "thick client" registration both tools log in through.
#: Not a secret: a native app's client ID only names the application on the
#: consent screen, and every user still authenticates as themselves -- the
#: same reason globus-cli ships its own. Never pair it with a client secret.
DEFAULT_CLIENT_ID = "6a4a00f5-cded-42a4-a2e9-f535af8fc5b0"

SERVICE_ACCOUNT_HELP = """\
{env}_CLIENT_SECRET is set but {env}_CLIENT_ID is not.

A client secret belongs to a service account registration of your own, so
{name} will not pair it with its built-in client ID. Export both:

       export {env}_CLIENT_ID=<service account UUID>
       export {env}_CLIENT_SECRET=<secret>
"""


class Tool:
    """One command line tool: its name, credential store and error advice."""

    def __init__(self, name, forbidden_hint="", login_check=None):
        """Describe a tool.

        :param name: the command name, e.g. ``"globus-fs"``; it also names
            the config directory and the environment variable prefix
        :param forbidden_hint: advice appended to a 403 from the API
        :param login_check: optional callable ``(tool, app)`` run after a
            successful login to report what the new identity can do
        """
        self.name = name
        self.env = name.upper().replace("-", "_")
        self.forbidden_hint = forbidden_hint
        self.login_check = login_check

    # ---- paths ----------------------------------------------------------
    @property
    def config_dir(self) -> str:
        """The private directory holding this tool's config and tokens.

        Read on every access so a test or wrapper can move it with
        ``$<TOOL>_HOME``.

        :returns: an absolute path, e.g. ``~/.globus-fs`` expanded
        """
        return os.path.expanduser(os.environ.get(f"{self.env}_HOME", f"~/.{self.name}"))

    @property
    def config_file(self) -> str:
        """The saved configuration, currently just the client UUID.

        :returns: the path of ``config.json``
        """
        return os.path.join(self.config_dir, "config.json")

    @property
    def token_file(self) -> str:
        """The refresh token store.

        :returns: the path of ``tokens.json``
        """
        return os.path.join(self.config_dir, "tokens.json")

    # ---- config ---------------------------------------------------------
    def load_config(self) -> dict:
        """Read the saved configuration.

        :returns: the config mapping, or an empty dict if missing or corrupt
        """
        try:
            with open(self.config_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_config(self, cfg: dict) -> None:
        """Write the config file atomically, owner-readable only.

        :param cfg: the configuration mapping to serialise
        """
        security.secure_directory(self.config_dir)
        tmp = self.config_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.config_file)

    def get_client_id(self, override: str | None = None) -> str:
        """Resolve the Globus client UUID to authenticate as.

        Precedence: explicit argument, then ``$<TOOL>_CLIENT_ID``, then the
        saved config, then :data:`DEFAULT_CLIENT_ID`. The built-in default
        is skipped when a client secret is set, since a secret only makes
        sense with the service account registration it was issued for.

        :param override: a client UUID supplied on the command line
        :returns: the client UUID
        :raises SystemExit: if a secret is set without its own client UUID
        """
        explicit = (
            override
            or os.environ.get(f"{self.env}_CLIENT_ID")
            or self.load_config().get("client_id")
        )
        if explicit:
            return explicit
        if os.environ.get(f"{self.env}_CLIENT_SECRET"):
            sys.exit(SERVICE_ACCOUNT_HELP.format(name=self.name, env=self.env))
        return DEFAULT_CLIENT_ID

    # ---- auth -----------------------------------------------------------
    def build_app(
        self, client_id: str | None = None, fix_permissions: bool = False
    ) -> globus_sdk.GlobusApp:
        """Build the GlobusApp that owns login and the token store.

        The credential store is checked before it is touched. A refresh
        token readable by other local users is treated the way ssh treats
        an exposed private key: refuse, explain, and say how to fix it.

        :param client_id: override for the configured Globus client UUID
        :param fix_permissions: True to chmod paths this user owns rather
            than refusing to run
        :returns: a ``ClientApp`` when a client secret is set, else a
            ``UserApp``
        """
        cid = self.get_client_id(client_id)
        try:
            security.secure_directory(self.config_dir, fix=fix_permissions)
            repaired = security.assert_secure(
                self.config_dir,
                self.token_file,
                self.config_file,
                fix=fix_permissions,
            )
        except security.InsecureCredentialStore as exc:
            sys.exit(exc.banner(self.name))
        for path in repaired:
            print(f"tightened permissions on {path}", file=sys.stderr)
        config = GlobusAppConfig(
            token_storage=JSONTokenStorage(self.token_file),
            request_refresh_tokens=True,
        )
        secret = os.environ.get(f"{self.env}_CLIENT_SECRET")
        if secret:
            return ClientApp(
                self.name,
                client_id=cid,
                client_secret=secret,
                scope_requirements=SCOPES,
                config=config,
            )
        return UserApp(
            self.name, client_id=cid, scope_requirements=SCOPES, config=config
        )

    def transfer_client(self, args) -> globus_sdk.TransferClient:
        """Build a Transfer API client bound to the credential store.

        :param args: parsed arguments, read for ``--client-id``
        :returns: an authenticated ``TransferClient``
        """
        return globus_sdk.TransferClient(
            app=self.build_app(getattr(args, "client_id", None))
        )

    def auth_client(self, args) -> globus_sdk.AuthClient:
        """Build an Auth API client, used to resolve usernames to identities.

        :param args: parsed arguments, read for ``--client-id``
        :returns: an authenticated ``AuthClient``
        """
        return globus_sdk.AuthClient(
            app=self.build_app(getattr(args, "client_id", None))
        )

    def api_error(self, e) -> None:
        """Turn a Globus API error into an actionable message and exit.

        :param e: the API error to explain
        :raises SystemExit: always
        """
        if e.http_status == 403:
            sys.exit(f"Permission denied ({e.code}): {e.message}{self.forbidden_hint}")
        if e.http_status == 401:
            sys.exit(
                f"Not authenticated ({e.code}): {e.message}\nRun:  {self.name} login"
            )
        sys.exit(f"Transfer API error {e.http_status} ({e.code}): {e.message}")

    # ---- login / logout / whoami ----------------------------------------
    def cmd_login(self, args):
        """Authenticate to Globus and store refresh tokens.

        :param args: parsed arguments
        """
        cid = self.get_client_id(args.client_id)
        if args.client_id:
            cfg = self.load_config()
            cfg["client_id"] = args.client_id
            self.save_config(cfg)
        app = self.build_app(cid, fix_permissions=args.fix_permissions)
        app.login(force=args.force)
        # the SDK writes new token files under a user-only umask, but an
        # existing one keeps whatever mode it already had
        security.harden(self.token_file)
        if self.login_check:
            self.login_check(self, app)
        else:
            print("Logged in.")
        print(f"Tokens stored in {self.token_file}")

    def cmd_logout(self, args):
        """Delete the stored tokens.

        :param args: parsed arguments, unused
        """
        try:
            os.remove(self.token_file)
            print(f"removed {self.token_file}")
        except FileNotFoundError:
            print(f"no tokens at {self.token_file}")

    def cmd_whoami(self, args):
        """Print the identity the stored tokens belong to.

        :param args: parsed arguments
        """
        ac = self.auth_client(args)
        try:
            info = ac.userinfo()
        except globus_sdk.AuthAPIError as e:
            sys.exit(f"Auth error {e.http_status} ({e.code}): {e.message}")
        print(
            json.dumps(info.data, indent=2)
            if args.format == "json"
            else f"{info.get('preferred_username')}  ({info.get('sub')})"
        )

    def add_auth_commands(self, sub):
        """Add ``login``, ``logout`` and ``whoami`` to a subparser set.

        :param sub: the object returned by ``add_subparsers``
        """
        lo = sub.add_parser("login", help="authenticate to Globus")
        lo.add_argument("--client-id", help="Globus client UUID (saved for next time)")
        lo.add_argument("--force", action="store_true", help="re-run the login flow")
        lo.add_argument(
            "--fix-permissions",
            action="store_true",
            help="chmod the credential store to owner-only instead of refusing",
        )
        lo.set_defaults(func=self.cmd_login)

        sub.add_parser("logout", help="forget stored tokens").set_defaults(
            func=self.cmd_logout
        )

        who = sub.add_parser("whoami", help="show the logged-in identity")
        add_common(who)
        who.set_defaults(func=self.cmd_whoami)

    def run(self, parser, argv, default_command=None, preprocess=None):
        """Parse arguments and dispatch to the chosen command.

        :param parser: the tool's top-level parser, carrying ``command_names``
        :param argv: the argument list, without the program name
        :param default_command: command assumed when argv does not start
            with one, e.g. ``"tasks"`` so bare ``globus-console --errors``
            works; None to show help instead
        :param preprocess: optional callable applied to argv first
        :returns: the process exit status
        """
        argv = list(argv)
        if preprocess:
            argv = preprocess(argv)
        known = parser.command_names | {"-h", "--help"}
        if default_command and (not argv or argv[0] not in known):
            argv = [default_command] + argv
        args = parser.parse_args(argv)
        if not hasattr(args, "func"):
            parser.print_help()
            return 1
        args.tool = self
        args._tc = getattr(args, "_tc", None)
        try:
            args.func(args)
        except globus_sdk.GlobusAPIError as e:
            self.api_error(e)
        except BrokenPipeError:  # piping into head/grep -m
            try:
                sys.stdout.close()
            finally:
                os._exit(0)
        return 0


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


def new_parser(prog, description, epilog):
    """Build a top-level parser with the formatting both tools use.

    :param prog: the command name
    :param description: one line shown under usage
    :param epilog: preformatted text shown after the options
    :returns: an ``ArgumentParser``
    """
    return argparse.ArgumentParser(
        prog=prog,
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def is_uuid(value):
    """Cheap check for something shaped like a UUID.

    :param value: the string to test
    :returns: True if it looks like a hyphenated UUID
    """
    return len(value) == 36 and value.count("-") == 4


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
