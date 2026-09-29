# globus-console-cli

Two command line tools for a campus Globus subscription, split by who may
use them:

| command | for | needs |
| --- | --- | --- |
| **`globus-fs`** | everyone | a Globus login, nothing more |
| **`globus-console`** | anyone holding an activity role | `activity_monitor` / `activity_manager` on a subscribed collection |

**`globus-fs`** gives `ls`, `find` and `du` (with file counts) for storage
that is only reachable through Globus, plus your own bookmarks and your own
transfers:

```
$ globus-fs du dataden:/umms-mylab -d 1
  SIZE  FILES  DIRS  PATH
104.1G      7     4  /umms-mylab
100.0G      1     1  /umms-mylab/backups
  4.1G      5     1  /umms-mylab/fastq
```

**`globus-console`** is a greppable, one-line-per-task view of the Globus
web console's **Activity** tab — every transfer on the collections you
monitor, not just your own:

```
$ globus-console --errors
TASK_ID   STATUS    ISSUE                OWNER            RATE      FILES  BYTES  FLT    AGE  SOURCE               DEST
0f3a91c2  ACTIVE    PERMISSION_DENIED    user2@umich.edu  0B/s   600/2400  15.0G   14  1h14m  Great Lakes scratch  Turbo Research Storage
7b1e4d80  INACTIVE  EXPIRED_CREDENTIALS  user3@umich.edu  0B/s   900/3600  22.5G    3  1h51m  Great Lakes scratch  Turbo Research Storage
```

**[USAGE.md](USAGE.md) is the full reference** — every command, every option,
with examples. This page covers what the tools are, who can use them, and how
to set them up.

## Who can use which

`globus-console` reads the Transfer API's `endpoint_manager` resources — the
same calls the web console makes. They are not "your tasks plus more"; they
are a different API that answers **403 PermissionDenied** to anyone without
an effective `activity_monitor` or `activity_manager` role on a subscribed
collection. Those roles are held by:

- subscription and endpoint administrators
- **the owner of a guest collection, on that collection.** An owner always
  has an effective `administrator` role, which implies both activity roles,
  so a researcher who shares data through a guest collection can watch every
  transfer to and from it
- anyone granted a role on a collection's **Roles** tab in the web app — a
  guest collection owner can grant `activity_monitor` to a lab manager
- anyone with a role on an endpoint or mapped collection, which carries down
  to the guest collections beneath it

`globus-console endpoints` lists exactly which collections an identity
covers, and `globus-console login` reports the count.

`globus-fs` needs no role. Listings use your own permissions on each
collection, bookmarks are yours, and `globus-fs tasks` / `history` read your
own transfers through `GET /task_list`.

The two tools keep **separate logins** (`~/.globus-fs/` and
`~/.globus-console/`), so logging out of one leaves the other alone.

## Setup

### Install

```sh
python3 -m venv .venv
.venv/bin/pip install .          # or: pip install -e '.[dev]' to develop
```

That puts `globus-fs` and `globus-console` in `.venv/bin/`. For a shared
install, point `pip install --prefix` at your software tree and put its
`bin/` on `PATH` from a module file.

### Log in

```sh
globus-fs login
globus-console login          # separate login, if you hold an activity role
```

