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
import fnmatch
import io
import os
import posixpath
import shlex
import stat
import subprocess  # nosec B404 - runs the allow-listed filters, without a shell
import sys
import time

import globus_sdk

from . import fs
from .common import escape_text, is_uuid, safe, terminal_output

#: What output may be piped into. Each only reads its input, once the
#: checks in :func:`check_filter` rule out the options that write files or
#: run programs.
FILTERS = frozenset(
    "wc grep egrep fgrep sort uniq head tail cut tr column nl less".split()
)

#: Commands whose path argument is resolved against the current directory.
PATH_COMMANDS = frozenset(("ls", "stat", "find", "du", "archivescan"))

#: Commands taking several paths, each resolved; they change the collection.
WRITE_COMMANDS = frozenset(("mkdir", "mv"))

#: Commands whose path arguments expand *, ? and [...] against the
#: collection, as bash does locally. Not find: its patterns are for --name.
GLOB_COMMANDS = frozenset(("ls", "stat", "du", "archivescan", "mv", "cd"))

#: Of those, the ones taking a single path.
ONE_PATH = frozenset(("archivescan", "cd"))

#: find's familiar single-dash spellings, mapped to globus-fs options.
FIND_ALIASES = {
    "-name": "--name",
    "-iname": "--iname",
    "-type": "--type",
    "-maxdepth": "--maxdepth",
}

#: Prompt colours (ANSI SGR): cyan label, amber for high assurance, bold path.
LABEL_COLOR = "36"
HA_COLOR = "1;33"
PATH_COLOR = "1"


def use_color():
    """Whether the prompt is coloured.

    ``GLOBUS_FS_COLOR=always|never|auto`` decides; auto, the default, means
    an interactive terminal, without ``NO_COLOR`` set or ``TERM=dumb``.

    :returns: True to colour the prompt
    """
    choice = os.environ.get("GLOBUS_FS_COLOR", "auto").lower()
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _paint(code, text):
    """Colour text for the prompt, marked so readline counts its width right.

    ``\\001``/``\\002`` tell readline the escape codes take no space;
    without them line editing miscounts the prompt.

    :param code: the SGR code, e.g. ``"36"``
    :param text: the text, already escaped
    :returns: the coloured text
    """
    return f"\001\033[{code}m\002{text}\001\033[0m\002"


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
    words = [parse_words(stage) for stage in stages]
    if any(not w for w in words):
        raise ShellError("nothing on one side of a |")
    return words[0], words[1:], redirect


class Word(str):
    """One word of a command line, remembering what was quoted.

    Compares and behaves as the plain text. ``glob`` is an fnmatch pattern
    if the word holds an unquoted ``*``, ``?`` or ``[...]``, with quoted
    wildcard characters escaped, else None.
    """

    glob = None
    parts = ()

    @classmethod
    def from_chars(cls, chars):
        """Build a word from ``(character, quoted)`` pairs.

        :param chars: the characters, each with whether it was quoted
        :returns: a :class:`Word`, with ``glob`` and, per ``/``-separated
            part, ``parts`` as ``(text, pattern or None)``
        """
        word = cls("".join(c for c, _ in chars))
        word.glob = _pattern(chars)
        if word.glob is not None:
            pieces, current = [], []
            for pair in chars:
                if pair[0] == "/":
                    pieces.append(current)
                    current = []
                else:
                    current.append(pair)
            pieces.append(current)
            word.parts = tuple(
                ("".join(c for c, _ in piece), _pattern(piece)) for piece in pieces
            )
        return word


def _pattern(chars):
    """The fnmatch pattern for some characters, or None if none are wild.

    :param chars: ``(character, quoted)`` pairs
    :returns: the pattern, quoted wildcards escaped, or None
    """
    free = [c for c, quoted in chars if not quoted]
    text = "".join(c for c, _ in chars)
    if "*" in free or "?" in free or ("[" in free and "]" in text[text.find("[") :]):
        return "".join(f"[{c}]" if quoted and c in "*?[" else c for c, quoted in chars)
    return None


