# globus-activity — usage reference

Every command and option, with examples. For what the tool is and how to
install it, see the [README](README.md).

```
globus-activity [COMMAND] [OPTIONS]
```

With no command, `tasks` runs — so `globus-activity --errors` and
`globus-activity tasks --errors` are the same thing.

---

## Targets

Anywhere a command takes a `TARGET` (`ls`, `find`, `du`) it accepts three
forms:

| form | meaning |
| --- | --- |
| `1234abcd-...:/path` | a collection UUID and an absolute path |
| `name:/path` | a collection shortcut or bookmark, plus a path |
| `name` | that shortcut or bookmark's own location |

A path written after a **name** is resolved *beneath* it, because a shortcut
or bookmark is a starting directory:

```sh
globus-activity ls dataden:/umms-mylab     # shortcut rooted at /  -> /umms-mylab
globus-activity ls scratch:mylab           # shortcut rooted at /scratch -> /scratch/mylab
globus-activity ls projects:alpha          # bookmark at /data/    -> /data/alpha
```

Names resolve in this order: **your own bookmark**, then a **site collection
shortcut**. A UUID is always taken literally.

---

## Collection shortcuts

So that nobody has to look up a UUID for the collections everyone uses.

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

### Format

```ini
# NAME = COLLECTION_UUID[:/optional/base/path]

dataden = ab65757f-00f5-4e5b-aa21-133187732a01
turbo   = 8e3a1e59-0000-0000-0000-000000000000
scratch = 1a2b3c4d-0000-0000-0000-000000000000:/scratch
```

- `NAME: UUID` and bare `NAME UUID` also parse
- `#` starts a comment; blank lines are ignored
- a file beginning with `{` is read as a JSON object of the same pairs
- a malformed line is **skipped, not fatal** — one typo must not break the
  tool for everyone on a shared machine
- names are typed on a command line, so no spaces and no colons

A copy to start from: [`examples/collections.conf`](examples/collections.conf).

### `collections`

```sh
globus-activity collections                    # what names exist here
globus-activity collections --check-bookmarks  # flag names your bookmarks shadow
globus-activity collections --format json
```

| option | |
| --- | --- |
| `--check-bookmarks` | mark names shadowed by one of your own bookmarks (makes an API call) |

With nothing configured it prints the paths it looked in, so you know where
to put the file.

---

## Shared options

Accepted by most commands.

| option | |
| --- | --- |
| `--format table\|tsv\|csv\|json` | output format; `table` by default |
| `--no-header` | omit the header row |
| `--client-id UUID` | authenticate as a different registered client |

Task listings (`tasks`, `watch`, `history`) add:

