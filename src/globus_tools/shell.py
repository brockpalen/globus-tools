"""globus-fs shell: cd around a collection and run globus-fs commands.

Not a filesystem. Nothing is mounted and no file is read: every command is
one or more Transfer API calls, the same ones ``globus-fs`` makes, and the
prompt names the collection so nobody mistakes it for local disk. What the
shell adds is a current collection and directory, so paths can be relative,
plus ``|`` and ``>`` for the output.

Output may only be piped into read-only local filters (:data:`FILTERS`):
their input is remote file names, and something like ``ls | xargs rm``
would delete the *local* files that happen to share those names. Each
filter runs without a shell, so nothing in the line is interpreted twice.
``> FILE`` creates a new local file readable only by its owner, since on a
high assurance collection the names themselves are data.
"""

from __future__ import annotations

import cmd
import contextlib
import io
import os
import posixpath
import shlex
import subprocess  # nosec B404 - runs the allow-listed filters, without a shell
import sys
import time

import globus_sdk

from . import fs
from .common import is_uuid

#: What output may be piped into. Each only reads its input, once the
#: checks in :func:`check_filter` rule out the options that write files or
#: run programs.
FILTERS = frozenset(
    "wc grep egrep fgrep sort uniq head tail cut tr column nl less".split()
)

#: Commands whose path argument is resolved against the current directory.
PATH_COMMANDS = frozenset(("ls", "stat", "find", "du", "archivescan"))

#: find's familiar single-dash spellings, mapped to globus-fs options.
FIND_ALIASES = {
    "-name": "--name",
    "-iname": "--iname",
    "-type": "--type",
    "-maxdepth": "--maxdepth",
}

#: Seconds a directory listing is reused for tab completion.
COMPLETION_TTL = 30

BANNER = (
    "globus-fs shell: each command is a Globus API call, not a filesystem.\n"
    "'help' lists commands, 'exit' or Ctrl-D leaves."
)


class ShellError(Exception):
    """A mistake in a command line, reported without ending the shell."""


def split_line(line):
    """Split a line into a command, filters, and an optional redirect.

    Quotes and backslashes protect ``|`` and ``>`` the way a shell's do.
    ``;``, ``&``, ``<`` and backticks are refused rather than passed on as
    text, since a reader would expect them to mean something.

    :param line: the line typed at the prompt
    :returns: ``(command_words, [filter_words, ...], (file, append) or None)``
    :raises ShellError: on a syntax the shell does not support
    """
    stages, current, quote, redirect = [], [], None, None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            current.append(c)
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(line):
                current.append(line[i + 1])
                i += 1
        elif c in "'\"":
            quote = c
            current.append(c)
        elif c == "\\" and i + 1 < len(line):
            current.append(line[i : i + 2])
            i += 1
        elif c == "|":
            stages.append("".join(current))
            current = []
        elif c == ">":
            append = line.startswith(">>", i)
            target = shlex.split(line[i + (2 if append else 1) :])
            if len(target) != 1:
                raise ShellError("> takes one file name, at the end of the line")
            redirect = (target[0], append)
            break
        elif c in ";&<`":
            raise ShellError(
                f"'{c}' is not supported: one command per line, and quote it "
                "if it is part of a name"
            )
        else:
            current.append(c)
        i += 1
    if quote:
        raise ShellError("unterminated quote")
    stages.append("".join(current))
    words = [shlex.split(stage) for stage in stages]
    if any(not w for w in words):
        raise ShellError("nothing on one side of a |")
    return words[0], words[1:], redirect


def check_filter(argv):
    """Refuse anything after ``|`` that could change local files.

    :param argv: one filter's words
    :raises ShellError: if it is not an allowed, read-only filter
    """
    name = argv[0]
    if "/" in name or name not in FILTERS:
        raise ShellError(
            f"'{name}' cannot follow |: only read-only filters can "
            f"({' '.join(sorted(FILTERS))}). Use !command for anything else."
        )
    if name == "sort":
        for arg in argv[1:]:
            short = arg.startswith("-") and not arg.startswith("--")
            if (short and "o" in arg) or arg.startswith(
                ("--output", "--compress-program")
            ):
                raise ShellError("sort may not write files or run programs here")
    files = [a for a in argv[1:] if a == "-" or not a.startswith("-")]
    if name == "uniq" and len(files) > 1:  # uniq IN OUT writes OUT
        raise ShellError("uniq may not write an output file here; use > instead")


