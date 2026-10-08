"""Shared fixtures: a fake Transfer client and a fake collection tree.

Every command reaches Globus through ``transfer_client()``, so the fixtures
here monkeypatch that one function and hand back a stub. Nothing in this
suite touches the network or the real credential store.
"""

import datetime as dt

import globus_sdk
import pytest

from globus_console_cli import common, console, fs

MB = 1000**2
GB = 1000**3

COLLECTION = "cccc1111-2222-3333-4444-555566667777"

#: name -> (type, size) for each directory in the fake collection
TREE = {
    "/data": [
        ("project-a", "dir", 0),
        ("project-b", "dir", 0),
        ("readme.txt", "file", 1024),
    ],
    "/data/project-a": [
        ("sub1", "dir", 0),
        ("file1.bin", "file", 100 * MB),
        ("file2.bin", "file", 2 * GB),
    ],
    "/data/project-a/sub1": [
        ("x.fastq.gz", "file", 500 * MB),
        ("y.fastq.gz", "file", 1500 * MB),
        ("old.log", "file", 10),
    ],
    "/data/project-b": [("big.tar", "file", 100 * GB), ("empty", "dir", 0)],
    "/data/project-b/empty": [],
}

BOOKMARKS = [
    {"id": "bm-0001", "name": "projects", "endpoint_id": COLLECTION, "path": "/data/"},
    {"id": "bm-0002", "name": "root", "endpoint_id": COLLECTION, "path": "/"},
]


class FakeAPIError(globus_sdk.TransferAPIError):
    """A TransferAPIError built without the SDK's response-backed constructor.

    Must really subclass the SDK error: the walk only treats
    ``TransferAPIError`` as a listing failure, and anything else propagates.
    ``message`` and ``info`` are read-only properties upstream, so both are
    overridden rather than assigned.
    """

    def __init__(self, http_status, code, message, raw_json=None):  # noqa: B042
        """Record the three attributes the tool reads off an API error.

        ``super().__init__()`` is deliberately bypassed: TransferAPIError's
        constructor wants a real HTTP response to parse, which is exactly
        what a stub does not have. Every argument still reaches
        ``Exception.__init__``, so pickle and copy behave.

        :param http_status: the HTTP status the API returned
        :param code: the Globus error code
        :param message: the human-readable message
        :param raw_json: the response body, e.g. one carrying
            ``authorization_parameters`` for a login demand
        """
        Exception.__init__(self, http_status, code, message, raw_json)
        self.http_status = http_status
        self.code = code
        self._message = message
        self._raw_json = raw_json or {"code": code, "message": message}

    def __str__(self):
        """Render as the message alone.

        :returns: the human-readable error message
        """
        return self._message

    @property
    def message(self):
        """The human-readable error message.

        :returns: the message this error was built with
        """
        return self._message

    @property
    def raw_json(self):
        """The response body, as the SDK's GARE parser reads it.

        :returns: the JSON document this error was built with
        """
        return self._raw_json

    @property
    def errors(self):
        """Sub-errors; the stub has none.

        :returns: an empty list
        """
        return []

    @property
    def info(self):
        """Error info container; nothing here needs a consent hint.

        :returns: None
        """
        return None


class FakeTransferClient:
    """A Transfer client serving :data:`TREE` and :data:`BOOKMARKS`."""

    def __init__(self, tasks=None, unlistable=()):
        """Build a stub client.

        :param tasks: task documents to return from the admin task list
        :param unlistable: paths that should raise PermissionDenied
        """
        self.tasks = tasks or []
        self.unlistable = set(unlistable)
        self.ls_calls = []
        self.created = []
        self.paginated = _Paginated(self)

    def operation_ls(self, collection, path=None, **kwargs):
        """List one directory of the fake tree.

        :param collection: the collection id, recorded but unused
        :param path: the directory to list
        :param kwargs: accepted and ignored
        :returns: the entries for that directory
        :raises FakeAPIError: if the path is marked unlistable or absent
        """
        self.ls_calls.append(path)
        if path in self.unlistable:
            raise FakeAPIError(403, "PermissionDenied", f"denied: {path}")
        key = path if path in TREE else (path.rstrip("/") or "/")
        if key not in TREE:
            raise FakeAPIError(404, "ClientError.NotFound", f"no such path {path}")
        # "dirlink"/"filelink" model what Globus returns for a symlink: the
        # target's type, plus a link_target
        return [
            {
                "name": name,
                "type": kind.replace("link", ""),
                "size": size,
                "last_modified": "2026-09-01 12:00:00+00:00",
                "permissions": "0755" if kind == "dir" else "0644",
                "user": "brockp",
                "group": "hpcstaff",
                "link_target": "/elsewhere" if kind.endswith("link") else None,
            }
            for name, kind, size in TREE[key]
        ]

    def bookmark_list(self):
        """Return the fake bookmarks.

        :returns: a copy of :data:`BOOKMARKS`
        """
        return [dict(b) for b in BOOKMARKS]

    def create_bookmark(self, data):
        """Record a bookmark creation.

        :param data: the bookmark document
        :returns: the document with an id attached
        """
        self.created.append(data)
        return dict(data, id="bm-new")

    def update_bookmark(self, bookmark_id, data):
        """Pretend to rename a bookmark.

        :param bookmark_id: the bookmark to update
        :param data: the fields to change
        :returns: the merged document
        """
        return {"id": bookmark_id, **data}

    def delete_bookmark(self, bookmark_id):
        """Pretend to delete a bookmark.

        :param bookmark_id: the bookmark to remove
        :returns: the deleted id
        """
        return {"id": bookmark_id}


