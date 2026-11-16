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
    "check_shortcut_file",
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


def check_shortcut_file(path: str, personal: bool) -> tuple[bool, str | None]:
    """Decide whether a collection shortcuts file can be trusted.

    A shortcut decides which collection a name like ``dataden:`` reaches,
    so whoever can change the file can send someone's ls, mkdir and mv to
    a collection of their choosing. The rule depends on whose file it is:

    - **personal** (``~/.globus/.../collections.conf``): the token file's
      rule. Another user's file is ignored; group or other write access is
      removed, with a warning.
    - **shared** (``/etc/globus-fs``, ``$GLOBUS_FS_COLLECTIONS``): a lab's
      file reached through its own module may be group-writable -- whoever
      can edit that module can already change its users' PATH -- but a
      file anyone can write is ignored.

    Either way, a file in a directory anyone can write, without the sticky
    bit, could be replaced, and is ignored.

    :param path: the shortcuts file; it need not exist
    :param personal: True for the user's own file
    :returns: ``(usable, warning)``; warning is None if there is nothing to say
    """
    try:
        st = os.stat(path)
        parent = os.stat(os.path.dirname(os.path.abspath(path)))
    except OSError:
        return True, None  # missing or unreadable: nothing is read from it
    mode = stat.filemode(st.st_mode)
    if parent.st_mode & stat.S_IWOTH and not parent.st_mode & stat.S_ISVTX:
        return False, (
            f"ignoring {path}: its directory is writable by anyone, so the "
            "file could be replaced"
        )
    if personal:
        if st.st_uid != os.geteuid():
            return False, (
                f"ignoring {path}: owned by uid {st.st_uid}, not you "
                f"(uid {os.geteuid()}); your shortcuts file must be your own"
            )
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o022)
            return True, (
                f"{path} was writable by others ({mode}); removed group and "
                "other write. Check it holds only the shortcuts you expect."
            )
        return True, None
    if st.st_mode & stat.S_IWOTH:
        return False, (
            f"ignoring {path}: anyone can write it ({mode}), so anyone could "
            "redirect its shortcuts"
        )
    return True, None
