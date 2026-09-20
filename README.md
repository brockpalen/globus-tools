# globus-activity

A greppable, one-line-per-task view of the Globus web console **Activity** tab,
for subscription / endpoint administrators.

It calls the same Transfer API that the console does
(`GET /endpoint_manager/task_list` and friends), so you see every task on the
collections where you hold an activity role — not just your own tasks.

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

## Commands

| Command | What it does |
| --- | --- |
| `globus-activity` / `tasks` | in-progress transfers (`ACTIVE` + `INACTIVE`), one per line |
| `globus-activity watch` | same table, refreshed on an interval, with a live rate column |
| `globus-activity history` | completed tasks for an endpoint (needs `--endpoint`) |
| `globus-activity task <id>` | full task document, `--events` for the error log |
| `globus-activity endpoints` | endpoints/collections you are allowed to monitor |
| `globus-activity ls` | list a directory on a collection |
| `globus-activity find` | recursive search over a collection |
| `globus-activity du` | sizes and file counts per directory |
| `globus-activity bookmark` | named collection+path shortcuts (shared with globus-cli) |
| `globus-activity consent` | grant a mapped collection's data_access consent |
| `globus-activity login` / `logout` / `whoami` | credentials |

Add `--mine` to `ls`, `watch` or `history` to read `GET /task_list` — your own
tasks, no role required:

```sh
globus-activity --mine                        # my in-progress transfers
globus-activity --mine --errors               # ...that are unhappy
globus-activity watch --mine -n 5
globus-activity history --mine --hours 48     # no --endpoint needed here
globus-activity task <id> --mine --events     # my own task, no role
```

`--mine` drops the `OWNER` column (they are all yours) and ignores the
admin-only server-side filters (`--user`, `--local-user`, `--min-faults`,
`--paused`), warning when you pass one. `--endpoint` still works, matched
locally. `task` falls back to the user view automatically on a 403, so
`globus-activity task <id>` works for your own tasks without the flag.

Any flag can be given without the `tasks` subcommand: `globus-activity
--errors` is the same as `globus-activity tasks --errors`.

## Filesystem helpers

For collections that are only reachable over Globus — no shell, no `ssh`, no
`find`. These walk the tree with `operation_ls`, which is **not recursive**:
one API call per directory. A tree with 50,000 directories is 50,000 round
trips, so they run a bounded thread pool (`-j`, default 8) and print progress
to stderr.

Listing is metadata only, so on an HSM-backed collection like Data Den these
read sizes **without recalling anything from tape**.

### Bookmarks

Targets are `COLLECTION_UUID:/path` or a **bookmark name**. Bookmarks are the
real server-side Globus bookmarks — the same ones `globus bookmark` and the web
app use — so a name saved in any of them works in all of them. The commands
and argument order match globus-cli deliberately:

```sh
globus-activity bookmark create 1234abcd-...:/projects/ projects
globus-activity bookmark list
globus-activity bookmark show projects            # ENDPOINT_ID:PATH
globus-activity bookmark show projects -v         # all fields
globus-activity bookmark rename projects proj2
globus-activity bookmark delete proj2
```

Then use the name anywhere a target is taken:

```sh
globus-activity ls projects
globus-activity du projects -d 1
globus-activity find projects --min-size 1G
```

A path after a bookmark name is **relative to the bookmark**, so
`projects:alpha/sub` is `alpha/sub` underneath wherever `projects` points.
A raw UUID keeps absolute-path semantics: `1234abcd-...:/projects/alpha`.

`bookmark show` prints `ENDPOINT_ID:PATH` by default, like globus-cli, so it
substitutes into other commands:

```sh
globus ls "$(globus-activity bookmark show projects)"
```

### ls

```sh
globus-activity ls projects              # names, in columns on a tty
globus-activity ls -l projects           # mode, owner, size, mtime
globus-activity ls -lS projects          # largest first
globus-activity ls -lt projects          # newest first
globus-activity ls -lR projects          # recursive
globus-activity ls -F projects           # mark directories with /
```

```
drwxr-xr-x  brockp  hpcstaff      0B  2026-09-01 12:00:00  project-a
-rw-r--r--  brockp  hpcstaff  100.0M  2026-09-01 12:00:00  file1.bin
```

Behaves the way `ls` does: multi-column when writing to a terminal, one name
per line when piped (`-1` forces that on a terminal too). Sizes are human
units by default, `-b` for raw bytes. `-r` reverses, `--format tsv|csv|json`
for machine use.

Unlike `ls`, dotfiles are shown by default — that is the Globus API's
behaviour and usually what an admin wants. `--no-hidden` skips them.

One directory is one API call. `-R` walks, so it costs one call per
directory and takes `-j`/`--maxdepth` like `find`.

### du

```sh
globus-activity du projects              # whole tree, by size
globus-activity du projects -d 1         # like du -d 1 -h
globus-activity du projects -d 2 --sort files
globus-activity du projects --bytes --format tsv
```

```
  SIZE  FILES  DIRS  PATH
104.1G      7     4  /data
100.0G      1     1  /data/project-b
  4.1G      5     1  /data/project-a
```

`FILES` and `DIRS` are cumulative counts, so this answers "how big" and "how
many files" in one walk — no separate count command needed. `-d/--depth`
limits what is *printed*; `--maxdepth` limits what is *walked* (and therefore
changes the totals, like `du -d` versus not descending at all).

### find

