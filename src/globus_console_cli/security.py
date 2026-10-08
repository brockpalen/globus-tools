"""Protect the token file, the same way archivetar does.

The token file holds a Globus **refresh token**: a long-lived credential
that can be exchanged for access tokens until it is revoked. On a shared
login node, a token file other users can read lets them act as its owner.

The secret is the file, not the directory. globus-sdk keeps it under
``~/.globus/app/<client-id>/<app-name>/tokens.json``, and ``~/.globus`` is
shared with globus-cli (which creates it 775) and archivetar, so its
permissions are not ours to enforce. The file itself must be 600:

- globus-sdk creates it under a user-only umask, so a new file is 600.
- A file that already exists keeps whatever mode it had, so a loose one is
  tightened to 600 with a warning rather than used as is -- archivetar's
  rule (brockpalen/archivetar#49).
- A file someone else owns cannot be tightened, and is refused.
"""

from __future__ import annotations

import os
import stat

__all__ = [
    "UnsafeTokenFile",
    "check_token_file",
    "ensure_globus_dir",
    "private_directory",
]


class UnsafeTokenFile(Exception):
    """A token file that cannot be made private to this user."""


def ensure_globus_dir(path: str | None = None) -> None:
    """Create ``~/.globus`` as 700 if it does not exist yet.

    An existing directory is left alone: globus-cli creates it 775, and
    refusing to run because of that would block anyone who used the CLI
    first (archivetar#49). It is shared with globus-cli and archivetar.

    :param path: the directory; ``~/.globus``, resolved now, by default
    """
    path = path or os.path.expanduser("~/.globus")
    if not os.path.lexists(path):
        os.makedirs(path, mode=0o700, exist_ok=True)


def check_token_file(path: str) -> str | None:
    """Make sure a token file is readable by its owner only.

    :param path: the token file; it need not exist yet
    :returns: a warning to show if the mode had to be tightened, else None
    :raises UnsafeTokenFile: if the file belongs to another user, so its
        mode cannot be fixed and it must not be trusted
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None  # the SDK will create it 600 at login
    if st.st_uid != os.geteuid():
        raise UnsafeTokenFile(
            f"{path} is owned by uid {st.st_uid}, not you (uid {os.geteuid()}).\n"
            "Refusing to use another user's Globus tokens. Remove it and log in "
            "again."
        )
    if not st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return None
    os.chmod(path, 0o600)
    return (
        f"Permissions {stat.filemode(st.st_mode)} for {path} were too open, "
        "changed to -rw-------.\n"
        "If other users could have copied it, revoke this app's access at "
        "https://app.globus.org/settings/consents and log in again."
    )


def private_directory(path: str) -> None:
    """Make a directory only this tool uses private (700).

    ``~/.globus/app/<client-id>/<tool>`` holds the token file and nothing
    another program needs, so unlike the shared ``~/.globus`` it can be
    locked down: nobody else can create a file -- or a symlink in place of
    the token file -- inside it.

    :param path: the directory, which the SDK has just created
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)
