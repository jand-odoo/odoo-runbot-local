# Working on odoo-runbot-local

For using the tool, see [README.md](README.md). This is about changing it.

## Layout

| Path | Purpose |
|---|---|
| `odoo_runbot_local/config.py` | Schema, defaults and migration: the single source of truth |
| `odoo_runbot_local/instance.py` | Checkout, database and process lifecycle |
| `odoo_runbot_local/server.py` | HTTP layer only |
| `odoo_runbot_local/resolve.py` | Branch name → commit pair |
| `odoo_runbot_local/legacy.py` | Cleanup of earlier installs |
| `odoo_runbot_local/client.py` | Talking to the local server |
| `odoo_runbot_local/ui.py` | Terminal output, prompts, step accounting |
| `odoo_runbot_local/commands/` | One module per subcommand |
| `odoo_runbot_local/platform/` | packages, systemd, postgres, git, ports, proc |
| `extension/shared/` | The extension's real source. Edit here, never the built copies |
| `extension/{chrome,firefox}/manifest.json` | Per-browser manifests (MV3 / MV2) |
| `bin/odoo-runbot-local` | Stdlib-only launcher |
| `tests/` | pytest suite |

### Two constraints worth knowing before you change things

**The CLI must run on the system interpreter, with no third-party packages.** Setup creates
the virtualenv, so the CLI has to work before one exists. Only the server may import `flask`
and `psycopg2`. CI enforces this by running the CLI on a bare interpreter.

**The service runs the package from this cloned repository**, not from a copy. The unit sets
`PYTHONPATH` to the clone's path, so `update` is a pull plus a restart with nothing to
synchronise. The cost is that moving or deleting the clone breaks the service, which `doctor`
checks for, and `setup` rewrites the unit when run from a new location.

> Two directories are easy to confuse, especially since they often share a basename. This
> cloned repository is the *tool*. The `checkout_path` setting points somewhere else
> entirely, at the `odoo` and `enterprise` repositories the tool checks commits out into.
> Moving the tool breaks the service; moving the Odoo repositories only needs
> `orl config set checkout_path <new path>`.

## Running the tests

```bash
pip install pytest flask psycopg2-binary
python -m pytest tests/ -q
```

Every test runs against an isolated `HOME`, so they never touch a real install. The resolution
tests build throwaway git repositories, including a fake `odoo-dev` fork whose `master` is a
stub, to exercise commit pairing for real rather than with mocks.

Lint the way CI does:

```bash
ruff check --isolated --select E4,E7,E9,F odoo_runbot_local/ tests/
shellcheck --severity=warning ./*.sh
for f in extension/shared/*.js; do node --check "$f"; done
```

## How branch resolution works

`resolve.py` turns a branch name into a matching pair of commits.

Odoo branches are named `<base>-<feature>-<initials>`, so the base version is read from the
name. When a branch exists in only one repository, the other is pinned using **the fork
point**, not the branch tip:

```
timestamp = commit date of: git merge-base <branch> origin/<base>    # in the repo that has it
other     = git rev-list -1 --before=<timestamp> origin/<base>       # in the other repo
```

Pairing on the branch tip would be wrong for a long-lived branch, whose tip can be months
past the core it was actually developed against. The fork point also survives a rebase,
because the merge base moves with the branch.

**Version branches resolve against `origin`, never `dev`.** `odoo-dev/odoo`'s `master` is an
abandoned 2014 commit reading *"Please use the right repo"*, and checking it out yields a
tree with no `odoo-bin` in it. Feature branches are the opposite and prefer `dev`. There are
regression tests for both directions.

This is a heuristic. Runbot's batch pairing is authoritative because it records what was
actually built together, so `run` says when it has inferred rather than found a pair.

## HTTP API

The extension and the CLI both use it.

| Endpoint | Method | Purpose |
|---|---|---|
| `/checkout` | POST | Fetch commits, create the database, start Odoo |
| `/stop` | POST | Stop the instance and drop its database |
| `/status` | GET | Current state, including the in-flight `phase` |
| `/health` | GET | `{"ok": bool, "problems": [...]}` |

`POST` bodies are JSON: `{"branch": …, "commit_odoo": …, "commit_enterprise": …}`.

**Mutating endpoints require an `X-Runbot-Local: 1` header** and reject any request whose
`Origin` is not `https://runbot.odoo.com` or a browser extension. A web page cannot set a
custom header on a simple request, and the preflight it would need is refused. Without this,
any site you visited could drop your database, as it could in version 1.0.

An absent `Origin` is allowed, because that means the call did not come from page JavaScript:
the extension's background fetch and the CLI both send none.

## Releasing a signed Firefox extension

Firefox only installs signed add-ons permanently.

1. Bump `version` in **both** `extension/chrome/manifest.json` and
   `extension/firefox/manifest.json`. AMO permanently rejects a version it has already signed.
2. Put your AMO credentials in `~/.web-ext-config.mjs`, never in the repo, which is
   gitignored against exactly this mistake:

   ```js
   export default {
     sign: { apiKey: 'user:12345678:901', apiSecret: '<secret>' },
   };
   ```

   Get them from <https://addons.mozilla.org/developers/addon/api/key/>. The secret is shown
   once.
3. Sign:

   ```bash
   orl extension sign                    # unlisted, for self-distribution
   orl extension sign --channel listed   # public AMO listing
   ```

The add-on ID must stay `odoo-runbot-local@example.com` so Firefox treats a new build as an
upgrade rather than a second add-on. The channel must match how the add-on was first
submitted; AMO rejects a mismatch with a clear message.

Signing verifies the returned file actually contains `META-INF/mozilla.rsa` before publishing
it, and refuses to overwrite an existing release.

Chrome needs none of this. It loads unpacked from `~/.odoo-runbot-local/extension/chrome/`.

## Data locations

| Path | |
|---|---|
| `~/.odoo-runbot-local/` | venv, config, logs, built extension |
| `~/.odoo-runbot-local/logs/server.log` | Server log, self-rotating at 5 MB × 3 |
| `~/.odoo-runbot-local/logs/odoo-<port>.log` | Odoo output, rotated on each start |
| `~/.odoo-runbot-local/running.json` | The current instance: pid, start time, branch, database |
| `~/.local/share/odoo-runbot-local/checkout/` | Default `checkout_path`: the odoo and enterprise repositories |
| `~/.config/systemd/user/odoo-runbot-local.service` | systemd unit |
