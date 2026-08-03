# odoo-runbot-local

One-click tool to run any Odoo branch from [runbot.odoo.com](https://runbot.odoo.com) locally.

A browser extension adds a **Run locally** button on runbot bundle pages. Clicking it checks out the
exact commits from the latest batch, creates a fresh database, and starts Odoo on
`http://127.0.0.1:8072` — all automatically.

## Requirements

- Linux with **systemd** (user services). Package installation is automated for
  `apt`, `dnf`, `pacman` and `zypper`; on anything else setup tells you which binaries to install.
- PostgreSQL, Python 3.10+, Git
- An Odoo employee GitHub account with access to **`odoo/odoo`**, **`odoo/enterprise`** and
  **`odoo-dev`** — setup verifies all four repositories before it does anything else

## Quick start

```bash
git clone git@github.com:jand-odoo/odoo-runbot-local.git
cd odoo-runbot-local
bash setup.sh
```

Setup is **idempotent** — if a step fails, fix the cause and re-run; completed steps are detected and
skipped. It prints a per-step ✓/✗ summary and **exits non-zero if anything failed**, so a green
summary means it actually worked.

```
bash setup.sh --yes       # non-interactive, take every default
bash setup.sh --doctor    # diagnose an existing install, change nothing
bash setup.sh --help
```

Verbose output goes to `$TMPDIR/odoo-runbot-local-setup.log`.

### Something not working?

```bash
bash doctor.sh
```

It checks tools, PostgreSQL, config validity, both repositories and their remotes' reachability,
ports, the systemd unit, and the server's own `/health` report — then exits non-zero listing exactly
what is broken. **Run this first** before reading any other troubleshooting.

## Installing the extension

Setup builds it into `~/.odoo-runbot-local/extension/`.

**Chrome** — `chrome://extensions` → enable Developer mode → *Load unpacked* →
`~/.odoo-runbot-local/extension/chrome/`

**Firefox** — open the signed `releases/odoo-runbot-local-1.2.xpi`, or `about:addons` →
gear icon → *Install Add-on From File*. This installs permanently.

> For extension development, load the unsigned build instead:
> `about:debugging#/runtime/this-firefox` → *Load Temporary Add-on* →
> `~/.odoo-runbot-local/extension/firefox/manifest.json`. It is discarded when Firefox restarts.
> Don't run both at once — two copies each inject their own button.

## Usage

1. Open any bundle page on `runbot.odoo.com`
2. Click **Run locally** next to the bundle name — the button reports live progress
   (`fetching odoo`, `creating database`, `starting odoo`)
3. A tab opens at `http://127.0.0.1:8072`
4. Click **Stop** on the same page, or use the extension popup, to stop Odoo and drop the database

## Git access: SSH vs HTTPS

Both fetch identical content; only authentication and network reachability differ. Setup probes SSH
first and falls back to HTTPS:

| | SSH | HTTPS |
|---|---|---|
| Credential | key registered on GitHub | credential helper, `gh auth login`, or a PAT |
| Network | needs outbound **:22**, blocked on many corporate networks | plain `:443`, works nearly everywhere |

`GIT_TERMINAL_PROMPT=0` is exported everywhere, so a missing credential fails immediately instead of
hanging on an invisible prompt. If you already have Odoo repositories locally, setup offers to create
worktrees from them and inherits their existing remote URLs.

**`odoo-dev` remotes** are registered in both repositories (never fetched in full). Runbot bundles
for development branches reference commits that only exist in `odoo-dev`, so without them those
bundles cannot be resolved.

## Configuration

`~/.odoo-runbot-local/config.json` (schema version 2). Restart the service after editing:
`systemctl --user restart odoo-runbot-local`

| Key | Purpose |
|---|---|
| `checkout_path` | Where `odoo/` and `enterprise/` live |
| `mode` | `worktree` (from your existing repos) or `clone` |
| `python` | Interpreter used to run `odoo-bin` |
| `git_protocol` | `ssh` or `https` |
| `server_port` | This server (default 8765). **Changing it requires editing the extension.** |
| `odoo_port` | The Odoo instance (default 8072) — change this if 8072 collides with your own Odoo |
| `db_user`, `db_host`, `db_port` | PostgreSQL connection; host/port `null` means a local socket |
| `allowed_origins` | Origins permitted to call the mutating endpoints |

A missing key falls back to a built-in default, and malformed JSON is reported through `/health`
rather than crash-looping the service.

Override the checkout location at setup time with `RUNBOT_LOCAL_CHECKOUT=/path bash setup.sh`.

## API

| Endpoint | Method | Purpose |
|---|---|---|
| `/checkout` | POST | Fetch commits, create the DB, start Odoo |
| `/stop` | POST | Stop the instance and drop its database |
| `/status` | GET | Current instance state, including the in-flight `phase` |
| `/health` | GET | `{"ok": bool, "problems": [...]}` — why checkouts would fail |

`POST` bodies are JSON: `{"branch": "...", "commit_odoo": "...", "commit_enterprise": "..."}`.

**Security.** The mutating endpoints require an `X-Runbot-Local: 1` header, and reject any request
whose `Origin` is not `https://runbot.odoo.com` or a browser extension. A web page you visit cannot
set a custom header on a simple request, and the preflight it would need is refused — so an arbitrary
site can no longer drop your database, which it could in version 1.0.

## Layout

| Path | Purpose |
|---|---|
| `server.py` | Flask server (`/checkout`, `/stop`, `/status`, `/health`) |
| `lib.sh` | Shared constants and helpers for all shell scripts |
| `setup.sh` / `update.sh` / `uninstall.sh` | Install, upgrade, remove |
| `doctor.sh` | Read-only diagnostics |
| `build-extension.sh` | Assembles per-browser packages from `extension/shared/` |
| `extension/shared/` | The extension's actual source — edit here |
| `extension/{chrome,firefox}/manifest.json` | Per-browser manifests (MV3 / MV2) |

### Data locations

| Path | Purpose |
|---|---|
| `~/.odoo-runbot-local/` | Server, venv, config, logs, built extension |
| `~/.odoo-runbot-local/logs/server.log` | Server log (self-rotating, 5 MB × 3) |
| `~/.odoo-runbot-local/logs/odoo-8072.log` | Odoo output (rotated on each start) |
| `~/.local/share/odoo-runbot-local/checkout/` | Default checkout location |
| `~/.config/systemd/user/odoo-runbot-local.service` | systemd unit |

## Releasing a signed Firefox extension

Firefox only installs signed add-ons permanently. To cut a release:

1. Bump `version` in **both** `extension/chrome/manifest.json` and
   `extension/firefox/manifest.json` — AMO permanently rejects a version it has already signed.
2. Put your AMO credentials in `~/.web-ext-config.mjs` (never in the repo):

   ```js
   export default {
     sign: { apiKey: 'user:12345678:901', apiSecret: '<secret>' },
   };
   ```

   Get them from <https://addons.mozilla.org/developers/addon/api/key/>. The secret is
   shown only once.
3. Sign:

   ```bash
   bash sign-extension.sh                    # unlisted (self-distribution)
   bash sign-extension.sh --channel listed   # public AMO listing
   ```

The script rebuilds from `extension/shared/`, lints, uploads, verifies the result actually
carries Mozilla's signature, and writes `releases/odoo-runbot-local-<version>.xpi`.

Chrome is unaffected — it loads unpacked from `~/.odoo-runbot-local/extension/chrome/`.

## Updating

```bash
bash update.sh
```

Pulls, reinstalls Python dependencies, migrates the config, re-renders the systemd unit, restarts and
health-checks the server, and rebuilds the extension. Reload the extension in your browser afterwards.

## Uninstall

```bash
bash uninstall.sh
```

Removes the service, app data and desktop shortcut, and offers to remove the repositories. It stops
only the Odoo process it started — never whatever else happens to be on the Odoo port. PostgreSQL,
system packages and SSH keys are left alone.

## License

MIT
