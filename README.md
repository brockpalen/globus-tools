# globus-tools

Two command line tools for Globus. Community tools, not an official Globus
product.

| command | for | needs |
| --- | --- | --- |
| **`globus-fs`** | everyone | a Globus login |
| **`globus-console`** | anyone with an activity role | `activity_monitor` or `activity_manager` on a subscribed collection |

**`globus-fs`** is `ls`, `find`, `du` and `archivescan` for storage reachable
only through Globus, plus your bookmarks and your own transfers:

```
$ globus-fs du dataden:/umms-mylab -d 1
  SIZE  FILES  DIRS  PATH
104.1G      7     4  /umms-mylab
100.0G      1     1  /umms-mylab/backups
  4.1G      5     1  /umms-mylab/fastq
```

**`globus-console`** is the web console's **Activity** tab as a greppable
table, one line per transfer, across every collection you monitor:

```
$ globus-console --errors
TASK_ID   STATUS    ISSUE                OWNER            RATE      FILES  BYTES  FLT    AGE  SOURCE               DEST
0f3a91c2  ACTIVE    PERMISSION_DENIED    user2@umich.edu  0B/s   600/2400  15.0G   14  1h14m  Great Lakes scratch  Turbo Research Storage
```

## Try it

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/)
(`curl -LsSf https://astral.sh/uv/install.sh | sh`):

```sh
uv tool install git+https://github.com/brockpalen/globus-tools
globus-fs login
globus-fs ls <collection-uuid>:/
```

`uv tool upgrade globus-tools` picks up changes. Please report problems,
and what you tried, as
[GitHub issues](https://github.com/brockpalen/globus-tools/issues).

Every command and option, with examples: **[USAGE.md](USAGE.md)**.
Building a site-wide install: **[INSTALL.md](INSTALL.md)**.

## Who can use `globus-console`

Anyone holding `activity_monitor` or `activity_manager` on a subscribed
collection; everyone else gets `PermissionDenied`. That includes:

- subscription and endpoint administrators
- **the owner of a guest collection**, on that collection (owners hold
  both roles automatically)
- anyone granted a role on a collection's **Roles** tab in the web app;
  a role on an endpoint or mapped collection covers its guest collections

`globus-console endpoints` shows which collections you can monitor.

## Log in

```sh
globus-fs login
globus-console login        # separate login, if you hold an activity role
```

Both tools use a built-in Globus client ID, as globus-cli does; it is not a
secret. A site can use its own with `GLOBUS_FS_CLIENT_ID` /
`GLOBUS_CONSOLE_CLIENT_ID`. For cron, see
[unattended use](USAGE.md#logging-in).

## Collection shortcuts

Friendly names, so nobody looks up a UUID:

```sh
globus-fs ls dataden:/umms-mylab
globus-fs collections                 # which names exist here
```

One per line, `NAME = COLLECTION_UUID`, optionally with a base path:

```ini
# NAME = COLLECTION_UUID[:/optional/base/path]
dataden = ab65757f-00f5-4e5b-aa21-133187732a01
scratch = 1a2b3c4d-0000-0000-0000-000000000000:/scratch
```

Read from, later files winning:

| file | for |
| --- | --- |
| `/etc/globus-fs/collections.conf` | everyone on the machine |
| `$GLOBUS_FS_COLLECTIONS` | `:`-separated files, e.g. set by an Lmod module |
| `~/.globus/app/<client-id>/globus-fs/collections.conf` | one user's own |

`#` starts a comment; `NAME: UUID` and `NAME UUID` also work; a malformed
line is skipped. Names have no spaces or colons. A personal Globus bookmark
of the same name wins. Start from
[`examples/collections.conf`](examples/collections.conf).

## High assurance collections (CUI, PHI)

- **Expect login prompts.** The first use of a high assurance collection,
  and every time its session times out, asks you to log in again — with
  the identity the collection requires (e.g. `umich.edu`). The command then
  carries on. This needs a terminal: in a script or cron job the command
  stops and says so.
- **Log out when you are done:** `globus-fs logout` revokes your tokens
  with Globus, as Globus advises for its own CLI on high assurance data.
- **File names are data.** Paths printed by `ls`, `find`, `du` and
  `globus-console` can themselves be CUI or PHI. Before saving output to a
  file, run `umask 077` so only you can read it, and keep it out of shared
  directories.
- If your home directory is on NFS, root on any NFS client can read your
  token file.

## Credential security

Login, tokens, refresh and logout are handled by globus-sdk, as in
globus-cli. Tokens are stored at
`~/.globus/app/<client-id>/<tool>/tokens.json`. On top of what globus-cli
does:

- **The token file must be 600.** A looser one is tightened, with a warning;
  one owned by another user is refused.
- **The tool's own directory is 700**, so nobody else can create a file or
  symlink beside the token file. The shared `~/.globus` is left alone, as
  globus-cli and archivetar do.
- **`logout` revokes** the tokens with Globus, not just deletes the file.

If a token file was ever readable by others, revoke it at
<https://app.globus.org/settings/consents>; `chmod` does not invalidate a
copy already taken.

## Troubleshooting

- **`PermissionDenied` from globus-console** — no activity role.
  `globus-console endpoints` shows what you can monitor; `globus-fs tasks`
  shows your own transfers.
- **"needs you to log in … in an interactive terminal"** — consent or a high
  assurance session is needed. Run the same command in a terminal.
- **`Not authenticated`** — `globus-fs login --force`.

Contributing: [DEVELOPMENT.md](DEVELOPMENT.md).
