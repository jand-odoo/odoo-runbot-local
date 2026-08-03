#!/bin/bash
# Pulls the latest code and redeploys it. Safe to run from either the repo
# checkout or the installed copy in ~/.odoo-runbot-local.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The installed copy is a snapshot and goes stale; find the real repo first.
find_repo_dir() {
    if [ -n "${REPO_DIR:-}" ]; then echo "$REPO_DIR"; return; fi
    if [ -n "${1:-}" ]; then echo "$1"; return; fi
    if [ -d "$SCRIPT_DIR/.git" ]; then echo "$SCRIPT_DIR"; return; fi
    if [ -f "$HOME/.odoo-runbot-local/config.json" ]; then
        python3 - "$HOME/.odoo-runbot-local/config.json" <<'PY' 2>/dev/null && return
import json, sys
try:
    path = json.load(open(sys.argv[1])).get("repo_path")
except Exception:
    path = None
if not path:
    raise SystemExit(1)
print(path)
PY
    fi
    echo "$SCRIPT_DIR"
}

REPO_DIR="$(find_repo_dir "${1:-}")"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[ERROR] No git repository found at $REPO_DIR" >&2
    echo "        Usage: bash update.sh [path-to-repo]" >&2
    echo "               REPO_DIR=/path/to/repo bash update.sh" >&2
    exit 1
fi

# Re-exec from the repo so a stale installed copy cannot run outdated logic.
if [ "$SCRIPT_DIR" != "$REPO_DIR" ] && [ -z "${RUNBOT_UPDATE_REEXEC:-}" ]; then
    export RUNBOT_UPDATE_REEXEC=1
    exec bash "$REPO_DIR/update.sh" "$REPO_DIR"
fi

# shellcheck source=lib.sh
source "$REPO_DIR/lib.sh"

echo ""
info "Updating $APP_NAME from $REPO_DIR"
echo ""

# ─── 1. Pull ─────────────────────────────────────────────────
info "Pulling latest code..."
if ! git -C "$REPO_DIR" pull --ff-only; then
    err "git pull failed — resolve it in $REPO_DIR and re-run."
    exit 1
fi
ok "Repo updated"

# ─── 2. Python dependencies ──────────────────────────────────
# Never skipped: a new server.py may need a package the installed venv lacks.
if [ -x "$VENV_DIR/bin/pip" ]; then
    info "Updating Python dependencies..."
    if ! "$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/requirements.txt"; then
        err "Failed to install server requirements."
        exit 1
    fi
    ok "Dependencies up to date"
else
    warn "No virtualenv at $VENV_DIR — run setup.sh first."
    exit 1
fi

# ─── 3. Deploy files ─────────────────────────────────────────
info "Deploying server..."
mkdir -p "$APP_DIR" "$LOG_DIR"
cp "$REPO_DIR/server.py"  "$APP_DIR/server.py"  || exit 1
cp "$REPO_DIR/lib.sh"     "$APP_DIR/lib.sh"     || exit 1
cp "$REPO_DIR/update.sh"  "$APP_DIR/update.sh"  || exit 1
cp "$REPO_DIR/doctor.sh"  "$APP_DIR/doctor.sh"  || exit 1
chmod +x "$APP_DIR/update.sh" "$APP_DIR/doctor.sh"
ok "Server files deployed"

# ─── 4. Migrate config ───────────────────────────────────────
info "Checking configuration..."
CONFIG_FILE="$CONFIG_FILE" TARGET_VERSION="$CONFIG_VERSION" \
DEFAULT_CHECKOUT="$DEFAULT_CHECKOUT_DIR" DEFAULT_PYTHON="$VENV_DIR/bin/python" \
DEFAULT_USER="$(current_user)" REPO="$REPO_DIR" \
SERVER_PORT="$DEFAULT_SERVER_PORT" ODOO_PORT="$DEFAULT_ODOO_PORT" \
python3 - <<'PY'
import json, os, sys

path = os.environ["CONFIG_FILE"]
target = int(os.environ["TARGET_VERSION"])
try:
    with open(path) as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        raise ValueError("not an object")
except Exception as exc:
    print(f"  config unreadable ({exc}); rebuilding from defaults")
    config = {}

before = json.dumps(config, sort_keys=True)

defaults = {
    "version": target,
    "mode": "clone",
    "checkout_path": os.environ["DEFAULT_CHECKOUT"],
    "python": os.environ["DEFAULT_PYTHON"],
    "repo_path": os.environ["REPO"],
    "git_protocol": "ssh",
    "server_port": int(os.environ["SERVER_PORT"]),
    "odoo_port": int(os.environ["ODOO_PORT"]),
    "db_user": os.environ["DEFAULT_USER"],
    "db_host": None,
    "db_port": None,
    "allowed_origins": ["https://runbot.odoo.com"],
}
for key, value in defaults.items():
    config.setdefault(key, value)

# repo_path must track wherever the repo actually lives now.
config["repo_path"] = os.environ["REPO"]
config["version"] = target

if json.dumps(config, sort_keys=True) != before:
    with open(path, "w") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    print(f"  config migrated to version {target}")
else:
    print(f"  config already at version {target}")
PY
ok "Configuration checked"

# ─── 5. Systemd unit ─────────────────────────────────────────
info "Updating systemd unit..."
install_unit_file "$REPO_DIR/$APP_NAME.service" || exit 1
systemctl --user daemon-reload
ok "Unit updated"

# ─── 6. Restart ──────────────────────────────────────────────
info "Restarting $APP_NAME..."
if ! systemctl --user restart "$APP_NAME"; then
    err "Restart failed — check: systemctl --user status $APP_NAME"
    exit 1
fi

SERVER_PORT_CFG=$(read_config_key server_port "$DEFAULT_SERVER_PORT")
for _ in 1 2 3 4 5; do
    sleep 1
    if curl -fsS --max-time 3 "http://127.0.0.1:$SERVER_PORT_CFG/health" >/dev/null 2>&1; then
        ok "Server restarted and healthy"
        HEALTHY=1
        break
    fi
done
if [ -z "${HEALTHY:-}" ]; then
    err "Server did not report healthy on port $SERVER_PORT_CFG."
    info "Run 'bash $REPO_DIR/doctor.sh' for details."
    tail -20 "$LOG_DIR/server.log" 2>/dev/null | sed 's/^/    /' >&2
    exit 1
fi

# ─── 7. Extension ────────────────────────────────────────────
info "Rebuilding extension..."
bash "$REPO_DIR/build-extension.sh" "$APP_DIR/extension" >/dev/null || exit 1
ok "Extension rebuilt"

echo ""
echo -e "${GREEN}============== Update Complete ==============${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} Code pulled and dependencies updated"
echo -e "  ${GREEN}✓${NC} Server restarted and healthy"
echo -e "  ${GREEN}✓${NC} Extension rebuilt"
echo ""
echo -e "  ${YELLOW}⚠${NC} Reload the extension in your browser:"
echo -e "     Chrome:  chrome://extensions → Reload"
echo -e "     Firefox: about:debugging#/runtime/this-firefox → Reload"
echo ""
