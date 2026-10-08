"""globus-fs - ls, find and du for storage you can only reach through Globus.

For everyone. Nothing here needs an administrator role: listings use the
caller's own permissions on each collection, bookmarks are the caller's
own, and the task commands read the caller's own transfers.

Names instead of UUIDs come from two places: the user's Globus bookmarks
(shared with globus-cli and the web app) and site-wide collection
shortcuts an administrator ships (see :mod:`.shortcuts`).
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import posixpath
import shlex
import shutil
import sys
import time

import globus_sdk
import humanfriendly

from . import shortcuts as globus_collections
from . import tasks
from . import walk as globus_fs
from .common import Tool, add_common, human_bytes, is_uuid, new_parser, parse_time

TOOL = Tool("globus-fs")

_BOOKMARK_CACHE = None


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
            TOOL.api_error(e)
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


def user_shortcut_file():
    """A user's own shortcuts, beside their tokens.

    ``~/.globus/app/<client-id>/globus-fs/collections.conf``: one per-user
    directory for everything, the way ``~/.ssh`` holds both keys and config.

    :returns: the path, whether or not the file exists
    """
    return os.path.join(TOOL.config_dir, "collections.conf")


def site_shortcuts():
    """Load the merged site and user collection shortcuts.

    :returns: mapping of name to :class:`globus_collections.Shortcut`
    """
    return globus_collections.load(user_shortcut_file())


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

    if is_uuid(head):
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
            "(globus-fs collections)"
        )

    names = sorted(n for n in (b.get("name") for b in bookmark_index(tc)) if n)
    if names:
        lines.append("Your bookmarks:       " + ", ".join(names))
    else:
        lines.append(
            "Your bookmarks:       none yet "
            "(globus-fs bookmark create <uuid>:/ <name>)"
        )

    lines.append("")
    lines.append("A collection UUID always works:  globus-fs du <uuid>:/path")
    return "\n".join(lines)


def _walk_or_explain(args, tc, collection, path, maxdepth=None, skip=()):
    """Walk, turning a ConsentRequired on the first listing into advice.

    :param args: parsed arguments, read for the walk options
    :param tc: an authenticated Transfer client
    :param collection: the collection UUID
    :param path: the directory to start from
    :param maxdepth: how many levels to descend, or None for all
    :param skip: directory names never descended into
    :returns: an iterator of ``(path, depth, entries)``
    """
    walker = globus_fs.Walker(
        tc,
        collection,
        jobs=args.jobs,
        show_hidden=not args.no_hidden,
        local_user=getattr(args, "local_user", None),
        progress=sys.stderr.isatty() and args.format == "table",
        skip_names=skip,
        call=args.tool.call,
    )
    produced = False
    for item in walker.walk(path, maxdepth=maxdepth):
        produced = True
        yield item
    walker.clear_progress()
    if not produced and walker.errors:
        first = walker.errors[0]
        if first.needs_login or first.code == "PermissionDenied":
            sys.stderr.write(str(first) + "\n")
            if first.needs_login:
                # on a terminal the login happens in place, so this only
                # happens without one
                sys.stderr.write(
                    "\nThis collection needs you to log in (consent, or a high "
                    "assurance session). Run the same command in an interactive "
                    "terminal and it will ask you to.\n"
                )
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


#: Every character ``str.splitlines()`` breaks on. globus-cli splits
#: ``--batch`` input with it, so a name holding one cannot be written as a
#: single line and is left out rather than risk deleting the wrong path.
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


def batch_line(path):
    """Quote one absolute path for ``globus delete --batch`` input.

    globus-cli reads each line with ``shlex.split(line, comments=True)``, so
    shell quoting round-trips spaces, quotes and ``#`` exactly. Paths are
    always absolute, which globus-cli leaves untouched by any command-line
    prefix, and which can never be mistaken for an option.

    :param path: an absolute path on the collection
    :returns: the quoted line, or None if the path cannot be represented
    """
    if any(c in _LINE_BREAKS for c in path):
        return None
    return shlex.quote(path)


def batch_header(target, collection, type_filter):
    """The comment block that opens ``find --batch`` output.

    globus-cli skips ``#`` lines, so a saved file carries the collection
    and the command to submit it, and can be reviewed before anything is
    deleted.

    :param target: the target as the user typed it
    :param collection: the resolved collection UUID
    :param type_filter: the ``--type`` value, ``"f"``, ``"d"`` or None
    :returns: the header, newline terminated
    """
    lines = [
        f"# Paths from:  globus-fs find {target}",
        f"# Collection:  {collection}",
        "# Review, then submit with:",
        f"#   globus delete --no-enable-globs --batch FILE {collection}",
    ]
    if type_filter != "f":
        lines.append(
            "# Directories listed here fail without --recursive, which deletes"
        )
        lines.append("# everything beneath them. Use --type f to list files only.")
    return "\n".join(lines) + "\n"


def _write_match(args, full, entry, is_dir, end):
    """Write one find match in the requested output form.

    :param args: parsed arguments, read for ``--long`` and ``--batch``
    :param full: the match's absolute path
    :param entry: its listing entry
    :param is_dir: whether it is a directory
    :param end: the record terminator
    :returns: False if the path could not be written, else True
    """
    if args.batch:
        line = batch_line(full)
        if line is None:
            print(
                f"skipped, contains a line break: {full!r}",
                file=sys.stderr,
            )
            return False
        sys.stdout.write(line + "\n")
    elif args.long:
        mtime = (entry.get("last_modified") or "")[:19]
        kind = "d" if is_dir else "-"
        sys.stdout.write(
            f"{kind} {human_bytes(entry.get('size') or 0):>9}  {mtime:19}  "
            f"{full}{end}"
        )
    else:
        sys.stdout.write(full + end)
    return True


def cmd_find(args):
    """Search a collection recursively and print matching paths.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    collection, root = _resolve_target(args, tc)
    min_size = globus_fs.parse_size(args.min_size) if args.min_size else None
    max_size = globus_fs.parse_size(args.max_size) if args.max_size else None
    newer = globus_fs.parse_age(args.newer) if args.newer else None
    older = globus_fs.parse_age(args.older) if args.older else None

    if args.batch and (args.long or args.print0):
        sys.exit("--batch cannot be combined with --long or --print0")
    end = "\0" if args.print0 else "\n"
    count = 0
    total = 0
    skipped = 0
    if args.batch:
        sys.stdout.write(batch_header(args.target, collection, args.type))
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
            if not _write_match(args, full, e, is_dir, end):
                skipped += 1
                continue
            count += 1
            total += e.get("size") or 0
            if args.limit and count >= args.limit:
                sys.stdout.flush()
                _report_walk_errors(args)
                return
    sys.stdout.flush()
    if args.summary:
        print(f"\n{count} match(es), {human_bytes(total)}", file=sys.stderr)
    if skipped:
        print(
            f"{skipped} path(s) with line breaks in the name were left out",
            file=sys.stderr,
        )
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
    tc = args.tool.transfer_client(args)
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
        entries = list(args.tool.call(tc.operation_ls, collection, **kwargs))
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)
    _ls_emit(args, [(e, _ls_name(e, args)) for e in _ls_sort(entries, args)])


