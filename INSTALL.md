# Installing

Builds `globus-fs` and `globus-console` as a self-contained tree with its
own Python. Put `PREFIX/bin` on users' `PATH` (with Lmod or however your
site does it); users need nothing else.

Requires [uv](https://docs.astral.sh/uv/) on the build host only.

## Build

```sh
packaging/build-standalone.sh /sw/pkgs/globus-fs/0.2.0
```

```
/sw/pkgs/globus-fs/0.2.0/
├── bin/globus-fs        put this directory on PATH
├── bin/globus-console
├── python/              private Python and dependencies
└── BUILD-INFO           version, commit, build date and target
```

Build as the account that owns your software tree, with `umask 022`:
every user runs this code with their own Globus tokens, so nothing in it
may be writable by anyone else. Check with
`find /sw/pkgs/globus-fs/0.2.0 -perm /022 ! -type l` (should print nothing).

The script refuses to write into a non-empty directory, so build each
version into its own. The tree is relocatable and runs read-only. The
launchers run Python in isolated mode, so a user's `PYTHONPATH`,
`~/.local` or loaded modules cannot interfere.

### Build variables

| variable | default | |
| --- | --- | --- |
| `GLIBC` | `2.28` | oldest glibc the install must run on (`ldd --version`); 2.28 is RHEL 8 |
| `PYTHON_VERSION` | `3.12` | bundled interpreter |
| `ARCH` | build host | `x86_64` or `aarch64` |

```sh
GLIBC=2.34 packaging/build-standalone.sh /sw/pkgs/globus-fs/0.2.0
```

Set `GLIBC` to your oldest target system, not the build host: compiled
dependencies are chosen for it.

## Updating dependencies

Dependencies are pinned, with hashes, in `uv.lock`, the same idea as
`Pipfile.lock`:

```sh
uv lock --upgrade                    # pipenv update
uv lock --upgrade-package requests   # just one package
uv sync                              # update .venv to match
uv run pytest                        # test
git commit uv.lock
```

After changing dependencies in `pyproject.toml`, run `uv lock`. The build
and a pre-commit hook both refuse a lock that no longer matches.

## Runtime variables

Set these in your module, or anywhere in users' environment. All are
optional.

| variable | |
| --- | --- |
| `GLOBUS_FS_COLLECTIONS` | colon-separated collection shortcut files; format in the [README](README.md#collection-shortcuts). `/etc/globus-fs/collections.conf` is always read too |
| `GLOBUS_FS_ARCHIVESCAN_SIZE` | default for `archivescan --size`, e.g. `10M`; built-in default `100M` |
| `GLOBUS_FS_CLIENT_ID`, `GLOBUS_CONSOLE_CLIENT_ID` | use your own Globus client registration instead of the built-in one |
| `GLOBUS_CONSOLE_CLIENT_SECRET` | with `GLOBUS_CONSOLE_CLIENT_ID`, run `globus-console` unattended as a service account |
