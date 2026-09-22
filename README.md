# globus-activity

A greppable, one-line-per-task view of the Globus web console **Activity**
tab, plus `ls`, `find` and `du` for collections that are only reachable over
Globus.

It calls the same Transfer API the console does, so you see every task on the
collections where you hold an activity role — not just your own.

```
$ globus-activity --errors
TASK_ID   STATUS    ISSUE                OWNER            RATE      FILES  BYTES  FLT    AGE  SOURCE               DEST
0f3a91c2  ACTIVE    PERMISSION_DENIED    user2@umich.edu  0B/s   600/2400  15.0G   14  1h14m  Great Lakes scratch  Turbo Research Storage
7b1e4d80  INACTIVE  EXPIRED_CREDENTIALS  user3@umich.edu  0B/s   900/3600  22.5G    3  1h51m  Great Lakes scratch  Turbo Research Storage
```

```
$ globus-activity du dataden:/umms-mylab -d 1
  SIZE  FILES  DIRS  PATH
104.1G      7     4  /umms-mylab
100.0G      1     1  /umms-mylab/backups
  4.1G      5     1  /umms-mylab/fastq
```

**[USAGE.md](USAGE.md) is the full reference** — every command, every option,
with examples. This page covers what it is, who can use it, and how to set it
up.

## Who can use it

| | what they see |
| --- | --- |
| `activity_monitor` / `activity_manager` on a **subscribed** collection | every task on those collections — the full Activity tab |
| no activity role | **403 on every admin command**; use `--mine` for their own tasks |

The admin view is not "your tasks plus more" — it is a different API. A user
with no activity role gets `PermissionDenied` rather than an empty table, and
owning a guest collection does **not** by itself grant the role; it has to be
assigned explicitly, and the collection must be subscribed.

So this is safe to hand to anyone: delegated monitors (a PI or lab data
steward given `activity_monitor` on their project collection) get exactly
their collections, and everyone else can still run `--mine`.

```
$ globus-activity --errors
TASK_ID   STATUS    ISSUE                OWNER            RATE      FILES  BYTES  FLT    AGE  SOURCE                 DEST
0f3a91c2  ACTIVE    PERMISSION_DENIED    user2@umich.edu  0B/s   600/2400  15.0G   14  1h14m  Great Lakes scratch    Turbo Research Storage
7b1e4d80  INACTIVE  EXPIRED_CREDENTIALS  user3@umich.edu  0B/s   900/3600  22.5G    3  1h51m  Great Lakes scratch    Turbo Research Storage
```

## Setup

### 1. Register a Globus client (once)

1. Open <https://app.globus.org/settings/developers>
2. Choose **Register a thick client or script that will be installed and run by
   users on their devices**
3. Pick or create a project, name it something like `campus globus-activity`
4. Copy the **Client UUID**

### 2. Log in

```sh
./globus-activity login --client-id <UUID>
```

The client ID is saved to `~/.globus-activity/config.json`; refresh tokens go in
`~/.globus-activity/tokens.json` (mode 0600), so you only log in once.

Put it on your `PATH` if you like:

```sh
ln -s "$PWD/globus-activity" ~/bin/globus-activity
```

### Unattended use (cron, dashboards)

Register a **service account / application credential** instead, grant that
client's identity the `activity_monitor` role on your endpoints, and export:

```sh
export GLOBUS_ACTIVITY_CLIENT_ID=<UUID>
export GLOBUS_ACTIVITY_CLIENT_SECRET=<secret>
```

No interactive login is needed in that mode.

## What it does

| command | |
| --- | --- |
| `globus-activity` / `tasks` | in-progress transfers, one line each |
| `globus-activity watch` | the same table, refreshed, with a live rate column |
| `globus-activity history` | completed tasks |
| `globus-activity task <id>` | one task in full, `--events` for the error log |
| `globus-activity endpoints` | collections you are allowed to monitor |
| `globus-activity ls` | list a directory on a collection |
| `globus-activity find` | recursive search, with size and age filters |
| `globus-activity du` | sizes and file counts per directory |
| `globus-activity collections` | site-wide collection shortcuts |
| `globus-activity bookmark` | your own shortcuts, shared with globus-cli |
| `globus-activity consent` | unlock a GCS v5 mapped collection for listing |
| `globus-activity login` / `logout` / `whoami` | credentials |

A few things it is good at:

```sh
globus-activity --errors                      # faulting, paused or stuck
globus-activity --max-rate 0                  # stalled: no bytes moving
globus-activity watch -n 5 --errors           # a live problem board
globus-activity --mine                        # your own transfers, no role needed
globus-activity find dataden:/lab --older 365d --min-size 1G
globus-activity du dataden:/lab -d 1
globus-activity --format json | jq '.[] | select(.faults > 0) | .task_id'
```

Filesystem commands walk the tree with one API call per directory, since the
Transfer API has no recursive listing. They run a bounded thread pool and
report progress. Listing is metadata only, so on tape-backed storage `du` and
`find` read sizes **without recalling anything**.

