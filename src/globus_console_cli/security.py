"""Permission checks for the on-disk credential store.

``~/.globus-fs/tokens.json`` (and ``~/.globus-console/tokens.json``) holds a Globus **refresh token**: a
long-lived credential that can be exchanged for access tokens indefinitely
until it is revoked.  On a shared login node or DTN, a world-readable token
file lets any local user act as its owner -- and for an activity monitor or
administrator that means reading every transfer on the subscription.

The globus-sdk writes new token files under a user-only umask, so a fresh
install is safe.  It does not repair a file that already exists with looser
permissions, and ``os.makedirs(..., exist_ok=True)`` silently ignores its
``mode`` argument when the directory is already there.  Either can leave a
credential exposed without anything ever complaining.

So this module checks, and refuses to run rather than using an exposed
credential -- the same stance ``ssh`` takes over private keys.
"""

from __future__ import annotations

import os
import stat

__all__ = [
    "InsecureCredentialStore",
    "assert_secure",
    "check_path",
    "format_mode",
    "harden",
    "secure_directory",
]

#: Permission bits that must not be set on a credential file or its
#: directory: any access at all for group or other.
FORBIDDEN_BITS = 0o077

DIR_MODE = 0o700
FILE_MODE = 0o600


class InsecureCredentialStore(Exception):
    """Raised when a credential path is readable or writable by other users.

    :ivar path: the offending path
    :ivar reason: a short machine-ish description of what is wrong
    :ivar detail: the human-readable explanation shown to the user
    :ivar fixable: whether a ``chmod`` by the current user would resolve it
    """

    def __init__(
        self, path: str, reason: str, detail: str, fixable: bool = False
    ) -> None:
        """Record the failing path and why it failed.

        :param path: the path that failed inspection
        :param reason: short slug, e.g. ``"mode"`` or ``"owner"``
        :param detail: the sentence shown to the user under the banner
        :param fixable: True when ``chmod`` by this user would fix it
        """
        # every argument is forwarded so the exception survives pickle and
        # copy.copy(); __str__ keeps the readable form
        super().__init__(path, reason, detail, fixable)
        self.path = path
        self.reason = reason
        self.detail = detail
        self.fixable = fixable

    def __str__(self) -> str:
        """Render the exception as its human-readable detail.

        :returns: the explanation shown to the user
        """
        return self.detail

    def banner(self, command: str = "globus-fs") -> str:
        """Render an ``ssh``-style refusal suitable for stderr.

        :param command: the tool the user ran, named in the repair hint
        :returns: a multi-line message ending with the command that fixes it
        """
        width = 68
        at = "@" * width
        title = "WARNING: UNPROTECTED GLOBUS CREDENTIAL FILE!"
        lines = [
            at,
            "@" + title.center(width - 2) + "@",
            at,
            self.detail,
            "",
            "This file holds a Globus refresh token, which grants access to your",
            "Globus account until it is revoked. It must not be accessible by",
            "other users on this system.",
            "",
            "Refusing to run.",
        ]
        if self.fixable:
            mode = FILE_MODE if os.path.isfile(self.path) else DIR_MODE
            lines += [
                "",
                "Fix it with:",
                f"    chmod {mode:04o} {self.path}",
                "",
                "or let this tool repair what it owns:",
                f"    {command} login --fix-permissions",
            ]
        return "\n".join(lines)


def format_mode(mode: int) -> str:
    """Format a stat mode as four octal digits.

    :param mode: a raw ``st_mode`` value
    :returns: the permission bits, e.g. ``"0644"``
    """
    return f"{stat.S_IMODE(mode):04o}"