def parse_words(text):
    """Split a stage into words as a shell would: quotes, backslashes, braces.

    :param text: one stage of a line, quotes and backslashes as typed
    :returns: a list of :class:`Word`
    :raises ShellError: on an unterminated quote
    """
    words, current, quote = [], None, None
    i = 0
    while i < len(text):
        c = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else None
        if quote == "'":
            if c == "'":
                quote = None
            else:
                current.append((c, True))
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\" and nxt is not None and nxt in '"\\`$':
                current.append((nxt, True))
                i += 1
            else:
                current.append((c, True))
        elif c in " \t":
            if current is not None:
                words.append(current)
                current = None
        else:
            if current is None:
                current = []
            if c in "'\"":
                quote = c
            elif c == "\\" and nxt is not None:
                current.append((nxt, True))
                i += 1
            else:
                current.append((c, False))
        i += 1
    if quote:
        raise ShellError("unterminated quote")
    if current is not None:
        words.append(current)
    return [Word.from_chars(w) for chars in words for w in expand_braces(chars)]


def expand_braces(chars):
    """Expand unquoted ``{a,b}`` as bash does: ``x{1,2}`` is ``x1 x2``.

    A brace pair without an unquoted comma at its own level is left as
    text, as in bash; expansion nests and repeats.

    :param chars: one word's ``(character, quoted)`` pairs
    :returns: the list of words it expands to
    """
    depth, start, commas = 0, None, []
    for i, (c, quoted) in enumerate(chars):
        if quoted:
            continue
        if c == "{":
            if depth == 0:
                start, commas = i, []
            depth += 1
        elif c == "," and depth == 1:
            commas.append(i)
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                if not commas:
                    start = None
                    continue
                head, tail = chars[:start], chars[i + 1 :]
                bounds = [start] + commas + [i]
                out = []
                for left, right in zip(bounds, bounds[1:]):
                    out += expand_braces(head + chars[left + 1 : right] + tail)
                return out
    return [chars]


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
    path = os.path.expanduser(name)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    try:
        if not append and not stat.S_ISREG(os.stat(path).st_mode):
            flags = os.O_WRONLY  # /dev/null and the like: noclobber is for files
    except OSError:
        pass  # does not exist yet: create it
    try:
        fd = os.open(path, flags, 0o600)
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
        self._collections = {}  # UUID -> its Globus document, fetched once
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
        """Show the collection and directory, and nothing when scripted.

        A high assurance collection is marked ``HA`` in words, not only in
        colour; colour, when on, makes the label cyan (amber for high
        assurance) and the path bold.
        """
        if not self.interactive:
            self.prompt = ""
            return
        if self.here is None:
            self.prompt = "(no collection)> "
            return
        collection, path, entered = self.here
        label = escape_text(self.label(collection, path, entered))
        high = self.high_assurance(collection)
        if high:
            label += " HA"
        path = escape_text(path)
        if use_color():
            label = _paint(HA_COLOR if high else LABEL_COLOR, label)
            path = _paint(PATH_COLOR, path)
        self.prompt = f"[{label}] {path}> "

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
                "not on a collection yet: cd NAME: or cd UUID:/path first, "
                "where NAME is a bookmark or collection shortcut "
                "('collections' and 'bookmark list' show them)"
            )
        collection, cwd, entered = self.here
        if not text or text == ".":
            return self.here
        if text == "~" or text.startswith("~/"):
            return collection, normalize("/~/" + text[2:]), entered
        return collection, normalize(posixpath.join(cwd, text)), entered

    def _start_location(self, start):
        """Accept a bare name or UUID as where to start, as ls does.

        With nothing to be relative to yet, ``dataden`` can only mean the
        shortcut or bookmark, so it starts at ``dataden:``.

        :param start: the target given to ``globus-fs shell``
        :returns: the target to cd to, or None after explaining a bad one
        """
        if ":" in start:
            return start
        if is_uuid(start) or self._is_location(start):
            return start + ":"
        print(
            f"globus-fs shell: '{escape_text(start)}' is not a collection UUID, "
            "a bookmark or a collection shortcut.\n"
            + fs._known_names_hint(self.tc, start),
            file=sys.stderr,
        )
        return None

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
        return self._collection(collection).get("display_name") or None

    def high_assurance(self, collection):
        """Whether Globus marks a collection as high assurance.

        :param collection: the collection UUID
        :returns: True for a high assurance collection
        """
        return bool(self._collection(collection).get("high_assurance"))

    def _collection(self, collection):
        """A collection's Globus document, fetched once per session.

        :param collection: the collection UUID
        :returns: the document, or an empty dict if it cannot be had
        """
        if collection not in self._collections:
            try:
                doc = self.tool.call(self.tc.get_endpoint, collection)
                self._collections[collection] = dict(doc.data)
            except globus_sdk.GlobusAPIError:
                self._collections[collection] = {}
        return self._collections[collection]

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

    def _listing(self, collection, path, login=False):
        """List a directory for completion or globbing, reusing a recent one.

        :param collection: the collection UUID
        :param path: the absolute directory path
        :param login: log in again if the collection asks (globbing); never
            for Tab completion, which must not start a sign-in
        :returns: the listing entries
        """
        now = time.monotonic()
        hit = self._cache.get((collection, path))
        if hit and now - hit[0] < COMPLETION_TTL:
            return hit[1]
        if login:
            entries = list(self.tool.call(self.tc.operation_ls, collection, path=path))
        else:
            entries = list(self.tc.operation_ls(collection, path=path))
        self._cache[(collection, path)] = (now, entries)
        return entries

    # ---- the loop -------------------------------------------------------
    def run(self):
        """Start at ``start``, if given, and read commands until exit.

        :returns: the status of the last command, as a script would see it
        """
        if self.start:
            start = self._start_location(self.start)
            if start is None:
                return 1
            self.onecmd(f"cd {shlex.quote(start)}")
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
            # where the output really lands decides whether names are
            # escaped: a file gets them exact, the terminal never raw, even
            # through `| head`
            reaches_terminal = None
            if redirect:
                reaches_terminal = False
            elif filters:
                reaches_terminal = sys.stdout.isatty()
            intent = (
                contextlib.nullcontext()
                if reaches_terminal is None
                else terminal_output(reaches_terminal)
            )
            with self._output(filters, redirect) as out:
                with contextlib.redirect_stdout(out), intent:
                    leave = self._run(words)
            self.status = 0
            return leave
        except ShellError as e:
            print(
                f"globus-fs shell: {escape_text(e, keep_newlines=True)}",
                file=sys.stderr,
            )
        except SystemExit as e:
            if isinstance(e.code, str):
                print(escape_text(e.code, keep_newlines=True), file=sys.stderr)
            elif not e.code:
                self.status = 0
        except KeyboardInterrupt:
            print("^C", file=sys.stderr)
        except BrokenPipeError:  # `| head` closed the pipe early
            self.status = 0
        except Exception as e:  # a bug must not throw away the session
            print(
                f"globus-fs shell: internal error, please report: "
                f"{type(e).__name__}: {escape_text(e)}",
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
        name = words[0]
        if name in GLOB_COMMANDS:
            words = [name] + self.expand_words(name, words[1:])
        rest = words[1:]
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
            if isinstance(args.target, list):  # ls, stat, du: several paths
                args.target = [
                    "%s:%s" % self.resolve(t)[:2] for t in args.target or [None]
                ]
            else:
                collection, path, _ = self.resolve(args.target)
                args.target = f"{collection}:{path}"
        if name not in WRITE_COMMANDS:
            self.tool.dispatch(args)
            return False
        args.paths = ["%s:%s" % self.resolve(p)[:2] for p in args.paths]
        try:
            self.tool.dispatch(args)
        finally:
            self._cache.clear()  # listings for Tab completion are stale now
            if name == "mv":
                self._check_cwd()
        return False

    # ---- globs ------------------------------------------------------------
    def expand_words(self, command, words):
        """Expand wildcards in path arguments against the collection.

        Options (``-x``, ``--x``) and words without unquoted wildcards pass
        through. A pattern that matches nothing is an error, as with bash's
        failglob: passing it on would only end in "not found".

        :param command: the command, for its one-path rule
        :param words: the words after the command name
        :returns: the words, each pattern replaced by its sorted matches
        :raises ShellError: for no match, or several for a one-path command
        """
        out = []
        for word in words:
            if getattr(word, "glob", None) is None or word.startswith("-"):
                out.append(word)
                continue
            matches = self.expand(word)
            if not matches:
                raise ShellError(f"no match: {escape_text(word)}")
            if command in ONE_PATH and len(matches) > 1:
                raise ShellError(
                    f"{command} takes one path; {escape_text(word)} matches "
                    f"{len(matches)}"
                )
            out += matches
        return out

    def expand(self, word):
        """Expand one pattern, one directory level at a time, as bash does.

        ``*`` does not match a leading dot unless the pattern starts with
        one, and a wildcard level before the last matches directories only.

        :param word: a :class:`Word` with a ``glob``
        :returns: the matching paths, written the way the pattern was
        """
        parts = list(word.parts)
        prefix = ""
        head, sep, rest = parts[0][0].partition(":")
        if sep and head and parts[0][1] is not None and self._is_location(head):
            # name:*.tar -- the name is literal, the rest a pattern
            prefix = head + ":"
            parts[0] = (rest, word.glob.split("/")[0][len(prefix) :] or None)
        first = next(i for i, (_, pat) in enumerate(parts) if pat is not None)
        literal = "/".join(text for text, _ in parts[:first])
        lead = prefix + (literal + "/" if first else "")
        collection, base, _ = self.resolve(lead or None)
        found = [("", base)]
        remaining = parts[first:]
        for index, (text, pattern) in enumerate(remaining):
            last = index == len(remaining) - 1
            step = []
            for shown, path in found:
                joiner = "" if not shown else "/"
                if pattern is None:
                    step.append((shown + joiner + text, posixpath.join(path, text)))
                    continue
                for entry in self._entries(collection, path):
                    name = entry.get("name", "")
                    if name.startswith(".") and not pattern.startswith("."):
                        continue
                    if not last and entry.get("type") != "dir":
                        continue
                    if fnmatch.fnmatchcase(name, pattern):
                        step.append((shown + joiner + name, posixpath.join(path, name)))
            found = step
        return sorted(lead + shown for shown, _ in found)

    def _is_location(self, head):
        """Whether ``head`` before a colon names a collection.

        :param head: the text before the first colon
        :returns: True for a UUID, bookmark or collection shortcut
        """
        return is_uuid(head) or fs.lookup_name(self.tc, head) is not None

    def _entries(self, collection, path):
        """List a directory for globbing; an unlistable one matches nothing.

        :param collection: the collection UUID
        :param path: the directory
        :returns: its entries, or an empty list
        """
        try:
            return self._listing(collection, path, login=True)
        except globus_sdk.GlobusAPIError:
            return []

    def _check_cwd(self):
        """After an mv, say so if the current directory is no longer there."""
        collection, path, _ = self.here
        if path == "/":
            return
        try:
            self.tool.call(self.tc.operation_stat, collection, path)
        except globus_sdk.GlobusAPIError:
            print(
                f"globus-fs shell: {escape_text(path)} was moved; cd to its new "
                "location",
                file=sys.stderr,
            )

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
                if e.http_status == 404 and words:
                    self._cd_hint(words[0])
                self.tool.api_error(e)
            if entry.get("type") != "dir":
                raise ShellError(f"{path}: not a directory")
        if self.here is None or self.here[0] != collection:
            self._cache.clear()
            self.home = target
        self.previous, self.here = self.here, target
        self._set_prompt()

    def _cd_hint(self, word):
        """Explain a cd that found nothing, when the word looks like a name.

        ``cd dataden`` means a subdirectory, as in any shell; if there is
        none but a shortcut or bookmark has that name, say how to reach it.
        ``cd nosuch:`` names nothing known; suggest Globus collections whose
        display name matches.

        :param word: what was typed after cd
        :raises ShellError: with the hint, when there is one
        """
        head, sep, _ = word.partition(":")
        if not head or "/" in head or is_uuid(head):
            return
        if not sep and self._is_location(head):
            kind = (
                "bookmark"
                if fs.resolve_bookmark(self.tc, head)
                else "collection shortcut"
            )
            raise ShellError(
                f"no directory {escape_text(word)} here. For your {kind} of "
                f"that name: cd {escape_text(head)}:"
            )
        if sep and not self._is_location(head):
            raise ShellError(
                f"no directory {escape_text(word)} here, and no bookmark or "
                f"collection shortcut named {escape_text(head)}."
                + fs.display_name_hint(self.tc, head)
            )

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
            print(safe(named[1] if named else uuid_form))
        elif words[0] == "-u":
            print(safe(uuid_form))
        else:
            print(f"      name: {safe(named[1]) if named else '-'}")
            print(f"collection: {safe(self.display_name(collection) or '-')}")
            print(f"      uuid: {safe(uuid_form)}")
            print(
                "high assurance: "
                + ("yes" if self.high_assurance(collection) else "no")
            )

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

        readline splits words at every space, even an escaped one, so
        ``text`` may be only the end of the word. The real word is found
        here, backslashes and quotes included, and each match is returned
        escaped the same way, minus the part of it already before ``text``.

        :param text: what readline will replace, from its last delimiter
        :param line: the whole line
        :param begidx: where ``text`` starts in ``line``
        :param endidx: where it ends
        :returns: replacements for ``text``
        """
        try:
            stage, raw, piped = current_word(line[:endidx])
            value, quote = unescape_partial(raw)
            if stage.lstrip() == raw:  # the first word of a stage
                # after |, only the allowed filters; the line's own first
                # word is completenames' job
                return sorted(f for f in FILTERS if f.startswith(text)) if piped else []
            command = stage.split()[0]
            if piped or command in ("lcd", "help"):
                return []  # local words, nothing remote to offer
            matches = self._path_matches(value, dirs_only=command == "cd")
            already = raw[: len(raw) - len(text)]
            out = []
            for match in matches:
                word = quote_word(match, quote)
                if word is not None and word.startswith(already):
                    out.append(word[len(already) :])
            return out
        except Exception:  # nosec B110 - completion must never break the prompt
            return []

    def _path_matches(self, value, dirs_only=False):
        """Remote paths starting with what has been typed.

        :param value: the partial path, unescaped
        :param dirs_only: offer directories only, for cd
        :returns: full paths as typed, directories with a trailing slash
        """
        cut = value.rfind("/") + 1
        prefix, partial = value[:cut], value[cut:]
        collection, path, _ = self.resolve(prefix or None)
        out = []
        for e in self._listing(collection, path):
            name = e.get("name", "")
            is_dir = e.get("type") == "dir"
            if escape_text(name) != name:
                continue  # would put control characters on the line
            if name.startswith(partial) and (is_dir or not dirs_only):
                out.append(prefix + name + ("/" if is_dir else ""))
        return out


#: Characters a word needs a backslash before, to stay one word.
_SPECIAL = frozenset(" \t\n\\'\"|<>;&`")