class _Paginated:
    """The ``paginated`` namespace of a Transfer client."""

    def __init__(self, client):
        """Bind to the client whose tasks should be served.

        :param client: the owning :class:`FakeTransferClient`
        """
        self.client = client
        self.last_kwargs = {}
        self.calls = []

    def endpoint_manager_task_list(self, **kwargs):
        """Serve the admin task list.

        :param kwargs: recorded so tests can assert on the server-side filters
        :returns: one page of task documents
        """
        self.last_kwargs = kwargs
        self.calls.append("endpoint_manager_task_list")
        return [self.client.tasks]

    def task_list(self, **kwargs):
        """Serve the user's own task list.

        :param kwargs: recorded so tests can assert on the filter clauses
        :returns: one page of task documents
        """
        self.last_kwargs = kwargs
        self.calls.append("task_list")
        return [self.client.tasks]


def make_task(index, **overrides):
    """Build a task document with sensible defaults.

    :param index: seeds the id, owner and counters so tasks differ
    :param overrides: fields to replace on the generated document
    :returns: a task document
    """
    now = dt.datetime.now(dt.timezone.utc)
    task = {
        "task_id": f"{index:08x}-1111-2222-3333-444455550000",
        "type": "TRANSFER",
        "status": "ACTIVE",
        "is_paused": False,
        "owner_string": f"user{index}@umich.edu",
        "label": f"transfer {index}",
        "source_endpoint_display_name": "Great Lakes scratch",
        "destination_endpoint_display_name": "Turbo Research Storage",
        "source_endpoint_id": "ssss1111-0000-0000-0000-000000000001",
        "destination_endpoint_id": "dddd2222-0000-0000-0000-000000000002",
        "files": 1200 * index,
        "files_transferred": 300 * index,
        "files_skipped": 0,
        "bytes_transferred": GB * index,
        "effective_bytes_per_second": 100 * MB * index,
        "faults": 0,
        "nice_status": "OK",
        "request_time": (now - dt.timedelta(minutes=30 * index)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        ),
        "completion_time": None,
    }
    task.update(overrides)
    return task


@pytest.fixture
def client():
    """A Transfer client stub with no tasks.

    :returns: a :class:`FakeTransferClient`
    """
    return FakeTransferClient()


def _runner(monkeypatch, main):
    """Build a CLI runner that serves a stub Transfer client.

    :param monkeypatch: pytest's monkeypatch fixture
    :param main: the tool's ``main`` function
    :returns: a callable taking ``(argv, client)`` and returning the client
    """

    def _run(argv, transfer=None):
        """Invoke the CLI with a stubbed Transfer client.

        :param argv: the argument list, without the program name
        :param transfer: the client to serve, or None for an empty one
        :returns: the client that was used
        """
        transfer = transfer or FakeTransferClient()
        monkeypatch.setattr(common.Tool, "transfer_client", lambda self, args: transfer)
        main(argv)
        return transfer

    return _run


@pytest.fixture
def run(monkeypatch):
    """Run globus-fs against a stub client.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: a callable taking ``(argv, client)`` and returning the client
    """
    return _runner(monkeypatch, fs.main)


@pytest.fixture
def run_console(monkeypatch):
    """Run globus-console against a stub client.

    :param monkeypatch: pytest's monkeypatch fixture
    :returns: a callable taking ``(argv, client)`` and returning the client
    """
    return _runner(monkeypatch, console.main)


def ha_session_error(domain="umich.edu"):
    """The error a high assurance collection returns for a wrong session.

    Shaped exactly like the real one from a UMich sensitive collection: a
    502, not a 403, carrying the login requirements.

    :param domain: the identity domain the collection's policy requires
    :returns: a :class:`FakeAPIError`
    """
    return FakeAPIError(
        502,
        "ExternalError.DirListingFailed.LoginFailed",
        "Login Failed",
        raw_json={
            "code": "ExternalError.DirListingFailed.LoginFailed",
            "message": "Login Failed",
            "authorization_parameters": {
                "session_message": "Session reauthentication required",
                "session_required_identities": [],
                "session_required_mfa": False,
                "session_required_single_domain": [domain],
            },
        },
    )
