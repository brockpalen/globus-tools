"""Filesystem helpers for collections that are only reachable over Globus.

`find`, `du` and file counts for storage with no shell access.

The Transfer API has no recursive listing: `operation_ls` returns one
directory, so walking a tree costs one API call per directory.  That is the
dominant cost here and it is why these commands run a bounded thread pool
and print progress -- a tree with 50,000 directories is 50,000 round trips
however you slice it.

Listing is metadata only.  On an HSM-backed collection like Data Den that
means `du` and `find` read sizes without recalling anything from tape.
"""

from __future__ import annotations

import concurrent.futures as cf
import posixpath
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import globus_sdk

SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1000,
    "KB": 1000,
    "M": 1000**2,
    "MB": 1000**2,
    "G": 1000**3,
    "GB": 1000**3,
    "T": 1000**4,
    "TB": 1000**4,
    "P": 1000**5,
    "KI": 1024,
    "MI": 1024**2,
    "GI": 1024**3,
    "TI": 1024**4,
    "PI": 1024**5,
}


def parse_size(text):
    """'100M' -> 100000000.  Accepts bare bytes, K/M/G/T, and Ki/Mi/Gi."""
    t = str(text).strip().upper().lstrip("+-")
    for suffix in sorted(SIZE_UNITS, key=len, reverse=True):
        if suffix and t.endswith(suffix):
            num = t[: -len(suffix)].strip()
            try:
                return int(float(num) * SIZE_UNITS[suffix])
            except ValueError:
                raise ValueError(f"cannot parse size {text!r}") from None
    try:
        return int(float(t))
    except ValueError:
        raise ValueError(f"cannot parse size {text!r}") from None


def parse_age(text):
    """'7' or '7d' -> datetime 7 days ago; an ISO date passes through."""
    t = str(text).strip()
    mult = {"D": 1, "H": 1 / 24.0, "W": 7, "M": 30, "Y": 365}
    if t and t[-1].upper() in mult and t[:-1].replace(".", "").isdigit():
        days = float(t[:-1]) * mult[t[-1].upper()]
        return datetime.now(timezone.utc) - timedelta(days=days)
    if t.replace(".", "").isdigit():
        return datetime.now(timezone.utc) - timedelta(days=float(t))
    v = t.replace(" ", "T")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# walking
# ---------------------------------------------------------------------------
class WalkError:
    """One directory that could not be listed, and why."""

    __slots__ = ("path", "code", "message")

    def __init__(self, path, code, message):
        """Record a failed directory listing.

        :param path: the directory that could not be listed
        :param code: the API error code
        :param message: the API error message
        """
        self.path = path
        self.code = code
        self.message = message

    def __str__(self):
        """Render the error as one line.

        :returns: ``path: code: message``
        """
        return f"{self.path}: {self.code}: {self.message}"


class Walker:
    """Concurrent directory walk over one collection.

    Errors on individual directories (permission denied, a dead symlink, a
    collection that went away mid-walk) are collected rather than raised --
    a 40 minute walk should not die on its last directory.
    """

    def __init__(
        self,
        tc,
        collection,
        jobs=8,
        show_hidden=True,
        local_user=None,
        progress=False,
        retries=3,
    ):
        """Record a failed directory listing.

        :param path: the directory that could not be listed
        :param code: the API error code
        :param message: the API error message
        """
        self.tc = tc
        self.collection = collection
        self.jobs = max(1, jobs)
        self.show_hidden = show_hidden
        self.local_user = local_user
        self.progress = progress
        self.retries = retries
        self.errors = []
        self.dirs_scanned = 0
        self.entries_seen = 0
        self._lock = threading.Lock()
        self._t0 = time.time()

    def _ls(self, path):
        """List one directory, retrying transient failures.

        :param path: the directory to list
        :returns: ``(entries, None)`` on success, ``([], WalkError)`` on failure
        """
        kwargs = {"path": path, "show_hidden": self.show_hidden}
        if self.local_user:
            kwargs["local_user"] = self.local_user
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                res = self.tc.operation_ls(self.collection, **kwargs)
                return list(res), None
            except globus_sdk.TransferAPIError as e:
                retryable = e.http_status in (429, 500, 502, 503, 504)
                if retryable and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return [], WalkError(path, e.code, e.message)
            except globus_sdk.GlobusAPIError as e:
                return [], WalkError(path, getattr(e, "code", "Error"), str(e))
        return [], WalkError(path, "Unknown", "exhausted retries")

    def _tick(self, entries):
        """Record progress and refresh the counter on stderr.

        :param entries: how many entries the latest listing returned
        """
        with self._lock:
            self.dirs_scanned += 1
            self.entries_seen += entries
            n, seen = self.dirs_scanned, self.entries_seen
        if self.progress and n % 25 == 0:
            elapsed = time.time() - self._t0
            print(
                f"\r  scanned {n} dirs, {seen} entries, {elapsed:.0f}s...",
                end="",
                file=sys.stderr,
                flush=True,
            )

    def clear_progress(self):
        """Erase the progress counter before other output is written."""
        if self.progress and self.dirs_scanned >= 25:
            print("\r" + " " * 60 + "\r", end="", file=sys.stderr, flush=True)

    def walk(self, root, maxdepth=None):
        """Yield (path, depth, entries) for every directory under root."""
        with cf.ThreadPoolExecutor(max_workers=self.jobs) as pool:
            pending = {pool.submit(self._ls, root): (root, 0)}
            while pending:
                done, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    path, depth = pending.pop(fut)
                    entries, err = fut.result()
                    if err is not None:
                        self.errors.append(err)
                        continue
                    self._tick(len(entries))
                    yield path, depth, entries
                    if maxdepth is not None and depth >= maxdepth:
                        continue
                    for e in entries:
                        if e.get("type") == "dir":
                            child = posixpath.join(path, e["name"])
                            pending[pool.submit(self._ls, child)] = (child, depth + 1)


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------
def needs_consent(err):
    """Test whether an API error is asking for an extra consent.

    :param err: the API error to inspect
    :returns: ``(is_consent_required, required_scopes)``
    """
    info = getattr(err, "info", None)
    cr = getattr(info, "consent_required", None)
    return bool(cr), list(getattr(cr, "required_scopes", []) or [])


def consent_hint(collection, scopes):
    """Explain how to grant a collection's data_access consent.

    :param collection: the collection that refused the listing
    :param scopes: the scopes the server asked for, if it named any
    :returns: a message to print to the user
    """
    return (
        f"\nThis collection ({collection}) needs an additional consent before "
        "it can be listed.\n"
        "Grant it once with:\n\n"
        f"    globus-fs consent {collection}\n\n"
        + (f"(required scopes: {', '.join(scopes)})\n" if scopes else "")
    )