See [USAGE.md](USAGE.md) for every option.

## Collection shortcuts

So that nobody has to look up a UUID for the collections everyone uses. An
administrator ships a file of `name = uuid` pairs and everyone gets friendly
names with no per-user setup:

```sh
globus-activity ls dataden:/umms-mylab
globus-activity collections                   # what names exist here
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
| `/etc/globus-activity/collections.conf` | everyone on the machine |
| `$GLOBUS_ACTIVITY_COLLECTIONS` | `:`-separated list of files, for Lmod |
| `~/.globus-activity/collections.conf` | one user's own additions |

```lua
-- in an Lmod module file
setenv("GLOBUS_ACTIVITY_COLLECTIONS", "/sw/arcts/globus/collections.conf")
```

Start from [`examples/collections.conf`](examples/collections.conf).
`globus-activity collections` lists what resolved and which file each name
came from. With nothing configured it prints every place it looked -- the
environment variable included, even when unset -- and points a single user at
`bookmark create` instead, which is usually what they want.

A shortcut with a base path behaves like a bookmark — `scratch:mylab` is
`mylab` underneath it. **A personal Globus bookmark of the same name wins**,
the way user config beats `/etc` everywhere else;
`collections --check-bookmarks` flags a name being shadowed.

## Requirements / layout

- Python 3.9+ and `globus-sdk` (pinned in `requirements.txt`, installed into
  `.venv/` here)
- `globus-activity` — launcher that uses that venv
- `globus_activity.py` — the whole tool

To rebuild the virtualenv from scratch:

```sh
rm -rf .venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Credential security

`~/.globus-activity/tokens.json` holds a Globus **refresh token** — a
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
Permissions 0640 for '~/.globus-activity/tokens.json' are too open.
...
Refusing to run.

Fix it with:
    chmod 0600 ~/.globus-activity/tokens.json
```

What is enforced, on both the directory and every file in it:

| check | why |
| --- | --- |
| no group or other permission bits (`0077`) | anything else exposes the token to local users |
| owned by the current uid | never read or write another user's credentials |
| not a symbolic link | a link could redirect the write, or point at a file someone else controls |
| regular file / real directory | same |

`globus-activity login --fix-permissions` repairs the modes of paths you own,
instead of refusing. It never changes ownership, so a store owned by someone
else still fails — as it should.

Why this is not already handled: globus-sdk writes *new* token files under a
user-only umask, but it does not repair a file that already exists with looser
permissions, and `os.makedirs(..., exist_ok=True)` silently ignores its `mode`
argument for a directory that is already there. Either path can leave a
credential exposed with nothing complaining.

**Out of scope**, and worth knowing:

- If `~` is on NFS, root on any client can read the token regardless of mode.
  Consider `GLOBUS_ACTIVITY_HOME` pointing at local disk on shared systems.
- `GLOBUS_ACTIVITY_CLIENT_SECRET` lives in the environment, so it is visible
  in `/proc/<pid>/environ` to you and to root. Prefer a systemd
  `EnvironmentFile` with mode `0600` over exporting it in a shell profile.
- Revoke a leaked token at <https://app.globus.org/settings/consents>;
  `chmod` alone does not invalidate anything already copied.

## Development

```sh
.venv/bin/pip install -r requirements-dev.txt
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

149 tests, none of which touch the network or your real credential store —
`tests/conftest.py` monkeypatches `transfer_client()` and serves a fake
collection tree.

| file | covers |
| --- | --- |
| `tests/test_security.py` | the credential guard: modes, ownership, symlinks, repair |
| `tests/test_format.py` | byte/duration formatting, timestamps, the ISSUE column |
| `tests/test_tasks.py` | sorting, filters, column truncation, admin vs `--mine` |
| `tests/test_fs.py` | `ls`, `du` rollups, `find` filters, API call counts |
| `tests/test_bookmarks.py` | bookmark CRUD and target resolution |
| `tests/test_collections.py` | site shortcut parsing, precedence and resolution |
| `tests/test_docs.py` | that USAGE.md still documents every option |

The `du` and `find` tests assert on **how many listings a command makes**,
since one API call per directory is the cost model users actually feel. And
`test_docs.py` fails when an option exists in the parser but not in
[USAGE.md](USAGE.md), so the reference cannot quietly fall behind.

## Troubleshooting

- **`Permission denied (PermissionDenied)`** — you do not hold
  `activity_monitor`/`activity_manager` on a subscribed collection.
  `globus-activity endpoints` lists what you can actually see;
  `globus-activity --mine` works regardless of role.
- **Empty output** — no tasks are running on the endpoints you monitor. Check
  with `globus-activity --status ACTIVE,INACTIVE,SUCCEEDED,FAILED -e <uuid>`.
- **`Not authenticated`** — `globus-activity login` (or `login --force` after
  changing scopes/roles).
