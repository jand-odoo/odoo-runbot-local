# odoo-runbot-local

Run any Odoo branch on your machine: from [runbot](https://runbot.odoo.com) with one click,
or straight from a branch name when runbot is down.

```bash
orl run master-l10n_cn-tax_report-jand   # check out the branch and start Odoo
orl status                               # what is running
orl stop                                 # stop it and drop the database
```

## Requirements

- Linux with systemd. Packages install automatically on `apt`, `dnf`, `pacman` and `zypper`;
  on anything else setup tells you what to add.
- PostgreSQL, Python 3.9+, Git
- An Odoo employee GitHub account with access to `odoo/odoo`, `odoo/enterprise` and `odoo-dev`

## Install

```bash
git clone git@github.com:jand-odoo/odoo-runbot-local.git
cd odoo-runbot-local
bash setup.sh
```

Setup asks before each step that needs sudo, and prints a ✓/✗/⊘ summary at the end. If
something fails it stops and tells you what to fix. Re-running is safe; completed steps are
skipped.

If you already have Odoo repositories locally it offers to create worktrees from them, which
takes seconds. Otherwise it clones, which takes a while and several GB.

Afterwards, **open a new terminal** so `orl` is on your PATH.

> **Keep this cloned folder where it is.** The background service runs from it, so moving or
> deleting it stops the tool working, and `orl doctor` will say so. If you do move it, run
> `bash setup.sh` again from the new location and everything reconnects.
>
> This is separate from `checkout_path`, which is where the odoo and enterprise repositories
> live. That one you can move freely: `orl config set checkout_path <new path>`.

## Using it

### From runbot

Install the browser extension. Setup builds it into `~/.odoo-runbot-local/extension/`.

**Chrome**: `chrome://extensions` → enable Developer mode → *Load unpacked* →
`~/.odoo-runbot-local/extension/chrome/`

**Firefox**: open `releases/odoo-runbot-local-1.2.xpi`, or `about:addons` → gear →
*Install Add-on From File*

Then open any bundle page on runbot and click **Run locally** next to the bundle name. The
button shows progress while it works, and a tab opens on `http://127.0.0.1:8072`. Click
**Stop** on the same page when you are done.

### From a branch name

Works with runbot down, and is usually quicker than clicking through the site.

```bash
orl run master-l10n_cn-tax_report-jand
orl run                    # asks for the branch, and offers matches if you misremember it
orl run 18.0
orl run master --dry-run   # show which commits it would use, and stop
```

It needs a matching pair of odoo and enterprise commits, and works them out from the branch
name:

| If the branch is… | You get |
|---|---|
| in **both** repositories | both branch tips |
| in **one** repository | that branch, plus the other repo at the point your branch was created |
| a version (`master`, `18.0`, `saas-18.4`) | the tip of that branch in each repository |
| in **neither** | a list of similarly-named branches to pick from |

The middle case is a best guess. Runbot knows which commits were genuinely built together and
this does not, so `orl run` tells you when it has inferred a pairing. Override it with
`--odoo-commit` and `--enterprise-commit` if you know better.

## Commands

| Command | |
|---|---|
| `orl run [BRANCH]` | Check out a branch and start Odoo |
| `orl status` | Branch, database, port, uptime, commits |
| `orl stop` | Stop Odoo and drop its database |
| `orl logs [server\|odoo] [-f]` | Show a log, `-f` to follow |
| `orl doctor` | Check the install and report anything broken |
| `orl config` | Show settings; `config set KEY VALUE` to change one |
| `orl update` | Get the latest version |
| `orl restart` | Restart the background service |
| `orl uninstall` | Remove it |

`odoo-runbot-local` works anywhere `orl` does, if you prefer typing it out.

## When something goes wrong

```bash
orl doctor
```

It checks tools, PostgreSQL, your repositories and their GitHub access, ports, the service
and the config, then prints exactly what is wrong. Start here.

Common cases:

| Symptom | Try |
|---|---|
| `orl: command not found` | Open a new terminal. Still missing → re-run `bash setup.sh` |
| Button on runbot does nothing | Reload the extension, then `orl doctor` |
| Two "Run locally" buttons | The extension is installed twice. Remove one |
| Odoo will not start | `orl logs odoo` |
| Port 8072 already in use | `orl config set odoo_port 8073` then `orl restart` |
| Cannot reach GitHub | `orl doctor` names the repository you lack access to |

## Settings

```bash
orl config                        # show everything
orl config set odoo_port 8073     # change one
```

| Setting | |
|---|---|
| `odoo_port` | Port Odoo runs on (default 8072). Change it if it clashes with your own |
| `checkout_path` | Where the odoo and enterprise repositories live (not this tool's folder) |
| `db_user`, `db_host`, `db_port` | PostgreSQL connection |
| `server_port` | The background service (default 8765). The extension expects this, so changing it breaks the button |

## Updating

```bash
orl update
```

Pulls the latest version, updates dependencies and restarts. Reload the browser extension
afterwards.

## Uninstall

```bash
orl uninstall
```

Removes the service, settings and logs, and asks before removing the checkouts. PostgreSQL,
your other databases, system packages and SSH keys are left alone. Remove the browser
extension yourself.

---

Working on the tool itself? See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
