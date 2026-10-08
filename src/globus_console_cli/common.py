"""What globus-console and globus-fs share: identity, credentials, formatting.

Each command is a :class:`Tool` wrapping one globus-sdk ``GlobusApp``. The
SDK owns the credential lifecycle -- where tokens live, the login flow,
refresh, revocation on logout, and re-login when a collection asks for
more consent -- and this module adds only what the SDK does not do:

- check the token file's permissions before the SDK reads it
  (:mod:`.security`, archivetar's rule), and
- keep the login prompt on stderr, so a re-login that interrupts
  ``find ... | xargs`` never writes into the pipe.

Each tool is its own app, with its own token file, so logging out of one
never logs you out of the other.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import threading
from datetime import datetime, timezone

try:
    import globus_sdk
    from globus_sdk import ClientApp, GlobusAppConfig, UserApp
    from globus_sdk.login_flows import CommandLineLoginFlowManager
    from globus_sdk.scopes import AuthScopes, TransferScopes
    from globus_sdk.token_storage import JSONTokenStorage
except ImportError as exc:  # pragma: no cover
    sys.exit(f"globus-sdk 4 is required ({exc}). Run:  pip install -U globus-sdk")

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

#: GlobusApp's storage namespace; fixed by the SDK, needed to build its storage.
STORAGE_NAMESPACE = "DEFAULT"

SERVICE_ACCOUNT_HELP = """\
{env}_CLIENT_SECRET is set but {env}_CLIENT_ID is not.

A client secret belongs to a service account registration of your own, so
{name} will not pair it with its built-in client ID. Export both:

       export {env}_CLIENT_ID=<service account UUID>
       export {env}_CLIENT_SECRET=<secret>