def cmd_du(args):
    """Report cumulative size and file counts per directory.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
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


#: Environment variable overriding the archivescan size threshold.
ARCHIVESCAN_SIZE_VAR = "GLOBUS_FS_ARCHIVESCAN_SIZE"

#: Files larger than this are Data Den candidates. 100 MiB, the same
#: threshold archivetar's archivescan uses.
ARCHIVESCAN_DEFAULT_SIZE = "100M"

#: Snapshot directories hold another full copy of the tree; walking them
#: would multiply the counts, and they are never migrated anyway.
ARCHIVESCAN_SKIP = (".snapshot",)


def archivescan_threshold(text):
    """Parse a size threshold the way archivetar does.

    Binary units throughout, so ``100M``, ``100MB`` and ``100MiB`` are all
    104,857,600 bytes -- the 100 MiB archivescan has always meant.

    :param text: a size such as ``"100M"``, ``"1.5G"`` or ``"104857600"``
    :returns: the threshold in bytes
    :raises SystemExit: if the size cannot be parsed
    """
    try:
        return humanfriendly.parse_size(text, binary=True)
    except humanfriendly.InvalidSize as exc:
        sys.exit(f"archivescan: bad size {text!r}: {exc}")


def archivescan_tally(entries, dirpath, threshold, pattern):
    """Split one directory's files into Data Den and cache candidates.

    Only regular files count. Symbolic links are skipped, as archivescan
    skips them, since the data they point at is counted where it lives.

    :param entries: the directory's listing entries
    :param dirpath: the directory's absolute path, for ``--filter``
    :param threshold: files larger than this many bytes go to Data Den
    :param pattern: glob matched against each file's full path
    :returns: ``[big_files, big_bytes, small_files, small_bytes]``
    """
    tally = [0, 0, 0, 0]
    for e in entries:
        if e.get("type") != "file" or e.get("link_target"):
            continue
        if pattern != "*" and not fnmatch.fnmatch(
            posixpath.join(dirpath, e.get("name", "")), pattern
        ):
            continue
        size = e.get("size") or 0
        slot = 0 if size > threshold else 2
        tally[slot] += 1
        tally[slot + 1] += size
    return tally


def cmd_archivescan(args):
    """Report how much of a tree is large enough for Data Den.

    A port of archivetar's ``archivescan`` for storage reached only through
    Globus. It reports the "future state" -- where files would land by
    size. The current migrated/resident state needs block counts, which the
    Transfer API does not expose.

    :param args: parsed arguments
    """
    threshold = archivescan_threshold(args.size)
    tc = args.tool.transfer_client(args)
    collection, root = _resolve_target(args, tc)
    start = time.time()
    totals = [0, 0, 0, 0]
    for path, _depth, entries in _walk_or_explain(
        args, tc, collection, root, maxdepth=args.maxdepth, skip=ARCHIVESCAN_SKIP
    ):
        for i, n in enumerate(archivescan_tally(entries, path, threshold, args.filter)):
            totals[i] += n
    elapsed = time.time() - start
    big_files, big_bytes, small_files, small_bytes = totals
    all_bytes = big_bytes + small_bytes
    fraction = 100.0 * big_bytes / all_bytes if all_bytes else None

    row = {
        "path": root,
        "threshold_bytes": threshold,
        "dataden_files": big_files,
        "dataden_bytes": big_bytes,
        "cache_files": small_files,
        "cache_bytes": small_bytes,
        "fraction_offline": None if fraction is None else round(fraction, 5),
        "seconds": round(elapsed, 2),
    }
    if args.format == "json":
        json.dump(row, sys.stdout, indent=2)
        print()
    elif args.format in ("tsv", "csv"):
        # one row per scan, so a loop over many directories builds a table
        writer = csv.writer(
            sys.stdout,
            delimiter="\t" if args.format == "tsv" else ",",
            lineterminator="\n",
        )
        if not args.no_header:
            writer.writerow([k.upper() for k in row])
        writer.writerow(["" if v is None else v for v in row.values()])
    else:
        _print_archivescan(row)
    walker = getattr(args, "_walker", None)
    if args.summary and walker:
        print(f"\n{walker.dirs_scanned} director(ies) scanned", file=sys.stderr)
    _report_walk_errors(args)


def _print_archivescan(row):
    """Print the report in archivetar's archivescan layout.

    :param row: the scan results, as built by :func:`cmd_archivescan`
    """
    size = humanfriendly.format_size
    print("----- Results ------")
    print(f"Threshold: files larger than {size(row['threshold_bytes'], binary=True)}")
    print("")
    print("Data Den Candidates:")
    print(f"Files: {row['dataden_files']}")
    print(f"Size: {size(row['dataden_bytes'], binary=True)}")
    print("")
    print("Cache (Locker) Candidates:")
    print(f"Files: {row['cache_files']}")
    print(f"Size: {size(row['cache_bytes'], binary=True)}")
    print("")
    print(f"Total Time {row['seconds']:.2f} Seconds")
    if row["fraction_offline"] is None:
        print("Fraction Offline: n/a (no files)")
    else:
        print(f"Fraction Offline: {row['fraction_offline']:.5f} %")


def _no_shortcuts_message():
    """Explain that nothing is configured, and what to do about it.

    Deliberately avoids a realistic-looking example: a line such as
    ``dataden = ab65757f-...`` here reads as though that shortcut already
    exists, which is exactly the wrong impression to leave.

    :returns: the message to print
    """
    rows = globus_collections.describe_sources(user_shortcut_file())
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
        "    globus-fs bookmark create <collection-uuid>:/ <name>",
        "",
        "To ship names for everyone on this system, write the site file with",
        "one line per collection, substituting your own values:",
        "",
        f"    # {globus_collections.DEFAULT_SITE_FILE}",
        "    <name> = <collection-uuid>",
        "",
        "Full format:  globus-fs collections --help",
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
                b.get("name") for b in bookmark_index(args.tool.transfer_client(args))
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
    tc = args.tool.transfer_client(args)
    bookmarks = bookmark_index(tc)
    if args.format == "json":
        json.dump(bookmarks, sys.stdout, indent=2, default=str)
        print()
        return
    if not bookmarks:
        print(
            "no bookmarks.  Create one with:\n"
            "    globus-fs bookmark create <collection-uuid>:/path/ NAME"
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
    tc = args.tool.transfer_client(args)
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
    tc = args.tool.transfer_client(args)
    head, sep, path = args.endpoint_plus_path.partition(":")
    if not sep or not is_uuid(head.strip()):
        sys.exit(
            "expected COLLECTION_UUID:/path/ as the first argument, "
            "e.g.  globus-fs bookmark create "
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
        args.tool.api_error(e)
    print(
        f"{res.get('name', args.bookmark_name)}  ->  "
        f"{res.get('endpoint_id', head)}:{res.get('path', path)}"
    )


def cmd_bookmark_rename(args):
    """Rename a bookmark.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    bm = resolve_bookmark(tc, args.bookmark)
    if bm is None:
        sys.exit(f"no bookmark named '{args.bookmark}'")
    try:
        tc.update_bookmark(bm["id"], {"name": args.new_name})
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)
    print(f"{bm.get('name')} renamed to {args.new_name}")


