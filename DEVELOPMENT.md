# Development

```sh
uv sync                          # .venv from uv.lock, with dev tools
.venv/bin/pre-commit install     # run the checks on every commit
.venv/bin/pytest                 # with coverage, per tox.ini
```

Every commit runs isort, black, flake8, bandit, pytest and a `uv.lock`
check (`.pre-commit-config.yaml`). GitHub runs the same hooks, plus the
tests on Python 3.9 and 3.13, for every push and pull request
(`.github/workflows/checks.yml`), so a commit made without the hooks is
still caught.

`tox.ini` holds the flake8, isort and pytest settings and `.bandit` the
bandit exclusions, matching archivetar. Every module, class, function and
method has a docstring. Updating dependencies: see
[INSTALL.md](INSTALL.md#updating-dependencies).

## Layout

```
src/globus_tools/
    console.py      globus-console: the Activity view
    fs.py           globus-fs: ls, find, du, archivescan, bookmarks, shortcuts
    tasks.py        the task table both tools draw
    common.py       the GlobusApp, login and re-login, formatting
    security.py     token file and directory permissions
    shortcuts.py    collections.conf parsing
    walk.py         the parallel directory walker
packaging/build-standalone.sh    see INSTALL.md
```

Which API fills the task table is fixed by the tool that registers the
command, never by a user flag, so nothing in `globus-fs` can reach
`endpoint_manager`.

## Tests

No test touches the network or a real token file: `tests/conftest.py`
stubs `Tool.transfer_client` and serves a fake collection tree. The `run`
fixture drives `globus-fs`, `run_console` drives `globus-console`.

| file | covers |
| --- | --- |
| `test_security.py` | token file and directory permissions, the stderr login prompt |
| `test_relogin.py` | re-login for consent and high assurance (the 502 case), one login across threads |
| `test_client_id.py` | client ID resolution |
| `test_fs.py` | `ls`, `du` rollups, `find` filters, API call counts |
| `test_archivescan.py` | the size split, env default, snapshot and symlink skipping |
| `test_batch.py` | `find --batch` round-trips through globus-cli's batch parser |
| `test_bookmarks.py`, `test_collections.py` | bookmarks, shortcuts and target resolution |
| `test_tasks.py`, `test_format.py` | the task table, and which API each tool calls |
| `test_docs.py` | USAGE.md documents every option of both tools |
