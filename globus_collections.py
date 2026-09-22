"""Site-wide collection shortcuts: friendly names instead of UUIDs.

Most people only ever touch one or two collections, and nobody should have
to look up a UUID to get at them. An administrator ships a file of
``name = uuid`` pairs -- from ``/etc``, or from an Lmod module via
``GLOBUS_ACTIVITY_COLLECTIONS`` -- and everyone gets ``dataden:/path``
without configuring anything.

These sit alongside, not instead of, a user's own Globus bookmarks. A
bookmark of the same name wins, because it is that person's explicit choice.

File format, deliberately dull because administrators hand-edit it::

    # ARC storage services
    dataden  = ab65757f-00f5-4e5b-aa21-133187732a01
    turbo    = 8e3a1e59-0000-0000-0000-000000000000
    scratch  = 1a2b3c4d-0000-0000-0000-000000000000:/scratch

``name: uuid`` and bare ``name uuid`` are accepted too, ``#`` starts a
comment, and a file beginning with ``{`` is parsed as a JSON object of the
same pairs. A name may carry an optional ``:/base/path``; paths given on the
command line are resolved beneath it, exactly as they are for a bookmark.
"""

from __future__ import annotations

import json
import os
import re

__all__ = [
    "DEFAULT_SITE_FILE",
    "describe_sources",
    "Shortcut",
    "load",
    "parse",
    "search_paths",
    "valid_name",
]

#: Shipped by an administrator for everyone on the system.
DEFAULT_SITE_FILE = "/etc/globus-activity/collections.conf"

#: Colon separated list of extra files, for Lmod and friends.
ENV_VAR = "GLOBUS_ACTIVITY_COLLECTIONS"

#: Names go on a command line, so no spaces and no colons.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

_LINE_RE = re.compile(r"^\s*(?P<name>[^\s:=#]+)\s*[:=\s]\s*(?P<value>\S+)\s*$")


class Shortcut:
    """One named collection, and optionally a base path within it."""

    __slots__ = ("name", "collection", "path", "source")

    def __init__(self, name: str, collection: str, path: str, source: str) -> None:
        """Record a shortcut and where it came from.

        :param name: the friendly name typed on the command line
        :param collection: the collection UUID
        :param path: the base path within the collection, ``/`` if unset
        :param source: the file this was read from, for the listing
        """
        self.name = name
        self.collection = collection
        self.path = path or "/"
        self.source = source

    def __repr__(self) -> str:
        """Render as ``name -> uuid:path``.

        :returns: a short debug representation
        """
        return f"<{self.name} -> {self.collection}:{self.path}>"


def valid_name(name: str) -> bool:
    """Check that a name is usable as a command line target.

    :param name: the candidate shortcut name
    :returns: True if it has no spaces, colons or leading punctuation
    """
    return bool(NAME_RE.match(name))


def _split_value(value: str) -> tuple[str, str]:
    """Split ``uuid`` or ``uuid:/path`` into its parts.

    :param value: the right hand side of a shortcut definition
    :returns: ``(collection, path)``, with ``/`` when no path is given
    """
    collection, sep, path = value.partition(":")
    if not sep or not path:
        return collection.strip(), "/"
    path = path.strip()
    return collection.strip(), path if path.startswith("/") else "/" + path


def parse(text: str, source: str = "<string>") -> dict[str, Shortcut]:
    """Parse a shortcuts file.

    Malformed lines are skipped rather than fatal: one bad line in a
    site-wide file should not break the tool for everyone on the machine.

    :param text: the file contents
    :param source: where it came from, recorded on each shortcut
    :returns: mapping of name to :class:`Shortcut`
    """
    shortcuts: dict[str, Shortcut] = {}
    stripped = text.lstrip()

    if stripped.startswith("{"):
        try:
            loaded = json.loads(stripped)
        except ValueError:
            return shortcuts
        items = loaded.items() if isinstance(loaded, dict) else []
        for name, value in items:
            if valid_name(str(name)) and isinstance(value, str):
                collection, path = _split_value(value)
                shortcuts[str(name)] = Shortcut(str(name), collection, path, source)
        return shortcuts

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if not valid_name(name):
            continue
        collection, path = _split_value(match.group("value"))
        if collection:
            shortcuts[name] = Shortcut(name, collection, path, source)
    return shortcuts


def search_paths(user_file: str | None = None) -> list[str]:
    """List the shortcut files to read, in increasing precedence.

    The site file comes first so that anything an administrator ships can be
    overridden: by a module-provided file, then by the user's own.

    :param user_file: the per-user file, typically in the config directory
    :returns: candidate paths, lowest precedence first
    """
    paths = [DEFAULT_SITE_FILE]
    paths += [p for p in os.environ.get(ENV_VAR, "").split(os.pathsep) if p]
    if user_file:
        paths.append(user_file)
    return paths


def describe_sources(user_file: str | None = None) -> list[tuple[str, str]]:
    """List the shortcut sources for display, with what each one is.

    Includes the environment variable even when it is unset, so the reader
    can see the mechanism exists rather than wondering why a file they set
    is being ignored.

    :param user_file: the per-user file, typically in the config directory
    :returns: ``(location, description)`` pairs, lowest precedence first
    """
    rows = [(DEFAULT_SITE_FILE, "site-wide, shipped by an administrator")]
    env_value = os.environ.get(ENV_VAR, "")
    if env_value:
        for path in env_value.split(os.pathsep):
            if path:
                rows.append((path, f"from ${ENV_VAR}"))
    else:
        rows.append((f"${ENV_VAR}", "not set"))
    if user_file:
        rows.append((user_file, "yours"))
    return rows


def load(user_file: str | None = None) -> dict[str, Shortcut]:
    """Read every shortcut file that exists and merge them.

    :param user_file: the per-user file, typically in the config directory
    :returns: mapping of name to :class:`Shortcut`, later files winning
    """
    merged: dict[str, Shortcut] = {}
    for path in search_paths(user_file):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        merged.update(parse(text, source=path))
    return merged