def current_word(text):
    """Find the word being typed at the end of a line, and its stage.

    :param text: the line up to the cursor
    :returns: ``(stage_text, raw_word, piped)``: the text after the last
        unquoted ``|``, the word after the last unquoted, unescaped space,
        and whether a ``|`` came before it
    """
    quote, escaped = None, False
    word_start = stage_start = 0
    for i, c in enumerate(text):
        if escaped:
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c in " \t":
            word_start = i + 1
        elif c in "|<>":
            word_start = i + 1
            if c == "|":
                stage_start = i + 1
    return text[stage_start:], text[word_start:], stage_start > 0


def unescape_partial(raw):
    """Undo backslashes and quotes in a word that may be unfinished.

    :param raw: the word as typed
    :returns: ``(value, open_quote)``; open_quote is ``'`` or ``"`` if the
        word is inside an unclosed quote, else None
    """
    out, quote, escaped = [], None, False
    for c in raw:
        if escaped:
            out.append(c)
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote:
            if c == quote:
                quote = None
            else:
                out.append(c)
        elif c in "'\"":
            quote = c
        else:
            out.append(c)
    return "".join(out), quote


def quote_word(value, quote=None):
    """Write a value back as one word, in the quoting style being used.

    :param value: the unescaped text
    :param quote: the open quote the user typed, or None for backslashes
    :returns: the word, or None if it cannot be written that way (a single
        quote inside single quotes)
    """
    if quote == "'":
        return None if "'" in value else "'" + value
    if quote == '"':
        return '"' + "".join("\\" + c if c in '"\\`' else c for c in value)
    return "".join("\\" + c if c in _SPECIAL else c for c in value)