def open_redirect(name, append):
    """Open the local file a ``>`` or ``>>`` writes to.

    ``>`` only ever creates a new file, mode 600; ``>>`` appends, creating
    the file the same way if it is missing.

    :param name: the file name, relative to the local working directory
    :param append: True for ``>>``
    :returns: a binary file object
    :raises ShellError: if ``>`` would overwrite, or the file cannot be opened
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    try:
        fd = os.open(os.path.expanduser(name), flags, 0o600)
    except FileExistsError:
        raise ShellError(
            f"{name} exists, and > never overwrites: use >> to append, "
            "or another name"
        ) from None
    except OSError as e:
        raise ShellError(f"{name}: {e.strerror}") from None
    return os.fdopen(fd, "wb")


def normalize(path):
    """Collapse ``.``, ``..`` and repeated slashes in an absolute path.

    :param path: an absolute path on a collection
    :returns: the normalised path; never above ``/``
    """
    path = posixpath.normpath(path)
    return "/" + path.lstrip("/")


class GlobusShell(cmd.Cmd):
    """The interactive loop, with one collection and directory at a time."""

    def __init__(self, tool, start=None):
        """Set up a shell; nothing touches the API until it runs.

        :param tool: the globus-fs :class:`~globus_tools.common.Tool`
        :param start: where to begin, as typed after ``globus-fs shell``
        """
        super().__init__()
        self.tool = tool
        self.start = start
        self.parser = fs.build_parser(in_shell=True)
        self.interactive = sys.stdin.isatty()
        self.here = None  # (collection, path, name it was entered by, or None)
        self.previous = None
        self.home = None
        self.status = 0
        self._tc = None
        self._cache = {}
        self._display_names = {}
        self._set_prompt()

    # ---- state ----------------------------------------------------------
    @property
    def tc(self):
        """The Transfer client, created on first use.

        :returns: an authenticated Transfer client
        """
        if self._tc is None:
            self._tc = self.tool.transfer_client(None)
        return self._tc

    def _set_prompt(self):
        """Show the collection and directory, and nothing when scripted."""
        if not self.interactive:
            self.prompt = ""
        elif self.here is None:
            self.prompt = "(no collection)> "
        else:
            collection, path, entered = self.here
            self.prompt = f"[{self.label(collection, path, entered)}] {path}> "

    def resolve(self, text):
        """Turn what was typed into a collection, a path and how it was named.

        ``name:``, ``name:/path`` and ``UUID:/path`` name a collection; a
        ``name`` that is not a bookmark or shortcut is just a file name with
        a colon in it. ``~`` is the collection's home directory, ``-`` the
        previous directory, and anything else is relative to the current one.

        :param text: the path argument, or None for the current directory
        :returns: ``(collection, path, name)``; name is the bookmark or
            shortcut typed, or None for a UUID
        :raises ShellError: for a relative path before any ``cd``
        """
        if text == "-":
            if self.previous is None:
                raise ShellError("no previous directory")
            return self.previous
        if text:
            head, sep, tail = text.partition(":")
            if sep and head and "/" not in head:
                if is_uuid(head):
                    return head, normalize(tail or "/"), None
                found = fs.lookup_name(self.tc, head)
                if found is not None:
                    collection, base = found
                    return collection, normalize(fs._join_within(base, tail)), head
        if self.here is None:
            raise ShellError(
                "not on a collection yet: cd NAME: or cd UUID:/path first "
                "('collections' and 'bookmark list' show the names)"
            )
        collection, cwd, entered = self.here
        if not text or text == ".":
            return self.here
        if text == "~" or text.startswith("~/"):
            return collection, normalize("/~/" + text[2:]), entered
        return collection, normalize(posixpath.join(cwd, text)), entered

    # ---- names for humans ------------------------------------------------
    def _names_on(self, collection):
        """Bookmarks, then site shortcuts, that point into a collection.

        Skips names that would not paste back as one word, and a shortcut
        hidden by a bookmark of the same name (the bookmark wins on lookup).

        :param collection: the collection UUID
        :returns: ``(name, base_path, rank)``, rank 0 for bookmarks, 1 shortcuts
        """
        found = []
        try:
            bookmarks = fs.bookmark_index(self.tc)
        except SystemExit:  # bookmarks unavailable: shortcuts still work
            bookmarks = []
        for b in bookmarks:
            if b.get("endpoint_id") == collection and b.get("name"):
                found.append((b["name"], normalize(b.get("path") or "/"), 0))
        taken = {b.get("name") for b in bookmarks}
        for name, sc in fs.site_shortcuts().items():
            if sc.collection == collection and name not in taken:
                found.append((name, normalize(sc.path), 1))
        return [f for f in found if not any(c.isspace() or c == ":" for c in f[0])]

    def name_for(self, collection, path, entered=None):
        """The ``name:path`` form of a location, as cd and globus-fs accept it.

        Prefers the name it was entered by, then bookmarks, then shortcuts,
        and the most specific base. ``name:rel`` is relative to the name's
        base path, so it resolves back to exactly this location.

        :param collection: the collection UUID
        :param path: the absolute path
        :param entered: the name the collection was entered by, if any
        :returns: ``(name, "name:path")``, or None if no name covers it
        """
        best = None
        for name, base, rank in self._names_on(collection):
            if path != base and not path.startswith(base.rstrip("/") + "/"):
                continue
            key = (name != entered, rank, -len(base))
            if best is None or key < best[0]:
                best = (key, name, base)
        if best is None:
            return None
        _, name, base = best
        if base == "/":
            return name, f"{name}:{path}"
        return name, f"{name}:{path[len(base):].lstrip('/')}"

    def display_name(self, collection):
        """The collection's Globus display name, fetched once per session.

        :param collection: the collection UUID
        :returns: the display name, or None if it cannot be had
        """
        if collection not in self._display_names:
            try:
                doc = self.tool.call(self.tc.get_endpoint, collection)
                self._display_names[collection] = doc.get("display_name") or None
            except globus_sdk.GlobusAPIError:
                self._display_names[collection] = None
        return self._display_names[collection]

    def label(self, collection, path, entered=None):
        """What the prompt calls the current collection.

        The name pwd would print, else the display name (shortened), else
        the start of the UUID.

        :param collection: the collection UUID
        :param path: the absolute path
        :param entered: the name the collection was entered by, if any
        :returns: a short label
        """
        named = self.name_for(collection, path, entered)
        if named:
            return named[0]
        title = self.display_name(collection)
        if title:
            return title if len(title) <= 32 else title[:31] + "\u2026"
        return collection[:8]

    def _listing(self, collection, path):
        """List a directory for tab completion, reusing a recent listing.

        :param collection: the collection UUID
        :param path: the absolute directory path
        :returns: the listing entries
        """
        now = time.monotonic()
        hit = self._cache.get((collection, path))
        if hit and now - hit[0] < COMPLETION_TTL:
            return hit[1]
        entries = list(self.tc.operation_ls(collection, path=path))
        self._cache[(collection, path)] = (now, entries)
        return entries

    # ---- the loop -------------------------------------------------------
    def run(self):
        """Start at ``start``, if given, and read commands until exit.

        :returns: the status of the last command, as a script would see it
        """
        if self.start:
            self.onecmd(f"cd {shlex.quote(self.start)}")
            if self.status:
                return self.status
        if self.interactive:
            print(BANNER, file=sys.stderr)
            self._setup_readline()
        while True:
            try:
                self.cmdloop()
                return self.status
            except KeyboardInterrupt:  # at the prompt, as bash does
                print(file=sys.stderr)

    @staticmethod
    def _setup_readline():
        """Complete whole paths; keep history in memory only."""
        try:
            import readline
        except ImportError:  # pragma: no cover - always present on Linux
            return
        readline.set_completer_delims(" \t\n|><")

    def emptyline(self):
        """Do nothing on an empty line, rather than repeat the last command.

        :returns: False, to keep going
        """
        return False

    def onecmd(self, line):
        """Run one line; report any error and carry on.

        :param line: the line typed
        :returns: True to leave the shell
        """
        line = line.strip()
        if line == "EOF":
            if self.interactive:
                print(file=sys.stderr)
            return True
        if not line or line.startswith("#"):
            return False
        if line.startswith("!"):
            return self._local(line[1:])
        self.status = 1
        try:
            words, filters, redirect = split_line(line)
            for f in filters:
                check_filter(f)
            with self._output(filters, redirect) as out:
                with contextlib.redirect_stdout(out):
                    leave = self._run(words)
            self.status = 0
            return leave
        except ShellError as e:
            print(f"globus-fs shell: {e}", file=sys.stderr)
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
            elif not e.code:
                self.status = 0
        except KeyboardInterrupt:
            print("^C", file=sys.stderr)
        except BrokenPipeError:  # `| head` closed the pipe early
            self.status = 0
        except Exception as e:  # a bug must not throw away the session
            print(
                f"globus-fs shell: internal error, please report: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
        return False

    @contextlib.contextmanager
    def _output(self, filters, redirect):
        """Where a command's output goes: the terminal, filters, or a file.

        :param filters: the words of each filter after ``|``
        :param redirect: ``(file, append)`` for ``>``/``>>``, or None
        :returns: a context manager yielding a text stream
        """
        sink = open_redirect(*redirect) if redirect else None
        try:
            if not filters:
                if sink is None:
                    yield sys.stdout
                    return
                stream = io.TextIOWrapper(sink, encoding="utf-8", write_through=True)
                yield stream
                stream.close()
                return
            env = dict(os.environ, LESSSECURE="1")  # no !, | or s in less
            procs = []
            for i, argv in enumerate(filters):
                last = i == len(filters) - 1
                try:
                    procs.append(
                        subprocess.Popen(  # nosec B603 - allow-listed, no shell
                            argv,
                            stdin=subprocess.PIPE if i == 0 else procs[-1].stdout,
                            stdout=(sink if last else subprocess.PIPE),
                            env=env,
                        )
                    )
                except OSError as e:
                    raise ShellError(f"{argv[0]}: {e.strerror}") from None
                if i:
                    procs[-2].stdout.close()  # so the writer sees a closed pipe
            stream = io.TextIOWrapper(
                procs[0].stdin, encoding="utf-8", write_through=True
            )
            try:
                yield stream
            finally:
                with contextlib.suppress(BrokenPipeError):
                    stream.close()
                for p in procs:
                    p.wait()
        finally:
            if sink is not None:
                sink.close()

    def _run(self, words):
        """Run one command, a shell builtin or a globus-fs command.

        :param words: the command and its arguments
        :returns: True to leave the shell
        """
        name, rest = words[0], words[1:]
        if name in ("exit", "quit"):
            return True
        builtin = getattr(self, f"builtin_{name}", None)
        if builtin is not None:
            builtin(rest)
            return False
        if name == "shell":
            raise ShellError("already in the shell")
        if name not in self.parser.command_names:
            raise ShellError(f"{name}: unknown command; 'help' lists them")
        if name == "find":
            words = [FIND_ALIASES.get(w, w) for w in words]
        args = self.parser.parse_args(words)
        if name in PATH_COMMANDS:
            collection, path, _ = self.resolve(args.target)
            args.target = f"{collection}:{path}"
        self.tool.dispatch(args)
        return False

    def _local(self, command):
        """Run ``!command`` with the user's own shell, explicitly local.

        :param command: everything after the ``!``
        :returns: False, to keep going
        """
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            self.status = subprocess.call(  # nosec B603 - the user's own command
                [shell, "-c", command]
            )
        except KeyboardInterrupt:
            self.status = 130
        return False

    # ---- builtins: each takes its words after the command name ---------
    def builtin_cd(self, words):
        """cd [PATH]: change directory, or collection with name: or UUID:.

        :param words: zero or one path
        """
        if len(words) > 1:
            raise ShellError("cd takes one path")
        if not words:
            if self.home is None:
                raise ShellError("cd needs a path: cd NAME: or cd UUID:/path")
            target = self.home
        else:
            target = self.resolve(words[0])
        collection, path, _ = target
        if path != "/":
            try:
                entry = self.tool.call(self.tc.operation_stat, collection, path)
            except globus_sdk.GlobusAPIError as e:
                self.tool.api_error(e)
            if entry.get("type") != "dir":
                raise ShellError(f"{path}: not a directory")
        if self.here is None or self.here[0] != collection:
            self._cache.clear()
            self.home = target
        self.previous, self.here = self.here, target
        self._set_prompt()

    def builtin_pwd(self, words):
        """pwd [-u | -v]: where you are, in a form you can paste back.

        ``name:path`` by default, for cd, globus-fs and people; ``-u`` the
        ``UUID:/path`` globus-cli and the web app take; ``-v`` both, plus
        the collection's display name.

        :param words: no option, ``-u`` or ``-v``
        """
        if len(words) > 1 or (words and words[0] not in ("-u", "-v")):
            raise ShellError("usage: pwd [-u | -v]")
        collection, path, entered = self.resolve(None)
        uuid_form = f"{collection}:{path}"
        named = self.name_for(collection, path, entered)
        if not words:
            print(named[1] if named else uuid_form)
        elif words[0] == "-u":
            print(uuid_form)
        else:
            print(f"      name: {named[1] if named else '-'}")
            print(f"collection: {self.display_name(collection) or '-'}")
            print(f"      uuid: {uuid_form}")

    def builtin_lcd(self, words):
        """lcd [DIR]: change the local directory > and >> write in.

        :param words: zero or one local directory
        """
        target = os.path.expanduser(words[0] if words else "~")
        try:
            os.chdir(target)
        except OSError as e:
            raise ShellError(f"lcd: {target}: {e.strerror}") from None

    def builtin_lpwd(self, words):
        """lpwd: print the local directory.

        :param words: none
        """
        print(os.getcwd())

    def builtin_help(self, words):
        """help [COMMAND]: list commands, or one command's options.

        :param words: zero or one command name
        """
        if words and words[0] in self.parser.command_names:
            self.parser.parse_args([words[0], "--help"])
        print(fs.SHELL_EPILOG)

    # ---- tab completion ------------------------------------------------
    def completenames(self, text, *ignored):
        """Complete the command name.

        :param text: the partial name
        :returns: matching command names
        """
        names = {n[len("builtin_") :] for n in dir(self) if n.startswith("builtin_")}
        names |= (self.parser.command_names | {"exit"}) - {"shell"}
        return sorted(n for n in names if n.startswith(text))

    def completedefault(self, text, line, begidx, endidx):
        """Complete a remote path from one (cached) directory listing.

        :param text: the partial path
        :param line: the whole line
        :param begidx: where ``text`` starts in ``line``
        :param endidx: where it ends
        :returns: matching paths, directories with a trailing slash
        """
        if line.split()[0] in ("lcd", "help"):
            return []
        try:
            cut = text.rfind("/") + 1
            prefix, partial = text[:cut], text[cut:]
            collection, path, _ = self.resolve(prefix or None)
            dirs_only = line.split()[0] == "cd"
            out = []
            for e in self._listing(collection, path):
                name = e.get("name", "")
                is_dir = e.get("type") == "dir"
                if name.startswith(partial) and (is_dir or not dirs_only):
                    out.append(prefix + name + ("/" if is_dir else ""))
            return out
        except Exception:  # nosec B110 - completion must never break the prompt
            return []