def cmd_bookmark_delete(args):
    """Delete a bookmark.

    :param args: parsed arguments
    """
    tc = args.tool.transfer_client(args)
    bm = resolve_bookmark(tc, args.bookmark)
    if bm is None:
        sys.exit(f"no bookmark named '{args.bookmark}'")
    try:
        tc.delete_bookmark(bm["id"])
    except globus_sdk.TransferAPIError as e:
        args.tool.api_error(e)
    print(f"deleted {bm.get('name')}")


MAIN_EPILOG = """\
targets (for ls, find, du)
  UUID:/path              a collection UUID and an absolute path
  name:/path              a collection shortcut or bookmark, plus a path
  name                    that shortcut or bookmark's own location
  A path after a name is resolved beneath it. Your own bookmark wins over a
  site shortcut of the same name.

collection shortcuts        (see: globus-fs collections)
  read from, later winning:
    /etc/globus-fs/collections.conf
    $GLOBUS_FS_COLLECTIONS      colon separated files, for Lmod
    $GLOBUS_FS_SHORTCUTS        the shortcuts themselves, ; separated
    ~/.globus/app/<client-id>/globus-fs/collections.conf
  format:
    NAME = COLLECTION_UUID[:/base/path]       # also NAME: UUID, or NAME UUID

your transfers
  tasks, watch, history and task show transfers you submitted. Collection
  administrators wanting everyone's use globus-console.

Full reference, with examples: USAGE.md
"""

