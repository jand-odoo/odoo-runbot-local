# odoo-runbot-local

Run any Odoo branch locally — from [runbot](https://runbot.odoo.com) with one click, or
straight from a branch name when runbot is unavailable.

```bash
orl run master-l10n_cn-tax_report-jand    # resolve, check out, start Odoo
orl status                                # what is running
orl stop                                  # stop it and drop the database
```

A browser extension adds a **Run locally** button on runbot bundle pages. Clicking it checks
out the exact commits from the latest batch, creates a fresh database, and starts Odoo on
`http://127.0.0.1:8072`.

## Requirements

- Linux with **systemd** (user services). Packages are installed automatically for
  `apt`, `dnf`, `pacman` and `zypper`; on anything else setup tells you which binaries to add.
- PostgreSQL, Python 3.9+, Git
- An Odoo employee GitHub account with access to **`odoo/odoo`**, **`odoo/enterprise`** and
  **`odoo-dev`** — setup verifies all four before doing anything else

## Quick start

```bash
git clone git@github.com:jand-odoo/odoo-runbot-local.git
cd odoo-runbot-local
bash setup.sh
```

`setup.sh` is a thin wrapper — on a fresh clone the CLI is not on your PATH yet, so it is the
one command you can run from the repository. Everything after that uses the CLI.

Setup is **idempotent**: if a step fails, fix the cause and re-run. It prints a per-step
✓/✗/⊘ summary and **exits non-zero if anything failed**, so a green summary means it worked.

It also symlinks the CLI into `~/.local/bin` as both `odoo-runbot-local` and `orl`, and offers
to add that directory to your shell's rc file if it is not already on PATH. You will need a
new shell afterwards — no process can change its parent's environment.

## Commands

| Command | What it does |
|---|---|
| `orl run [BRANCH]` | Resolve a branch to a commit pair, check out, and start Odoo |
| `orl status` | Server and instance state: branch, database, pid, uptime, commits |
| `orl stop` | Stop the running Odoo instance and drop its database |
| `orl start` / `orl restart` | Start or restart the background server |
| `orl logs [server\|odoo] [-f]` | Tail a log without hunting for the path |
| `orl config [get\|set KEY VALUE]` | Read or change configuration, with validation |
| `orl doctor` | Diagnose an install, changing nothing |
| `orl update` | Pull, reinstall dependencies, migrate config, restart |
| `orl setup` | Re-run installation (idempotent) |
| `orl uninstall` | Remove the service and app data |
| `orl extension build\|sign` | Build the browser packages, or sign a release on AMO |

### Something not working?

```bash
orl doctor
```

Checks tools, PostgreSQL, config validity, both repositories and their remotes' reachability,
ports, the systemd unit, and the server's own `/health`. Exits non-zero listing exactly what
is broken. **Run this first.**

## Running a branch without runbot

```bash
orl run master-l10n_cn-tax_report-jand
orl run                     # prompts, and offers matches if the name is not exact
orl run 18.0 --dry-run      # resolve the commit pair and stop
```

Odoo branches are named `<base>-<feature>-<initials>`, so the base version is read from the
name. Four cases:

| Situation | What happens |
|---|---|
| Branch in **both** repos | Both tips are used — nothing is inferred |
| Branch in **one** repo | The other is pinned to its base branch **at the fork point** |
| Branch **is** a version (`master`, `18.0`) | Each repository's tip of that branch |
| Branch in **neither** | Searches both remotes and offers matches |

The second case is the interesting one. The naive answer — "use the other repo's `master`
tip" — is wrong: `master` may have moved months past whatever your branch is based on. So the
pairing uses `git merge-base` to find where the branch diverged, and picks the other
repository's commit at that moment. This stays correct across rebases, because the merge base
moves with the branch.

> **This is a heuristic.** Runbot's batch pairing is authoritative because it records what was
> actually built together. `orl run` says so when it infers, and `--odoo-commit` /
> `--enterprise-commit` override it entirely.

Note that `odoo-dev`'s copies of `master` and other version branches are abandoned 2014 stubs,
so version branches always resolve against `origin`, and feature branches against `dev`.

## Installing the extension

Setup builds it into `~/.odoo-runbot-local/extension/`.

**Chrome** — `chrome://extensions` → enable Developer mode → *Load unpacked* →
`~/.odoo-runbot-local/extension/chrome/`

**Firefox** — open the signed `releases/odoo-runbot-local-1.2.xpi`, or `about:addons` → gear →
*Install Add-on From File*.

> For extension development, load the unsigned build from
> `about:debugging#/runtime/this-firefox` instead. Don't run both at once — two copies each
> inject their own button.

## Git access: SSH vs HTTPS

Both fetch identical content; only authentication and reachability differ. Setup probes SSH
first and falls back to HTTPS.

| | SSH | HTTPS |
|---|---|---|
| Credential | key registered on GitHub | credential helper, `gh auth login`, or a PAT |
| Network | needs outbound **:22**, blocked on many corporate networks | plain `:443` |

`GIT_TERMINAL_PROMPT=0` is set everywhere, so a missing credential fails immediately instead
of hanging on an invisible prompt. If you already have Odoo repositories locally, setup offers
to create worktrees from them rather than cloning several GB again.

## Configuration

`~/.odoo-runbot-local/config.json` (schema version 2). Use `orl config set` rather than
editing by hand — it validates types and tells you when a restart is needed.

| Key | Purpose |
|---|---|
| `checkout_path` | Where `odoo/` and `enterprise/` live |
| `mode` | `worktree` (from your existing repos) or `clone` |
| `python` | Interpreter used to run `odoo-bin` |
| `git_protocol` | `ssh` or `https` |
| `server_port` | This server (default 8765). **The extension hardcodes this.** |
| `odoo_port` | The Odoo instance (default 8072) — change if it collides with your own |
| `db_user`, `db_host`, `db_port` | PostgreSQL connection; `null` host means a local socket |
| `allowed_origins` | Origins permitted to call the mutating endpoints |

A missing key falls back to a built-in default, and malformed JSON is reported through
`/health` rather than crash-looping the service.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/checkout` | POST | Fetch commits, create the database, start Odoo |
| `/stop` | POST | Stop the instance and drop its database |
| `/status` | GET | Current state, including the in-flight `phase` |
| `/health` | GET | `{"ok": bool, "problems": [...]}` |

**Security.** Mutating endpoints require an `X-Runbot-Local: 1` header and reject any request
whose `Origin` is not `https://runbot.odoo.com` or a browser extension. A web page cannot set
a custom header on a simple request, and the preflight it would need is refused — so an
arbitrary site can no longer drop your database, which it could in version 1.0.

## Layout

| Path | Purpose |
|---|---|
| `odoo_runbot_local/config.py` | Schema, defaults, migration — the single source of truth |
| `odoo_runbot_local/instance.py` | Checkout, database and process lifecycle |
| `odoo_runbot_local/server.py` | HTTP layer only |
| `odoo_runbot_local/resolve.py` | Branch → commit pair, including fork-point pairing |
| `odoo_runbot_local/commands/` | One module per subcommand |
| `odoo_runbot_local/platform/` | Packages, systemd, postgres, git, ports |
| `extension/shared/` | The extension's actual source — edit here |
| `bin/odoo-runbot-local` | Stdlib-only launcher (works before any virtualenv exists) |
| `tests/` | pytest suite |

### Data locations

| Path | Purpose |
|---|---|
| `~/.odoo-runbot-local/` | venv, config, logs, built extension |
| `~/.odoo-runbot-local/logs/server.log` | Server log (self-rotating, 5 MB × 3) |
| `~/.local/share/odoo-runbot-local/checkout/` | Default checkout location |
| `~/.config/systemd/user/odoo-runbot-local.service` | systemd unit |

The service runs the package **straight from this repository** rather than a copy, so `update`
is a pull plus a restart. Moving or deleting the checkout breaks the service — `doctor` checks
for that.

## Releasing a signed Firefox extension

Firefox only installs signed add-ons permanently.

1. Bump `version` in **both** `extension/chrome/manifest.json` and
   `extension/firefox/manifest.json` — AMO permanently rejects a version it has already signed.
2. Put your AMO credentials in `~/.web-ext-config.mjs` (never in the repo):

   ```js
   export default {
     sign: { apiKey: 'user:12345678:901', apiSecret: '<secret>' },
   };
   ```

3. `orl extension sign` (add `--channel listed` for a public AMO listing).

## Development

```bash
pip install pytest flask psycopg2-binary
python -m pytest tests/ -q
```

The suite uses an isolated `HOME`, so it never touches a real install. The resolution tests
build throwaway git repositories to exercise fork-point pairing for real.

## Uninstall

```bash
orl uninstall
```

Removes the service, app data, CLI symlinks and desktop shortcut, and offers to remove the
repositories. It stops only the Odoo process it started — never whatever else is on the port.
PostgreSQL, system packages and SSH keys are left alone.

## License

MIT
