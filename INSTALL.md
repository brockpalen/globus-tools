# Installing a standalone build

How to build `globus-fs` and `globus-console` as a self-contained install
that an Lmod module puts on `PATH`. Users need nothing else: no Python, no
virtualenv, no other module. For development, see the [README](README.md).

## Quick start

```sh
# once, on the build host
curl -LsSf https://astral.sh/uv/install.sh | sh     # or: pip install --user uv

# each release
git clone <this repo> && cd globus-console-cli
packaging/build-standalone.sh /sw/pkgs/arc/globus-fs/0.2.0
```

Then install the modulefile ([`packaging/globus-fs.lua`](packaging/globus-fs.lua))
with `root` pointing at that directory, and users run:

```sh
module load globus-fs
globus-fs login
globus-fs du dataden:/mylab -d 1
```

A build takes a few seconds and about 135 MB.

## What gets built

```
/sw/pkgs/arc/globus-fs/0.2.0/
├── bin/
│   ├── globus-fs          the only two things put on PATH
│   └── globus-console
├── python/                a private CPython 3.12 with every dependency inside
├── BUILD-INFO             version, date, host, commit, target platform
└── etc/collections.conf   optional: add your site shortcuts here yourself
```

- **Its own interpreter.** `python/` is a
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  CPython, fetched by `uv`. It needs only glibc 2.17 and nothing from the
  system Python.
- **Locked dependencies.** Installed at the versions and hashes in
  [`uv.lock`](uv.lock): every site building the same commit gets the same,
  tested dependencies, and a build fails rather than quietly resolving
  something new. The lock covers every platform and Python version, so an
  aarch64 or older-glibc build uses it too.
- **Isolated launchers.** `bin/globus-fs` is a two-line shell script that
  runs the bundled Python with `-I` (isolated mode). `PYTHONPATH`,
  `PYTHONHOME`, `~/.local` packages and any loaded Python module are all
  ignored, so nothing in a user's environment can shadow a dependency.
- **Relocatable.** The launchers find the interpreter relative to
  themselves, so the tree can be built anywhere and then copied or moved —
  including onto another cluster's software tree.
- **Read-only friendly.** Bytecode is compiled at build time, so running
  from a read-only `/sw` never tries (and fails) to write `.pyc` files.

## Choosing the target glibc

A standalone install only runs where the **C library is at least as new**
as the newest one any file inside it needs. The build therefore picks
compiled dependencies (e.g. `cryptography`) for a stated target, not for
the host it runs on:

| variable | default | meaning |
| --- | --- | --- |
| `GLIBC` | `2.28` | oldest glibc the install must run on (RHEL/Rocky 8) |
| `PYTHON_VERSION` | `3.12` | the bundled interpreter |
| `ARCH` | this host | `x86_64` or `aarch64` |

```sh
ldd --version | head -1                     # check each target system
GLIBC=2.34 packaging/build-standalone.sh /sw/pkgs/arc/globus-fs/0.2.0   # RHEL 9 only
```

This matters most when the build host is newer than the clusters. Built on
Ubuntu 24.04 (glibc 2.39) with the defaults, the most any file in the tree
requires is `GLIBC_2.28`. To check a finished build:

```sh
find /sw/pkgs/arc/globus-fs/0.2.0 -name '*.so*' -type f \
  | xargs -n1 objdump -T 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1
```

Changing `GLIBC`, `PYTHON_VERSION` or `ARCH` needs no change to the lock.
If no locked version of a package has a wheel for the target, the install
stops with an error naming it.

## Updating dependencies

The same loop as `pipenv update`: move the lock, test, commit. `uv.lock` is
generated; do not edit it by hand.

```sh
uv lock --upgrade                    # everything to the newest allowed
uv lock --upgrade-package requests   # or just one package
uv sync                              # make .venv match the new lock
uv run pytest                        # test against it
git diff --stat uv.lock              # review what moved
git commit uv.lock
```

After adding or changing a dependency in `pyproject.toml`, run `uv lock`
(or `uv add <package>`, which edits both). A pre-commit hook runs
`uv lock --check` and refuses a commit whose lock no longer matches
`pyproject.toml`.

| pipenv | uv |
| --- | --- |
| `pipenv install --dev` | `uv sync` |
| `pipenv update` | `uv lock --upgrade && uv sync` |
| `pipenv update requests` | `uv lock --upgrade-package requests && uv sync` |
| `pipenv install requests` | `uv add requests` |
| `pipenv run pytest` | `uv run pytest` |
| `Pipfile.lock` | `uv.lock` |

## Releasing a new version

1. Bump `__version__` in `src/globus_console_cli/__init__.py`, commit, tag.
2. Build into a **new** directory — the script refuses to overwrite a
   non-empty one, since people may be running the old install:

   ```sh
   packaging/build-standalone.sh /sw/pkgs/arc/globus-fs/0.3.0
   ```

3. Copy the modulefile to `0.3.0.lua`, update `root`, and move the default
   when you are ready. Old versions keep working until you delete them.

The build smoke-tests both commands with `--help` before it reports
success. A quick real check after installing:

```sh
module load globus-fs/0.3.0
globus-fs whoami
globus-fs ls dataden:/
```

## Site configuration

Neither is required; both are shared by every version.

- **Collection shortcuts.** Either ship `/etc/globus-fs/collections.conf` on
  every host, or put the file in the install and point
  `GLOBUS_FS_COLLECTIONS` at it from the module, as the example modulefile
  does. Start from [`examples/collections.conf`](examples/collections.conf).
- **archivescan threshold.** `setenv("GLOBUS_FS_ARCHIVESCAN_SIZE", "10M")`
  in the module for a tier that migrates below the 100 MiB default.

There is no client ID to configure: the Globus client ID is built in (see
the README).

## Why not PyInstaller?

PyInstaller works, and is a reasonable way to ship a single Python
program. This approach was chosen instead because:

| | this build | PyInstaller `--onefile` |
| --- | --- | --- |
| startup | immediate | unpacks itself to `$TMPDIR` on **every run** — slow, and fails where `/tmp` is `noexec` |
| glibc | pick the target explicitly (`GLIBC=2.28`) from any build host | inherits the build host's glibc; must be built on the oldest OS, usually in a container |
| debugging | ordinary Python files; `python/bin/python3 -I -m globus_console_cli.fs` | opaque archive |
| hidden imports | none — the package is installed normally | needs hooks when libraries import lazily |
| dependencies | hash-locked | whatever the build environment had |
| size | ~135 MB, one tree | smaller single file |

PyInstaller's `--onedir` mode avoids the unpacking cost, but still ties
the build to the host's glibc. If a single file you can `scp` to a machine
with no shared software tree matters more than any of the above, it
remains an option.