def check_path(path: str, must_be_dir: bool = False) -> None:
    """Verify that one credential path is private to the current user.

    Checks, in order: that the path is not a symlink (which could redirect a
    write, or point at a file another user controls), that it is the expected
    type, that the current effective uid owns it, and that no group or other
    permission bits are set.

    A missing path is not an error -- the caller creates it, and
    :func:`secure_directory` creates it with the right mode.

    :param path: the file or directory to inspect
    :param must_be_dir: True to require a directory, False to require a file
    :raises InsecureCredentialStore: if the path fails any check
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise InsecureCredentialStore(
            path, "stat", f"Cannot inspect {path!r}: {exc.strerror}."
        ) from exc

    if stat.S_ISLNK(info.st_mode):
        raise InsecureCredentialStore(
            path,
            "symlink",
            f"{path!r} is a symbolic link. Credential paths must be real "
            "files, so that neither the contents nor the destination can be "
            "controlled by someone else.",
        )

    if must_be_dir and not stat.S_ISDIR(info.st_mode):
        raise InsecureCredentialStore(
            path, "type", f"{path!r} exists but is not a directory."
        )
    if not must_be_dir and not stat.S_ISREG(info.st_mode):
        raise InsecureCredentialStore(
            path, "type", f"{path!r} exists but is not a regular file."
        )

    euid = os.geteuid()
    if info.st_uid != euid:
        raise InsecureCredentialStore(
            path,
            "owner",
            f"{path!r} is owned by uid {info.st_uid}, but this process runs "
            f"as uid {euid}. Refusing to read or write another user's "
            "credentials.",
        )

    if stat.S_IMODE(info.st_mode) & FORBIDDEN_BITS:
        kind = "directory" if must_be_dir else "file"
        raise InsecureCredentialStore(
            path,
            "mode",
            f"Permissions {format_mode(info.st_mode)} for {path!r} are too "
            f"open. It is required that your credential {kind} is NOT "
            "accessible by other users.",
            fixable=True,
        )


def harden(path: str, must_be_dir: bool = False) -> bool:
    """Tighten a path's permissions to owner-only.

    Only ever removes access. Ownership is never changed, so a path owned by
    another user still fails :func:`check_path` afterwards -- as it should.

    :param path: the file or directory to chmod
    :param must_be_dir: True to apply the directory mode, False for the file mode
    :returns: True if the mode was changed, False if it was already correct
              or the path does not exist
    """
    wanted = DIR_MODE if must_be_dir else FILE_MODE
    try:
        current = stat.S_IMODE(os.lstat(path).st_mode)
    except OSError:
        return False
    if current == wanted:
        return False
    try:
        os.chmod(path, wanted)
    except OSError:
        return False
    return True


def secure_directory(path: str, fix: bool = False) -> None:
    """Create the credential directory if needed, and enforce its mode.

    ``os.makedirs(..., exist_ok=True)`` ignores ``mode`` for a directory that
    already exists, so the mode is applied explicitly.

    With ``fix`` the mode is tightened *before* the check, so that
    ``--fix-permissions`` can repair a directory that would otherwise be
    refused. Without it, an exposed directory raises, as ssh would.

    :param path: the credential directory, e.g. ``~/.globus-fs``
    :param fix: True to chmod an existing directory rather than refuse it
    :raises InsecureCredentialStore: if the directory is exposed and ``fix``
        is False, or is owned by someone else, or is a symlink
    """
    if not os.path.lexists(path):
        os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    elif fix:
        harden(path, must_be_dir=True)
    check_path(path, must_be_dir=True)
    harden(path, must_be_dir=True)


def assert_secure(config_dir: str, *files: str, fix: bool = False) -> list[str]:
    """Check the whole credential store before any token is read or written.

    :param config_dir: the directory holding the credential files
    :param files: paths within it that must be private, e.g. the token file
    :param fix: True to chmod paths this user owns instead of refusing
    :returns: the paths whose permissions were repaired (empty unless ``fix``)
    :raises InsecureCredentialStore: if anything is still exposed
    """
    repaired: list[str] = []

    if fix:
        if harden(config_dir, must_be_dir=True):
            repaired.append(config_dir)
        for path in files:
            if harden(path):
                repaired.append(path)

    check_path(config_dir, must_be_dir=True)
    for path in files:
        check_path(path)
    return repaired
