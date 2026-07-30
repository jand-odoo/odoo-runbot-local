# odoo-runbot-local

One-click tool to run any Odoo branch from [runbot.odoo.com](https://runbot.odoo.com) locally on your machine.

A Chrome extension adds a "Run locally" button on runbot bundle pages. Clicking it checks out the exact commits from the latest build, creates a fresh database, and starts Odoo on `http://localhost:8072` — all automatically.

[<video src="demo.mp4" width="100%" controls></video>](https://github.com/user-attachments/assets/7b7b24ef-eb1d-420a-b617-9bb8f06f1176)

## Requirements

- Ubuntu / Debian (or any Linux with systemd)
- PostgreSQL (`sudo apt install postgresql postgresql-client`)
- Python 3.10+
- Git
- GitHub SSH key with access to `odoo/odoo` and `odoo/enterprise`

## Quick Start

Run the interactive setup script:

```bash
bash setup.sh
```

It walks through 10 steps:

1. **System packages** — installs PostgreSQL, Python, Git, etc.
2. **PostgreSQL setup** — creates your user role and database
3. **GitHub SSH** — generates a key and guides you through adding it to GitHub
4. **Clone repos** — clones or creates worktrees of `odoo` and `enterprise`
5. **Odoo dependencies** — runs `debinstall.sh` for system libs
6. **Python venv** — creates a virtualenv and installs dependencies
7. **Configuration** — writes `~/.odoo-runbot-local/config.json`
8. **Systemd service** — installs and starts the local server
9. **Desktop shortcut** — adds a launcher to restart the service
10. **Chrome extension** — copies the extension files and shows install instructions

After setup completes, the server runs on `http://localhost:8765`.

## Usage

### 1. Install the Chrome extension

- Open `chrome://extensions`
- Enable **Developer mode** (top right)
- Click **Load unpacked**
- Select `~/.odoo-runbot-local/extension/`

### 2. Run a branch

- Go to any bundle page on `runbot.odoo.com/*/bundle/*`
- Click **Run locally** (beside the bundle name at the top)
- Wait — the server fetches commits, creates a DB, and starts Odoo
- A new tab opens at `http://localhost:8072`

### 3. Stop the instance

- Go back to any bundle page — the button now shows **Stop** (red)
- Click it to stop Odoo and drop the database
- Or use the desktop shortcut to restart the server

### 4. Check status

- Click the extension icon (top right toolbar) to see what's running

## Architecture

```
┌──────────────────┐      ┌──────────────┐      ┌──────────────────┐
│  Chrome Extension │      │  Flask Server │      │   Odoo Instance  │
│                  │      │              │      │                  │
│  content.js ─────┼──────┼→ /checkout   │──────┼→ git checkout    │
│  (runbot page)   │      │  /status     │      │  createdb        │
│                  │      │  /stop       │      │  odoo-bin        │
│  background.js ──┼──────┼→ localhost:  │      │  localhost:8072  │
│  (service worker)│      │  8765        │      │                  │
│                  │      │              │      │                  │
│  popup.html     │      │  systemd      │      │                  │
└──────────────────┘      └──────────────┘      └──────────────────┘
```

### Components

| Path | Purpose |
|---|---|
| `server.py` | Flask HTTP server (checkout, status, stop) |
| `setup.sh` | Interactive 10-step onboarding |
| `uninstall.sh` | Removes server, service, repos |
| `killport.sh` | Helper to kill a process by port |
| `odoo-runbot-local.service` | systemd user unit (auto-start, restart) |
| `extension/content.js` | Injects buttons on runbot pages |
| `extension/background.js` | Service worker — proxies requests to server |
| `extension/popup.html` | Popup showing instance status |

### Data locations

| Path | Purpose |
|---|---|
| `~/.odoo-runbot-local/server.py` | Live copy of the server |
| `~/.odoo-runbot-local/venv/` | Python virtualenv |
| `~/.odoo-runbot-local/config.json` | Server config (python path, checkout path) |
| `~/.odoo-runbot-local/logs/server.log` | Server logs |
| `~/.odoo-runbot-local/logs/odoo-8072.log` | Odoo stdout/stderr |
| `~/.odoo-runbot-local/running.json` | Current instance state |
| `~/.odoo-runbot-local/extension/` | Live Chrome extension |
| `~/.config/systemd/user/odoo-runbot-local.service` | systemd unit |
| `~/odoo/repositories/localdev/odoo-runbot-local/` | Checkout location (odoo/ + enterprise/) |

## API Endpoints

### `GET /checkout?branch=<name>&commit_odoo=<hash>&commit_enterprise=<hash>`

Fetches commits, creates a fresh DB, starts Odoo. Kills any existing instance first.

### `GET /status`

Returns current instance state:
```json
{
  "running": true,
  "alive": true,
  "pid": 1234,
  "branch": "master-18.0-sale-fix",
  "db_name": "master-18.0-sale-fix",
  "odoo_commit": "6e58e88c",
  "enterprise_commit": "51c03afe",
  "uptime_seconds": 342
}
```

### `GET /stop`

Kills the running Odoo instance and cleans up.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Server unreachable" in popup | Server crashed. Restart: `systemctl --user restart odoo-runbot-local` or use the desktop shortcut |
| Odoo shows 500 error | Check `~/.odoo-runbot-local/logs/odoo-8072.log` |
| Button does nothing | Reload the extension in `chrome://extensions` |
| Failed to fetch branch/commit | Check `~/.odoo-runbot-local/logs/server.log` for git errors |

## Uninstall

```bash
bash uninstall.sh
```

This removes the server, systemd service, logs, repos, and desktop shortcut. PostgreSQL, system packages, and the Chrome extension are left untouched.

## License

MIT