```sh
globus-activity find projects --name '*.fastq.gz'
globus-activity find projects --type f --min-size 100M --long
globus-activity find projects --older 365d --min-size 1G --summary
globus-activity find projects --maxdepth 1 --long        # ls, the long way
globus-activity find projects --name '*.tmp' -0 | xargs -0 -n1 echo
```

Filters: `--name`/`--iname` (globs), `--type f|d`, `--min-size`/`--max-size`
(`100M`, `2G`, `500Ki`), `--newer`/`--older` (`7d`, `12h`, or an ISO date),
`--maxdepth`, `--limit`.

Output streams as directories come back, so **ordering is not deterministic**
— the walk is concurrent. Pipe through `sort` if you need stable output.

### Consent

GCS v5 mapped collections need an extra `data_access` consent before they can
be listed. If a walk fails with `ConsentRequired`:

```sh
globus-activity consent <collection-uuid>
```

This does not apply to guest collections, GCP, or High Assurance collections
— those use a different consent path, and `data_access` on them fails with
"Unknown Scope".

### Cost

Every directory is an API call, so `du` on a large tree is slow and counts
against Globus rate limits. Raise `-j` carefully; the walk retries 429s and
5xxs with backoff. Directories that cannot be listed (permissions, dead
symlinks) are collected and reported at the end rather than aborting the
walk — a 40-minute walk should not die on its last directory.

## Filtering

Server-side (fast, done by the Transfer API):

```sh
globus-activity -e <collection-uuid>          # one collection (source or dest)
globus-activity -e <uuid> --endpoint-use destination
globus-activity -u jdoe@umich.edu             # by Globus identity (username or UUID)
globus-activity --local-user jdoe             # by the local POSIX account used
globus-activity --min-faults 5                # tasks that keep retrying
globus-activity --paused                      # tasks held by a pause rule
globus-activity --status ACTIVE               # exclude INACTIVE
```

Client-side conveniences:

```sh
globus-activity --errors                      # faulting, paused, or bad nice_status
globus-activity --max-rate 0                  # stalled: no bytes moving
globus-activity --min-rate 1e9                # the heavy hitters
globus-activity --older-than 120              # running for 2+ hours
globus-activity --summary                     # aggregate line under the table
```

And of course:

```sh
globus-activity | grep -i turbo
globus-activity --format tsv | awk -F'\t' '$5=="0B/s"'
globus-activity --format json | jq '.[] | select(.faults > 0) | .task_id'
```

## Output control

```sh
globus-activity --wide                        # every useful column
globus-activity --fields id,owner,rate,src,dst,label
globus-activity --sort=-bytes                 # '-' prefix = descending
globus-activity --format tsv --no-header      # machine friendly
```

Long values are shortened with `~` only when writing to a terminal; piping to
`grep`/`awk` gives you the untruncated text. `--width 0` or `--no-truncate`
forces full values on a terminal too.

Available fields:

`id`, `fullid`, `status`, `issue`, `reason`, `owner`, `type`, `rate`, `cur`,
`bytes`, `files`, `pct`, `faults`, `age`, `start`, `done`, `deadline`, `src`,
`dst`, `srcid`, `dstid`, `srcuser`, `dstuser`, `label`, `cmd`, `paused`,
`sync`, `enc`

Notes on a few of them:

- **`rate`** is `effective_bytes_per_second` — the task's average since it was
  submitted, which is what the console shows. It lags a rate change.
- **`cur`** is computed locally from the change in `bytes_transferred` between
  two polls, so it only has a value in `watch` mode (and is blank on the first
  refresh). This is the number to watch when you want "right now".
- **`issue`** is `PAUSED`, the task's `nice_status`
  (`PERMISSION_DENIED`, `ENDPOINT_ERROR`, `CONNECT_FAILED`,
  `EXPIRED_CREDENTIALS`, `QUOTA_EXCEEDED`, ...), or `-` when healthy. It is
  never truncated, so `grep PERMISSION_DENIED` always works.
- **`pct`** is by file count, not bytes — Globus does not report total bytes
  for a running task.
- **`files`** reads `transferred/total`, with `+Ns` appended when files were
  skipped.

## Watching

```sh
globus-activity watch -n 5                    # refresh every 5s, clears screen
globus-activity watch --errors --summary
globus-activity watch --no-clear | tee -a activity.log
```

## History

The admin view only returns *completed* tasks per endpoint, so `history`
requires `--endpoint` (this restriction does not apply to `--mine`):

```sh
globus-activity history -e <uuid> --hours 48
globus-activity history -e <uuid> --status FAILED --summary
globus-activity history -e all --hours 6      # loops over every monitored endpoint
```

## Digging into one task

```sh
globus-activity task 0f3a91c2-... --events -v
```

`--events` is the admin event log: each fault with its code, description and
the raw server message (`-v`) — usually the fastest way to answer "why is this
user's transfer stuck?".

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
.venv/bin/isort globus_activity.py globus_fs.py globus_security.py
.venv/bin/black globus_activity.py globus_fs.py globus_security.py
```

Formatting settings live in `pyproject.toml` (black at 88 columns, isort with
the `black` profile). Every module, class, function and method carries a
docstring.

## Troubleshooting

- **`Permission denied (PermissionDenied)`** — you do not hold
  `activity_monitor`/`activity_manager` on a subscribed collection.
  `globus-activity endpoints` lists what you can actually see;
  `globus-activity --mine` works regardless of role.
- **Empty output** — no tasks are running on the endpoints you monitor. Check
  with `globus-activity --status ACTIVE,INACTIVE,SUCCEEDED,FAILED -e <uuid>`.
- **`Not authenticated`** — `globus-activity login` (or `login --force` after
  changing scopes/roles).