That is all a user does. Both tools ship a built-in Globus client ID
(`6a4a00f5-cded-42a4-a2e9-f535af8fc5b0`, a "thick client" registration in
the site's Globus project), the same way globus-cli and archivetar do. It
is not a secret: it only names the application on the consent screen, and
each user still authenticates as themselves. Refresh tokens are stored in
the tool's directory (mode 0600), so you only log in once.

To use a different registration, highest precedence first:
`--client-id <UUID>` (saved to the tool's `config.json`), then
`$GLOBUS_FS_CLIENT_ID` / `$GLOBUS_CONSOLE_CLIENT_ID`, then the saved
config, then the built-in default. Keep a second administrator on the
Globus project that owns the registration, so it outlives any one account.

A site module then only needs the shortcuts:

```lua
-- Lmod module file
setenv("GLOBUS_FS_COLLECTIONS", "/sw/arcts/globus/collections.conf")
```

### Unattended use (cron, dashboards)

Register a **service account / application credential** instead, grant that
client's identity the `activity_monitor` role on your collections, and
export:

```sh
export GLOBUS_CONSOLE_CLIENT_ID=<UUID>
export GLOBUS_CONSOLE_CLIENT_SECRET=<secret>
```

No interactive login is needed in that mode. A secret is never paired with
the built-in client ID: with `GLOBUS_CONSOLE_CLIENT_SECRET` set and no
client ID of its own, the tool refuses and says so.

## What they do

| command | |
| --- | --- |
| `globus-fs ls` | list a directory on a collection |
| `globus-fs find` | recursive search, with size and age filters |
| `globus-fs du` | sizes and file counts per directory |
| `globus-fs collections` | site-wide collection shortcuts |
| `globus-fs bookmark` | your own shortcuts, shared with globus-cli |
| `globus-fs consent` | unlock a GCS v5 mapped collection for listing |
| `globus-fs tasks` / `watch` / `history` / `task` | your own transfers |
| `globus-console` / `tasks` | in-progress transfers on collections you monitor |
| `globus-console watch` | the same table, refreshed, with a live rate column |
| `globus-console history` | completed tasks |
| `globus-console task <id>` | one task in full, `--events` for the error log |
| `globus-console endpoints` | collections you are allowed to monitor |
| `login` / `logout` / `whoami` | credentials, in both tools |

A few things they are good at:

```sh
globus-fs find dataden:/lab --older 365d --min-size 1G
globus-fs du dataden:/lab -d 1
globus-fs tasks --errors                     # why is my transfer stuck?
globus-console --errors                      # faulting, paused or stuck
globus-console --max-rate 0                  # stalled: no bytes moving
globus-console watch -n 5 --errors           # a live problem board
globus-console --format json | jq '.[] | select(.faults > 0) | .task_id'
```

Filesystem commands walk the tree with one API call per directory, since the
Transfer API has no recursive listing. They run a bounded thread pool and
report progress. Listing is metadata only, so on tape-backed storage `du` and
`find` read sizes **without recalling anything**.

## Collection shortcuts

So that nobody has to look up a UUID for the collections everyone uses. An
administrator ships a file of `name = uuid` pairs and everyone gets friendly
names with no per-user setup:

```sh
globus-fs ls dataden:/umms-mylab
globus-fs collections                   # what names exist here
```

### Format

```ini
# NAME = COLLECTION_UUID[:/optional/base/path]

dataden = ab65757f-00f5-4e5b-aa21-133187732a01
turbo   = 8e3a1e59-0000-0000-0000-000000000000
scratch = 1a2b3c4d-0000-0000-0000-000000000000:/scratch
```

`NAME: UUID` and bare `NAME UUID` also parse, `#` starts a comment, and a
file beginning with `{` is read as a JSON object of the same pairs. A
malformed line is skipped rather than fatal — one typo must not break the
tool for everyone on a shared machine. Names are typed on a command line, so
no spaces and no colons.

### Where they are read from

Later files win, so a user can override what an administrator ships:

| path | for |
| --- | --- |
| `/etc/globus-fs/collections.conf` | everyone on the machine |
| `$GLOBUS_FS_COLLECTIONS` | `:`-separated list of files, for Lmod |
| `~/.globus-fs/collections.conf` | one user's own additions |

```lua
-- in an Lmod module file
setenv("GLOBUS_FS_COLLECTIONS", "/sw/arcts/globus/collections.conf")
```

Start from [`examples/collections.conf`](examples/collections.conf).
`globus-fs collections` lists what resolved and which file each name
came from. With nothing configured it prints every place it looked -- the
environment variable included, even when unset -- and points a single user at
`bookmark create` instead, which is usually what they want.

A shortcut with a base path behaves like a bookmark — `scratch:mylab` is
`mylab` underneath it. **A personal Globus bookmark of the same name wins**,
the way user config beats `/etc` everywhere else;
`collections --check-bookmarks` flags a name being shadowed.

## Credential security

`~/.globus-fs/tokens.json` and `~/.globus-console/tokens.json` hold a Globus **refresh token** — a
long-lived credential that can be exchanged for access tokens until it is
revoked. On a shared login node or DTN, a readable token file lets any local
user act as its owner, and for an activity monitor that means reading every
transfer on the subscription.

So the store is checked before it is touched, and the tool **refuses to run**
on an exposed credential, the way `ssh` refuses an exposed private key:

```
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@           WARNING: UNPROTECTED GLOBUS CREDENTIAL FILE!           @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
Permissions 0640 for '~/.globus-fs/tokens.json' are too open.
...
Refusing to run.

Fix it with:
    chmod 0600 ~/.globus-fs/tokens.json
```

What is enforced, on both the directory and every file in it:

| check | why |
| --- | --- |
| no group or other permission bits (`0077`) | anything else exposes the token to local users |
| owned by the current uid | never read or write another user's credentials |
| not a symbolic link | a link could redirect the write, or point at a file someone else controls |
| regular file / real directory | same |

`globus-fs login --fix-permissions` (or `globus-console login --fix-permissions` repairs the modes of paths you own,
instead of refusing. It never changes ownership, so a store owned by someone
else still fails — as it should.

Why this is not already handled: globus-sdk writes *new* token files under a
user-only umask, but it does not repair a file that already exists with looser
permissions, and `os.makedirs(..., exist_ok=True)` silently ignores its `mode`
argument for a directory that is already there. Either path can leave a
credential exposed with nothing complaining.

**Out of scope**, and worth knowing:

- If `~` is on NFS, root on any client can read the token regardless of mode.
  Consider `GLOBUS_FS_HOME` / `GLOBUS_CONSOLE_HOME` pointing at local disk on shared systems.
- `GLOBUS_CONSOLE_CLIENT_SECRET` lives in the environment, so it is visible
  in `/proc/<pid>/environ` to you and to root. Prefer a systemd
  `EnvironmentFile` with mode `0600` over exporting it in a shell profile.
- Revoke a leaked token at <https://app.globus.org/settings/consents>;
  `chmod` alone does not invalidate anything already copied.

## Layout

```
pyproject.toml                      two console scripts, one package
src/globus_console_cli/
    console.py                      globus-console: the Activity view
    fs.py                           globus-fs: ls/find/du, bookmarks, shortcuts
    tasks.py                        the task table both tools draw
    common.py                       login, credential store, formatting
    security.py                     the credential permission guard
    shortcuts.py                    collections.conf parsing
    walk.py                         the parallel directory walker
```

`tasks.py` is shared because the table is identical; which API fills it is
fixed by the tool that registers the command, never by a user flag, so
nothing in `globus-fs` can reach `endpoint_manager`.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
pre-commit install
```

`tox.ini` carries the flake8, isort and pytest settings and `.bandit` the
bandit exclusions, matching archivetar and ltfsee-globus. Every module,
class, function and method carries a docstring.

## Tests

```sh
.venv/bin/pytest                      # with coverage, per tox.ini
.venv/bin/pytest --no-cov -q          # what the pre-commit hook runs
.venv/bin/pytest tests/test_security.py -v
.venv/bin/pytest -k "bookmark or maxdepth"
```

None of the tests touch the network or your real credential store —
`tests/conftest.py` patches `Tool.transfer_client` and serves a fake
collection tree. `run` drives `globus-fs`, `run_console` drives
`globus-console`.

| file | covers |
| --- | --- |
| `tests/test_security.py` | the credential guard: modes, ownership, symlinks, repair |
| `tests/test_client_id.py` | client ID precedence and the built-in default |
| `tests/test_format.py` | byte/duration formatting, timestamps, the ISSUE column |
| `tests/test_tasks.py` | sorting, filters, truncation, and which API each tool calls |
| `tests/test_fs.py` | `ls`, `du` rollups, `find` filters, API call counts |
| `tests/test_bookmarks.py` | bookmark CRUD and target resolution |
| `tests/test_collections.py` | site shortcut parsing, precedence and resolution |
| `tests/test_docs.py` | that USAGE.md documents every option of both tools |

The `du` and `find` tests assert on **how many listings a command makes**,
since one API call per directory is the cost model users actually feel.
`test_tasks.py` asserts that `globus-fs` only ever calls `task_list` and
`globus-console` only `endpoint_manager_task_list`.

## Troubleshooting

- **`Permission denied (PermissionDenied)` from globus-console** — no
  activity role on any subscribed collection. `globus-console endpoints`
  lists what you can see; `globus-fs tasks` shows your own transfers
  regardless.
- **Empty output** — no tasks are running on the collections you monitor.
  Check with `globus-console --status ACTIVE,INACTIVE,SUCCEEDED,FAILED -e <uuid>`.
- **`Not authenticated`** — `globus-fs login` or `globus-console login`
  (`--force` after changing scopes or roles).
