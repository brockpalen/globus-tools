# Usage reference

Every command and option, with examples. For what the tools are and how to
install them, see the [README](README.md).

Two commands, split by who may use them:

| | for | needs |
| --- | --- | --- |
| [`globus-fs`](#globus-fs) | everyone: `ls`, `find`, `du`, bookmarks, your own transfers | nothing beyond a Globus login |
| [`globus-console`](#globus-console) | anyone with an activity role: the console Activity tab | `activity_monitor` or `activity_manager` on a subscribed collection — automatic on a guest collection you own |

---

# globus-fs

```
globus-fs COMMAND [OPTIONS]
```

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
globus-fs ls dataden:/umms-mylab     # shortcut rooted at /  -> /umms-mylab
globus-fs ls scratch:mylab           # shortcut rooted at /scratch -> /scratch/mylab
globus-fs ls projects:alpha          # bookmark at /data/    -> /data/alpha
```

Names resolve in this order: **your own bookmark**, then a **site collection
shortcut**. A UUID is always taken literally.

## Collection shortcuts

Names such as `dataden` come from your Globus bookmarks, from
`collections.conf` files and from `$GLOBUS_FS_SHORTCUTS`; the format and where they are read from are
in the [README](README.md#collection-shortcuts). `globus-fs collections`
lists what is configured here.

## Shared options

Accepted by most commands.

| option | |
| --- | --- |
| `--format table\|tsv\|csv\|json` | output format; `table` by default |
| `--no-header` | omit the header row |

## `ls` — list a directory

```sh
globus-fs ls dataden:/umms-mylab
globus-fs ls -l dataden:/umms-mylab
globus-fs ls -lS dataden:/lab          # largest first
globus-fs ls -lt dataden:/lab          # newest first
globus-fs ls -lR dataden:/lab          # recursive
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
Dotfiles are shown by default, which is the Globus API's behaviour.

**One directory is one API call.** `-R` costs one per directory.

## `find` — search recursively

```sh
globus-fs find dataden:/lab --name '*.fastq.gz'
globus-fs find dataden:/lab --type f --min-size 100M --long
globus-fs find dataden:/lab --older 365d --min-size 1G --summary
globus-fs find dataden:/lab --maxdepth 1 --long
globus-fs find dataden:/lab --name '*.tmp' -0 | xargs -0 -n1 echo
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
| `--batch` | quoted paths for `globus delete --batch` — see below |
| `--summary` | count and total size, to stderr |
| `-j, --jobs N` | concurrent listings; 8 by default |
| `--no-hidden`, `--local-user`, `--max-errors` | as for `ls` |

Output **streams as directories come back**, so ordering is not
deterministic — pipe through `sort` if you need it stable.

The size and age filters are the ones `globus ls --filter` cannot express;
it matches file names only.

### Deleting what `find` matches

globus-fs does not delete anything itself. `--batch` writes the matches in
the form `globus delete --batch` reads, and globus-cli submits the delete
task. Write to a file, read it, then submit:

```sh
globus-fs find dataden:/lab --older 2y --name '*.tmp' --type f --batch > rm.txt
less rm.txt
globus delete --no-enable-globs --batch rm.txt <collection-uuid>
```

The file opens with comment lines naming the collection and giving the exact
`globus delete` command; globus-cli skips them.

```
# Paths from:  globus-fs find dataden:/lab
# Collection:  ab65757f-00f5-4e5b-aa21-133187732a01
# Review, then submit with:
#   globus delete --no-enable-globs --batch FILE ab65757f-00f5-4e5b-aa21-133187732a01
'/lab/run 42/scratch.tmp'
/lab/old.tmp
```

- Paths are absolute and shell-quoted, so spaces, quotes, `#`, `*` and
  leading dashes survive globus-cli's parser exactly. This is tested
  against globus-cli's own batch reader.
- A name containing a line break cannot be written as one line. It is left
  out, and named on stderr, rather than risk deleting a different path.
- Use `--type f`. A matched directory fails to delete without `--recursive`,
  and `--recursive` removes everything beneath it, including files the
  search did not match.
- `--no-enable-globs` is already globus-cli's default for `delete`; it is
  spelled out so a literal `*` in a file name can never widen the delete.
- Piping straight into `globus delete --batch - <uuid>` works too, but
  skips the review step.

## `du` — sizes and file counts

```sh
globus-fs du dataden:/lab              # whole tree, biggest first
globus-fs du dataden:/lab -d 1         # like du -d 1 -h
globus-fs du dataden:/lab -d 2 --sort files
globus-fs du dataden:/lab --bytes --format tsv
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

Symbolic links are listed but never followed, as with local `du`: a link to
a directory would otherwise count its data twice, or loop.

## `archivescan` — how Data Den friendly is it?

Data Den is built for large files: it provisions 10,000 files per TB, and
small files belong on a cache such as Locker. `archivescan` counts how much
of a tree falls either side of a size threshold — the same report as
archivetar's `archivescan`, for storage reached only through Globus.

```sh
globus-fs archivescan dataden:/umms-mylab
globus-fs archivescan turbo:/mylab --size 1G
globus-fs archivescan turbo:/mylab --filter '*.tar'
GLOBUS_FS_ARCHIVESCAN_SIZE=10M globus-fs archivescan locker:/mylab
```

```
----- Results ------
Threshold: files larger than 100 MiB

Data Den Candidates:
Files: 3635
Size: 394.7 TiB

Cache (Locker) Candidates:
Files: 11029
Size: 1.3 TiB

Total Time 2.74 Seconds
Fraction Offline: 99.66008 %
```

| option | |
| --- | --- |
| `-s, --size SIZE` | files **larger** than this are Data Den candidates; equal or smaller go to cache. Default `100M` |
| `-f, --filter GLOB` | only count files whose **full path** matches, e.g. `'*.tar'` |
| `--maxdepth N` | how deep to walk |
| `--summary` | directories scanned, to stderr |
| `-j, --jobs N` | concurrent listings; 8 by default |
| `--no-hidden`, `--local-user`, `--max-errors` | as for `ls` |

**Setting the default size.** `$GLOBUS_FS_ARCHIVESCAN_SIZE` replaces the
`100M` default, for a site or a module where the migration threshold is
different; `--size` still wins:

```lua
-- Lmod module file, for a Data Den tier migrating at 10 MiB
setenv("GLOBUS_FS_ARCHIVESCAN_SIZE", "10M")
```

**Sizes use binary units**, parsed by
[humanfriendly](https://humanfriendly.readthedocs.io/): `100M`, `100MB` and
`100MiB` are all 104,857,600 bytes, the 100 MiB archivescan has always
meant. A bare number is bytes.

- **Fraction Offline** is Data Den bytes over all bytes — where the data
  *would* live, by size. It is not the current migrated state: that needs
  block counts, which the Globus API does not provide, so archivescan's
  `--current-state`, `--print-offline` and `--recall` have no equivalent here.
- Only regular files count. Symbolic links are skipped, and `.snapshot`
  directories are not walked — a snapshot is another full copy of the tree.
- `--format tsv` or `csv` prints a header and one row, so a loop builds one
  table across many directories:

```sh
for d in lab1 lab2 lab3; do
  globus-fs archivescan "turbo:/$d" --format tsv --no-header
done
```

## `collections` — site shortcuts

```sh
globus-fs collections                    # what names exist here
globus-fs collections --check-bookmarks  # flag names your bookmarks shadow
globus-fs collections --format json
```

| option | |
| --- | --- |
| `--check-bookmarks` | mark names shadowed by one of your own bookmarks (makes an API call) |

With nothing configured it prints every place it looked, the environment
variable included even when unset, and suggests `bookmark create` for a
single user who just wants one name.

## `bookmark` — your own shortcuts

Server-side Globus bookmarks, shared with `globus bookmark` and the web app.
Names and argument order match globus-cli.

```sh
globus-fs bookmark create 1234abcd-...:/projects/ projects
globus-fs bookmark list
globus-fs bookmark show projects            # ENDPOINT_ID:PATH
globus-fs bookmark show projects -v         # all fields
globus-fs bookmark rename projects proj2
globus-fs bookmark delete proj2
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
globus ls "$(globus-fs bookmark show projects)"
```

A bookmark takes precedence over a collection shortcut of the same name.

## Consent and high assurance logins

There is no separate command. When a collection needs more than your
current login — consent for a GCS v5 mapped collection, or a high assurance
session that is new, expired or needs a particular identity (e.g.
`umich.edu`) — `ls`, `find`, `du` and `archivescan` ask you to log in, then
carry on. Parallel listings that all hit an expired session share one login.

That needs a terminal. In a script or cron job the command stops and says
so; run it once interactively. See
[high assurance collections](README.md#high-assurance-collections-cui-phi).

## Your own transfers: `tasks`, `watch`, `history`, `task`

The same table `globus-console` draws, filled from the transfers **you**
submitted (`GET /task_list`, no role needed). The `OWNER` column is dropped
since it is always you.

```sh
globus-fs tasks                       # in progress
globus-fs tasks --errors              # only the unhappy ones
globus-fs watch -n 5                  # live, with a CUR rate column
globus-fs history --hours 48          # completed
globus-fs task 0f3a91c2-... --events  # one task, with its error log
```

Options are those of the [`globus-console` task commands](#tasks--the-activity-view),
minus the filters only the admin API can apply (`--user`, `--local-user`,
`--min-faults`, `--endpoint-use`, `--paused`). `-e/--endpoint` still works,
as a filter over your own tasks, and `history` does not require it.

## `login`, `logout`, `whoami`

```sh
globus-fs login                       # once
globus-fs login --force               # re-run the flow
globus-fs whoami
globus-fs logout                      # revokes the tokens with Globus
```

| option | |
| --- | --- |
| `--force` | run the login flow again |

Tokens are kept by globus-sdk in
`~/.globus/app/<client-id>/globus-fs/tokens.json`, mode 600. A token file
other users can read is tightened to 600 with a warning — see
[credential security](README.md#credential-security). Set
`GLOBUS_FS_CLIENT_ID` to log in through a different client registration.

---

# globus-console

```
globus-console [COMMAND] [OPTIONS]
```

With no command, `tasks` runs — so `globus-console --errors` and
`globus-console tasks --errors` are the same thing.

Every command reads the Transfer API's `endpoint_manager` resources, which
answer **403 PermissionDenied** to anyone without `activity_monitor` or
`activity_manager` on a subscribed collection.

Who has those roles:

- subscription and endpoint administrators, on everything they administer
- **the owner of a guest collection, on that collection** — an owner always
  holds an effective `administrator` role, which implies both activity roles
- anyone granted a role on the collection's **Roles** tab in the web app;
  a guest collection's owner can grant `activity_monitor` to a lab manager
- anyone with a role on an endpoint or mapped collection, which carries down
  to every guest collection beneath it

Shared options (`--format`, `--no-header`) are as for
[globus-fs](#shared-options). Task listings (`tasks`, `watch`, `history`) add:

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

## `tasks` — the Activity view

The default command. Shows in-progress transfers across every collection you
monitor.

```sh
globus-console                              # ACTIVE + INACTIVE
globus-console --errors                     # only the unhappy ones
globus-console --summary --sort=-bytes
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

Client-side filters, applied after fetching:

| option | |
| --- | --- |
| `--errors` | faulting, paused, or a bad `nice_status` |
| `--min-rate BYTES` | at or above this rate |
| `--max-rate BYTES` | at or below it — `--max-rate 0` finds stalls |
| `--older-than MINUTES` | submitted at least this long ago |

## `watch` — the same table, refreshed

```sh
globus-console watch -n 5                   # refresh every 5s
globus-console watch --errors --summary
globus-console watch --no-clear | tee -a activity.log
```

| option | |
| --- | --- |
| `-n, --interval SECONDS` | refresh interval; 10 by default |
| `--no-clear` | append instead of clearing the screen |

Plus every `tasks` option. Watch mode adds a **`CUR`** column: an
instantaneous rate computed from the change in `bytes_transferred` between
polls. The API only ever reports an average since submission, so this is the
number to read when you want "right now".

## `history` — completed tasks

```sh
globus-console history -e <uuid> --hours 48
globus-console history -e <uuid> --status FAILED --summary
globus-console history -e all --hours 6     # every collection you monitor
```

| option | |
| --- | --- |
| `--hours N` | look back this far; 24 by default |
| `--status SUCCEEDED,FAILED` | which completed statuses |

Plus every `tasks` option. **`--endpoint` is required**: the Transfer API
only returns completed admin-visible tasks per collection. `--endpoint all`
loops over everything you monitor.

## `task` — one task in full

```sh
globus-console task 0f3a91c2-...
globus-console task 0f3a91c2-... --events -v
```

| option | |
| --- | --- |
| `--events` | also print the event log |
| `--events-limit N` | how many events; 50 by default |
| `-v, --verbose` | include the raw server message on each event |

`--events` is usually the fastest way to answer "why is this stuck?". On a
403 the command retries as your own task, so a task you submitted yourself
works even without a role on its collections.

## `endpoints` — what you can monitor

```sh
globus-console endpoints
globus-console endpoints --format json
```

No options beyond the shared ones. Run this first if `tasks` returns
`PermissionDenied` — it shows exactly which collections your role covers.

## Logging in

`login`, `logout` and `whoami` work as for
[globus-fs](#login-logout-whoami), with `GLOBUS_CONSOLE_` in place of
`GLOBUS_FS_` and tokens in `~/.globus/app/<client-id>/globus-console/`. The
two tools keep separate logins, so logging out of one leaves the other
alone. After login,
`globus-console` reports how many collections the identity can monitor.

For unattended use (cron, dashboards) set `GLOBUS_CONSOLE_CLIENT_ID` and
`GLOBUS_CONSOLE_CLIENT_SECRET` for a service account that holds
`activity_monitor`; no interactive login is needed.

## Fields

For `--fields` and `--sort` on task listings, in both tools:

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

# Piping

```sh
globus-console | grep -i turbo
globus-console --format tsv | awk -F'\t' '$5=="0B/s"'
globus-console --format json | jq '.[] | select(.faults > 0) | .task_id'
globus-fs du dataden:/lab --format tsv | sort -k2 -n
globus-fs find dataden:/lab --name '*.tmp' -0 | xargs -0 -n1 echo
```