COLLECTIONS_EPILOG = """\
Shortcuts let people type a name instead of a collection UUID. An
administrator ships them for everyone; a user can add their own.

read from, later winning:
  /etc/globus-fs/collections.conf     everyone on this machine
  $GLOBUS_FS_COLLECTIONS              colon separated files, for Lmod
  $GLOBUS_FS_SHORTCUTS                the shortcuts themselves, for Lmod:
                                        dataden=UUID;scratch=UUID:/scratch
  ~/.globus/app/<client-id>/globus-fs/collections.conf
                                            your own additions, beside your tokens

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
    """Build the globus-fs command line parser.

    :returns: the configured top-level parser
    """
    p = new_parser(
        "globus-fs",
        "ls, find and du for Globus collections, plus your own transfers.",
        MAIN_EPILOG,
    )
    sub = p.add_subparsers(dest="cmd")

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
    f.add_argument(
        "--batch",
        action="store_true",
        help="quoted paths for 'globus delete --batch', with a header "
        "naming the collection",
    )
    f.add_argument("--limit", type=int, default=0, help="stop after N matches")
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

    size_default = os.environ.get(ARCHIVESCAN_SIZE_VAR) or ARCHIVESCAN_DEFAULT_SIZE
    a = sub.add_parser(
        "archivescan",
        help="how Data Den friendly a tree is: files over and under a size",
    )
    add_walk_opts(a)
    a.add_argument(
        "-s",
        "--size",
        default=size_default,
        metavar="SIZE",
        help="files larger than this are Data Den candidates, e.g. 100M, 1G; "
        f"binary units. Default {size_default}, settable with "
        f"{ARCHIVESCAN_SIZE_VAR}",
    )
    a.add_argument(
        "-f",
        "--filter",
        default="*",
        metavar="GLOB",
        help="only count files whose full path matches, e.g. '*.tar'",
    )
    add_common(a)
    a.set_defaults(func=cmd_archivescan)

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

    tasks.add_task_commands(sub, admin=False)
    TOOL.add_auth_commands(sub)
    # Recorded so the runner can tell a command from a bare flag without
    # keeping a second list in step with this one.
    p.command_names = set(sub.choices)
    return p


def main(argv=None):
    """Run globus-fs.

    :param argv: argument list, defaulting to ``sys.argv[1:]``
    :returns: the process exit status
    """
    return TOOL.run(
        build_parser(),
        sys.argv[1:] if argv is None else argv,
        preprocess=tasks.glue_negative_values,
    )


if __name__ == "__main__":
    sys.exit(main())