| option | |
| --- | --- |
| `--fields a,b,c` | choose columns — see [Fields](#fields) |
| `--wide` | every useful column |
| `--width N` | width to fit; `0` disables truncation |
| `--no-truncate` | never shorten a value |
| `--sort FIELD[,FIELD]` | `-` prefix for descending, e.g. `--sort=-bytes` |
| `--summary` | aggregate line after the table |
| `--limit N` | stop fetching after N tasks (before sorting) |

Long values are only shortened when writing to a terminal, so piping to
`grep` or `awk` always gives you the full text.

---

## `tasks` — the Activity view

The default command. Shows in-progress transfers across every collection you
monitor.

```sh
globus-activity                              # ACTIVE + INACTIVE
globus-activity --errors                     # only the unhappy ones
globus-activity --mine                       # your own transfers, no role needed
globus-activity --summary --sort=-bytes
```

Server-side filters, applied by the Transfer API:

| option | |
| --- | --- |
| `--status ACTIVE,INACTIVE` | any task status; in-progress pair by default |
| `-e, --endpoint UUID[,UUID]` | restrict to collections, as source or destination |
| `--endpoint-use source\|destination` | with `--endpoint`, only that role |
| `-u, --user NAME_OR_UUID` | the submitting Globus identity |
| `--local-user NAME` | the local account the transfer mapped to |
| `--min-faults N` | tasks that have faulted at least N times |
| `--paused` / `--not-paused` | held by a pause rule, or not |
| `--mine` | your own tasks via `GET /task_list`; needs no role |

Client-side filters, applied after fetching:

| option | |
| --- | --- |
| `--errors` | faulting, paused, or a bad `nice_status` |
| `--min-rate BYTES` | at or above this rate |
| `--max-rate BYTES` | at or below it — `--max-rate 0` finds stalls |
| `--older-than MINUTES` | submitted at least this long ago |

`--mine` drops the `OWNER` column and ignores the admin-only server-side
filters, warning when you pass one.

---

## `watch` — the same table, refreshed

```sh
globus-activity watch -n 5                   # refresh every 5s
globus-activity watch --errors --summary
globus-activity watch --no-clear | tee -a activity.log
```

| option | |
| --- | --- |
| `-n, --interval SECONDS` | refresh interval; 10 by default |
| `--no-clear` | append instead of clearing the screen |

Plus every `tasks` option. Watch mode adds a **`CUR`** column: an
instantaneous rate computed from the change in `bytes_transferred` between
polls. The API only ever reports an average since submission, so this is the
number to read when you want "right now".

---

## `history` — completed tasks

```sh
globus-activity history -e <uuid> --hours 48
globus-activity history -e <uuid> --status FAILED --summary
globus-activity history -e all --hours 6     # every collection you monitor
globus-activity history --mine --hours 48    # yours; no --endpoint needed
```

| option | |
| --- | --- |
| `--hours N` | look back this far; 24 by default |
| `--status SUCCEEDED,FAILED` | which completed statuses |

Plus every `tasks` option. The **admin view requires `--endpoint`**: the
Transfer API only returns completed admin-visible tasks per collection.
`--endpoint all` loops over everything you monitor. That restriction does not
apply to `--mine`.

---

## `task` — one task in full

```sh
globus-activity task 0f3a91c2-...
globus-activity task 0f3a91c2-... --events -v
globus-activity task 0f3a91c2-... --mine
```

| option | |
| --- | --- |
| `--events` | also print the event log |
| `--events-limit N` | how many events; 50 by default |
| `-v, --verbose` | include the raw server message on each event |
| `--mine` | read it as your own task, which needs no role |

`--events` is usually the fastest way to answer "why is this stuck?". On a
403 the command automatically retries as the user view, so your own tasks
work without the flag.

---

## `endpoints` — what you can monitor

```sh
globus-activity endpoints
globus-activity endpoints --format json
```

No options beyond the shared ones. Run this first if `tasks` returns
`PermissionDenied` — it shows exactly which collections your role covers.

---

## `ls` — list a directory

```sh
globus-activity ls dataden:/umms-mylab
globus-activity ls -l dataden:/umms-mylab
globus-activity ls -lS dataden:/lab          # largest first
globus-activity ls -lt dataden:/lab          # newest first
globus-activity ls -lR dataden:/lab          # recursive
```

| option | |
| --- | --- |
| `-l, --long` | mode, owner, group, size, mtime |
| `-R, --recursive` | descend into subdirectories |
| `-1, --one` | one name per line even on a terminal |
| `-F, --classify` | append `/` to directory names |
| `-S` | sort by size, largest first |
| `-t` | sort by modification time, newest first |
| `-r, --reverse` | reverse the order |
| `-b, --bytes` | raw bytes instead of human units |
| `--no-hidden` | skip dotfiles |
| `--local-user NAME` | map to this local account (GCSv5 mapped collections) |
| `--width N` | terminal width to lay columns out for |
| `-j, --jobs N` | concurrent listings with `-R`; 8 by default |
| `--maxdepth N` | with `-R`, how deep to go |
| `--max-errors N` | unlistable directories to report; 10 by default |

Behaves like `ls`: multi-column on a terminal, one name per line when piped.
Dotfiles are shown by default — that is the Globus API's behaviour and
usually what an admin wants.

**One directory is one API call.** `-R` costs one per directory.

---

## `find` — search recursively

```sh
globus-activity find dataden:/lab --name '*.fastq.gz'
globus-activity find dataden:/lab --type f --min-size 100M --long
globus-activity find dataden:/lab --older 365d --min-size 1G --summary
globus-activity find dataden:/lab --maxdepth 1 --long
globus-activity find dataden:/lab --name '*.tmp' -0 | xargs -0 -n1 echo
```

| option | |
| --- | --- |
| `--name GLOB` | match the file name, e.g. `'*.fastq.gz'` |
| `--iname GLOB` | same, case insensitive |
| `--type f\|d` | files or directories only |
| `--min-size SIZE` | at least this big — `100M`, `2G`, `500Ki` |
| `--max-size SIZE` | at most this big |
| `--newer AGE` | modified since — `7d`, `12h`, or an ISO date |
| `--older AGE` | modified before that |
| `--maxdepth N` | how many levels below the target to look |
| `--limit N` | stop after N matches |
| `-l, --long` | show type, size and mtime |
| `-0, --print0` | NUL separated, for `xargs -0` |
| `--summary` | count and total size, to stderr |
| `-j, --jobs N` | concurrent listings; 8 by default |
| `--no-hidden`, `--local-user`, `--max-errors` | as for `ls` |

Output **streams as directories come back**, so ordering is not
deterministic — pipe through `sort` if you need it stable.

The size and age filters are the ones `globus ls --filter` cannot express;
it matches file names only.

---

## `du` — sizes and file counts

```sh
globus-activity du dataden:/lab              # whole tree, biggest first
globus-activity du dataden:/lab -d 1         # like du -d 1 -h
globus-activity du dataden:/lab -d 2 --sort files
globus-activity du dataden:/lab --bytes --format tsv
```

```
  SIZE  FILES  DIRS  PATH
104.1G      7     4  /data
100.0G      1     1  /data/project-b
  4.1G      5     1  /data/project-a
```

| option | |
| --- | --- |
| `-d, --depth N` | how deep to **print**, like `du -d` |
| `--maxdepth N` | how deep to **walk** — changes the totals |
| `-b, --bytes` | raw bytes instead of human units |
| `--sort size\|files\|path` | ordering; `size` by default |
| `--summary` | directories scanned and elapsed time, to stderr |
| `-j, --jobs N` | concurrent listings; 8 by default |
| `--no-hidden`, `--local-user`, `--max-errors` | as for `ls` |

`FILES` and `DIRS` are cumulative, so one walk answers "how big" and "how
many files" together.

Listing is metadata only, so on tape-backed storage this reads sizes
**without recalling anything**.

---

## `bookmark` — your own shortcuts

Server-side Globus bookmarks, shared with `globus bookmark` and the web app.
Names and argument order match globus-cli.

```sh
globus-activity bookmark create 1234abcd-...:/projects/ projects
globus-activity bookmark list
globus-activity bookmark show projects            # ENDPOINT_ID:PATH
globus-activity bookmark show projects -v         # all fields
globus-activity bookmark rename projects proj2
globus-activity bookmark delete proj2
```

| subcommand | arguments |
| --- | --- |
| `list` | — |
| `show` | `BOOKMARK_ID_OR_NAME`, `-v/--verbose` |
| `create` | `ENDPOINT_ID:PATH BOOKMARK_NAME` |
| `rename` | `BOOKMARK_ID_OR_NAME NEW_BOOKMARK_NAME` |
| `delete` | `BOOKMARK_ID_OR_NAME` |

`show` prints the substitution-friendly form by default:

```sh
globus ls "$(globus-activity bookmark show projects)"
```

A bookmark takes precedence over a collection shortcut of the same name.

---

## `consent` — unlock a mapped collection

```sh
globus-activity consent <collection-uuid>
```

GCS v5 mapped collections need a `data_access` consent before they can be
listed. Run this once if `ls`, `find` or `du` fails with `ConsentRequired`.

Does **not** apply to guest collections, GCP, or High Assurance collections —
those use a different consent path and `data_access` on them fails with
"Unknown Scope".

---

## `login`, `logout`, `whoami`

```sh
globus-activity login --client-id <UUID>     # first time; saves the client id
globus-activity login --force                # re-run the flow
globus-activity login --fix-permissions      # repair an exposed token store
globus-activity whoami
globus-activity logout
```

| option | |
| --- | --- |
| `--client-id UUID` | the registered Globus client to authenticate as |
| `--force` | run the login flow again |
| `--fix-permissions` | chmod the credential store instead of refusing to run |

The tool refuses to run if `~/.globus-activity/` or the token file inside it
is readable by other users — see [credential security](README.md#credential-security).

---

## Fields

For `--fields` and `--sort` on task listings:

| | |
| --- | --- |
| `id`, `fullid` | task id, short or complete |
| `status` | `ACTIVE`, `INACTIVE`, `SUCCEEDED`, `FAILED` |
| `issue` | `PAUSED`, or the `nice_status`, or `-` when healthy |
| `reason` | the longer description behind `issue` |
| `owner` | who submitted it |
| `type` | `TRANSFER` or `DELETE` |
| `rate` | average since submission (`effective_bytes_per_second`) |
| `cur` | instantaneous rate; `watch` only |
| `bytes` | bytes transferred so far |
| `files` | `transferred/total`, with `+Ns` for skipped files |
| `pct` | percent complete, by file count |
| `faults` | fault count |
| `age`, `start` | time since submission, and the timestamp |
| `done`, `deadline` | completion time, and time left |
| `src`, `dst` | source and destination display names |
| `srcid`, `dstid` | their collection UUIDs |
| `srcuser`, `dstuser` | the local accounts mapped to |
| `label` | the user's label for the task |
| `cmd` | the submitting client |
| `paused`, `sync`, `enc` | pause state, sync level, encryption |

`issue` is never truncated, so `grep PERMISSION_DENIED` always works.

---

## Piping

```sh
globus-activity | grep -i turbo
globus-activity --format tsv | awk -F'\t' '$5=="0B/s"'
globus-activity --format json | jq '.[] | select(.faults > 0) | .task_id'
globus-activity du dataden:/lab --format tsv | sort -k2 -n
globus-activity find dataden:/lab --name '*.tmp' -0 | xargs -0 rm-something
```
