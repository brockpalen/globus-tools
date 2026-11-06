# Installing

Builds `globus-fs` and `globus-console` as a self-contained tree with its
own Python. Put `PREFIX/bin` on users' `PATH` (with Lmod or however your
site does it); users need nothing else.

Requires [uv](https://docs.astral.sh/uv/) on the build host only.

## Build

Build from a release tag, so the tree matches a known version:

```sh
git clone https://github.com/brockpalen/globus-tools && cd globus-tools
git checkout v0.2.1
packaging/build-standalone.sh /sw/pkgs/globus-tools/0.2.1
packaging/verify-standalone.sh /sw/pkgs/globus-tools/0.2.1
```

```
/sw/pkgs/globus-tools/0.2.1/
├── bin/globus-fs        put this directory on PATH
├── bin/globus-console
├── python/              private Python and dependencies
└── BUILD-INFO           version, commit, build date and target
```

Build as the account that owns your software tree. Every user runs this
code with their own Globus tokens, so the script removes group and other
write permission from the whole tree, and fails if any remains.

The script refuses to write into a non-empty directory, so build each
version into its own. The tree is relocatable and runs read-only. The
launchers run Python in isolated mode, so a user's `PYTHONPATH`,
`~/.local` or loaded modules cannot interfere. `verify-standalone.sh`
checks all of this, and the glibc requirement, on the finished tree.

Build on the architecture you are installing for: `x86_64`, or `aarch64`
for NVIDIA Grace and Vera nodes. The bundled Python is always the build
host's, so the script refuses to cross-build.

The tree depends on the glibc version, not the distribution: the default
target, 2.28, runs on RHEL 8 and later (Rocky, Alma), SLES 15 SP3 and
later (including HPE Cray OS) and Ubuntu 20.04 and later. CI tests every
build on Rocky 8, Rocky 10 and openSUSE Leap 15.6, and on Ubuntu 24.04.

### Build variables

| variable | default | |
| --- | --- | --- |
| `GLIBC` | `2.28` | oldest glibc the install must run on (`ldd --version`); 2.28 is RHEL 8 |
| `PYTHON_VERSION` | `3.12` | bundled interpreter |

```sh
GLIBC=2.34 packaging/build-standalone.sh /sw/pkgs/globus-tools/0.2.1
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
| `GLOBUS_FS_SHORTCUTS` | the shortcuts themselves, `;`-separated, e.g. `dataden=UUID;scratch=UUID:/scratch`; no file needed. Lmod: `append_path("GLOBUS_FS_SHORTCUTS", "dataden=UUID", ";")` |
| `GLOBUS_FS_ARCHIVESCAN_SIZE` | default for `archivescan --size`, e.g. `10M`; built-in default `100M` |
| `GLOBUS_FS_CLIENT_ID`, `GLOBUS_CONSOLE_CLIENT_ID` | use your own Globus client registration instead of the built-in one |
| `GLOBUS_CONSOLE_CLIENT_SECRET` | with `GLOBUS_CONSOLE_CLIENT_ID`, run `globus-console` unattended as a service account |