"""


class StderrLoginFlowManager(CommandLineLoginFlowManager):
    """The SDK's command line login, talking to the terminal on stderr.

    Identical to :class:`CommandLineLoginFlowManager` except that the URL
    and the code prompt go to stderr. A login can now start in the middle of
    a command -- when a collection asks for consent -- and stdout may be a
    pipe at that moment.
    """

    def print_authorize_url(self, authorize_url: str) -> None:
        """Show the login URL on stderr.

        :param authorize_url: where the user logs in and consents
        """
        rule = "-" * 36
        print(
            f"\nPlease authenticate with Globus here:\n{rule}\n"
            f"{authorize_url}\n{rule}\n",
            file=sys.stderr,
        )

    def prompt_for_code(self) -> str:
        """Read the authorization code, prompting on stderr.

        :returns: the code the user pasted
        """
        sys.stderr.write("Enter the resulting Authorization Code here: ")
        sys.stderr.flush()
        with self._handle_input_errors():
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line.strip()


class Tool:
    """One command line tool: its name, its GlobusApp and its error advice."""

    def __init__(self, name, forbidden_hint="", login_check=None):
        """Describe a tool.

        :param name: the command name, e.g. ``"globus-fs"``; it is also the
            SDK app name, which places the token file, and the environment
            variable prefix
        :param forbidden_hint: advice appended to a 403 from the API
        :param login_check: optional callable ``(tool, app)`` run after a
            successful login to report what the new identity can do
        """
        self.name = name
        self.env = name.upper().replace("-", "_")
        self.forbidden_hint = forbidden_hint
        self.login_check = login_check
        self._app = None
        self._config = None
        self._storage = None
        # serialises re-logins: parallel listings that all hit an expired
        # high assurance session must produce one login, not eight
        self._login_lock = threading.Lock()
        self._logins = 0

    def get_client_id(self) -> str:
        """Resolve the Globus client UUID to authenticate as.

        ``$<TOOL>_CLIENT_ID`` when set, else :data:`DEFAULT_CLIENT_ID`. The
        built-in default is never used with a client secret, since a secret
        only makes sense with the service account it was issued for.

        :returns: the client UUID
        :raises SystemExit: if a secret is set without its own client UUID
        """
        explicit = os.environ.get(f"{self.env}_CLIENT_ID")
        if explicit:
            return explicit
        if os.environ.get(f"{self.env}_CLIENT_SECRET"):
            sys.exit(SERVICE_ACCOUNT_HELP.format(name=self.name, env=self.env))
        return DEFAULT_CLIENT_ID

    # ---- the app --------------------------------------------------------
    @property
    def app(self) -> globus_sdk.GlobusApp:
        """This tool's GlobusApp, built once per run.

        :returns: a ``ClientApp`` when a client secret is set, else a
            ``UserApp``
        """
        if self._app is None:
            self._app = self._build_app()
        return self._app

    @property
    def config(self) -> GlobusAppConfig:
        """This tool's GlobusApp configuration.

        The SDK's own ``auto_redrive_gares`` is off: it only acts on 403
        responses, and a high assurance collection reports an expired or
        wrong-domain session from a directory listing as a 502. Re-login is
        handled by :meth:`call` and :meth:`run` instead, for any status.

        :returns: the configuration, without token storage yet
        """
        if self._config is None:
            self._config = GlobusAppConfig(
                request_refresh_tokens=True,
                login_flow_manager=StderrLoginFlowManager,
            )
        return self._config

    @property
    def storage(self) -> JSONTokenStorage:
        """The SDK's JSON token storage, at the SDK's default location.

        Built here, rather than left to the app, because the app reads the
        file as soon as it is constructed: owning the storage is what gives
        the path to check first. The location is unchanged,
        ``~/.globus/app/<client-id>/<app-name>/tokens.json``.

        :returns: the storage, with ``~/.globus`` created 700 if it was missing
        """
        if self._storage is None:
            security.ensure_globus_dir()
            self._storage = JSONTokenStorage.for_globus_app(
                client_id=self.get_client_id(),
                app_name=self.name,
                config=self.config,
                namespace=STORAGE_NAMESPACE,
            )
            # this tool's own directory is used by nothing else, so it can
            # be private without the compatibility cost of ~/.globus
            security.private_directory(os.path.dirname(self._storage.filepath))
        return self._storage

    @property
    def token_file(self) -> str:
        """Where the SDK keeps this tool's tokens.

        :returns: the token file path
        """
        return self.storage.filepath

    @property
    def config_dir(self) -> str:
        """This tool's own directory: its tokens and the user's config.

        Everything per-user lives together, the way ``~/.ssh`` holds both
        keys and ``config``.

        :returns: ``~/.globus/app/<client-id>/<app-name>``
        """
        return os.path.dirname(self.token_file)

    def _build_app(self) -> globus_sdk.GlobusApp:
        """Build the app, checking the token file before the SDK reads it.

        :returns: a ``ClientApp`` when a client secret is set, else a
            ``UserApp``
        """
        try:
            warning = security.check_token_file(self.token_file)
        except security.UnsafeTokenFile as exc:
            sys.exit(str(exc))
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        config = dataclasses.replace(self.config, token_storage=self.storage)

        cid = self.get_client_id()
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

    # ---- re-login on demand ---------------------------------------------
    def call(self, fn, *args, **kwargs):
        """Call the API, logging in again once if it asks for that.

        A Globus Authorization Requirements Error -- consent for a GCS v5
        collection, or a high assurance session that has expired or needs
        a particular domain -- names what the login must satisfy. On a
        terminal, log in with exactly that and retry; otherwise re-raise for
        :meth:`api_error` to explain.

        Safe from many threads at once: whichever thread fails first logs
        in, and the rest retry with the new session.

        :param fn: the API method, e.g. ``tc.operation_ls``
        :param args: positional arguments for it
        :param kwargs: keyword arguments for it
        :returns: whatever ``fn`` returns
        """
        seen = self._logins
        try:
            return fn(*args, **kwargs)
        except globus_sdk.GlobusAPIError as error:
            if not self.relogin(error, seen):
                raise
        return fn(*args, **kwargs)

    def relogin(self, error, seen) -> bool:
        """Log in again to satisfy what an API error asked for.

        :param error: the API error
        :param seen: the login count when the failed request began; if
            another thread has logged in since, just retry
        :returns: True if a retry may now succeed, False if it cannot
        """
        gare = _to_gare(error)
        if gare is None or not sys.stdin.isatty():
            return False
        params = gare.authorization_parameters
        # Always sign in afresh, as globus-cli does. Without this, a browser
        # that already has a session with the required identity is accepted
        # as is -- and a high assurance collection rejects it again when
        # that sign-in is older than its timeout (seen: umich.edu signed in
        # 11 hours earlier, so the login "succeeded" and the retry failed).
        params.prompt = "login"
        with self._login_lock:
            if self._logins == seen:
                if params.session_message:
                    print(f"\n{params.session_message}", file=sys.stderr)
                self.app.login(auth_params=params)
                self._logins += 1
        return True

    def transfer_client(self, args) -> globus_sdk.TransferClient:
        """A Transfer API client on this tool's app.

        :param args: parsed arguments, unused; kept so tests can stub it
        :returns: an authenticated ``TransferClient``
        """
        return globus_sdk.TransferClient(app=self.app)

    def auth_client(self, args) -> globus_sdk.AuthClient:
        """An Auth API client on this tool's app.

        :param args: parsed arguments, unused
        :returns: an authenticated ``AuthClient``
        """
        return globus_sdk.AuthClient(app=self.app)

    def api_error(self, e) -> None:
        """Turn a Globus API error into an actionable message and exit.

        :param e: the API error to explain
        :raises SystemExit: always
        """
        gare = _to_gare(e)
        if gare is not None:
            sys.exit(f"{e.code}: {e.message}\n\n" + self._login_advice(gare))
        if e.http_status == 403:
            sys.exit(f"Permission denied ({e.code}): {e.message}{self.forbidden_hint}")
        if e.http_status == 401:
            sys.exit(
                f"Not authenticated ({e.code}): {e.message}\nRun:  {self.name} login"
            )
        sys.exit(f"Transfer API error {e.http_status} ({e.code}): {e.message}")

    def _login_advice(self, gare) -> str:
        """Say what a login still needs, after one failed or could not run.

        :param gare: the parsed requirements from the API error
        :returns: advice naming the identity or domain to sign in with
        """
        params = gare.authorization_parameters
        need = params.session_required_single_domain or []
        who = params.session_required_identities or []
        if not sys.stdin.isatty():
            return (
                "This needs you to log in (consent, or a high assurance session).\n"
                f"Run the same {self.name} command in an interactive terminal and "
                "it will ask you to."
            )
        if need or who:
            names = ", ".join(need) or ", ".join(who)
            return (
                f"Globus still refused after logging in. This collection requires "
                f"a recent sign-in with: {names}\n"
                f"Run it again, and on the Globus page sign in with that identity "
                f"(not another linked one). Check yours with:  {self.name} whoami "
                "--format json"
            )
        return "Globus still refused after logging in; try again."

    # ---- login / logout / whoami ----------------------------------------
    def cmd_login(self, args):
        """Log in through the SDK's login flow and store refresh tokens.

        Always a fresh sign-in (``prompt=login``), as in globus-cli, so a
        browser's existing session cannot stand in for one -- high assurance
        collections need a recent authentication.

        :param args: parsed arguments
        """
        self.app.login(
            force=args.force,
            auth_params=globus_sdk.gare.GlobusAuthorizationParameters(prompt="login"),
        )
        if self.login_check:
            self.login_check(self, self.app)
        else:
            print("Logged in.")
        print(f"Tokens stored in {self.token_file}")

    def cmd_logout(self, args):
        """Revoke this tool's tokens with Globus and remove them.

        :param args: parsed arguments, unused
        """
        self.app.logout()
        print(f"Logged out; tokens revoked and removed from {self.token_file}")

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
        lo.add_argument("--force", action="store_true", help="re-run the login flow")
        lo.set_defaults(func=self.cmd_login)

        sub.add_parser("logout", help="revoke and remove stored tokens").set_defaults(
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
            seen = self._logins
            try:
                args.func(args)
            except globus_sdk.GlobusAPIError as e:
                if not self.relogin(e, seen):
                    raise
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
    """Add the output options shared by most commands.

    :param p: the parser to extend
    """
    p.add_argument(
        "--format",
        choices=("table", "tsv", "csv", "json"),
        default="table",
        help="output format (default: table)",
    )
    p.add_argument("--no-header", action="store_true", help="omit the header row")


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


def _to_gare(error):
    """Parse what an API error needs from a new login, if anything.

    :param error: a Globus API error
    :returns: a GARE, or None if the error is not asking for a login
    """
    try:
        return globus_sdk.gare.to_gare(error)
    except (AttributeError, TypeError, ValueError):
        return None


def _is_gare(error) -> bool:
    """Whether an API error asks for a new login or consent.

    :param error: a Globus API error
    :returns: True for a Globus Authorization Requirements Error
    """
    return _to_gare(error) is not None
