#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="odoo-runbot-local"
APP_DIR="$HOME/.odoo-runbot-local"

# Try config.json first, then env var, then script directory
if [ -f "$APP_DIR/config.json" ]; then
    REPO_DIR="${REPO_DIR:-$(python3 -c "import json; print(json.load(open('$APP_DIR/config.json')).get('repo_path', '$SCRIPT_DIR'))" 2>/dev/null || echo "$SCRIPT_DIR")}"
else
    REPO_DIR="${REPO_DIR:-$SCRIPT_DIR}"
fi
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# Allow override via env var or first argument
if [ -n "$1" ]; then
    REPO_DIR="$1"
fi

if [ ! -d "$REPO_DIR/.git" ]; then
    err "No git repo found at $REPO_DIR"
    info "Usage: bash update.sh [path-to-repo]"
    info "       REPO_DIR=/path/to/repo bash update.sh"
    exit 1
fi

echo ""
info "Updating $APP_NAME from $REPO_DIR"
echo ""

# Step 1: Pull latest code
info "Pulling latest code..."
git -C "$REPO_DIR" pull
ok "Repo updated"

# Step 2: Update server
info "Updating server..."
cp "$REPO_DIR/server.py" "$APP_DIR/server.py"
ok "Server file updated"

# Step 3: Restart server
info "Restarting server..."
SERVICE_NAME="runbot-local"
if systemctl --user list-units --full --all 2>/dev/null | grep -q "odoo-runbot-local"; then
    SERVICE_NAME="odoo-runbot-local"
fi
systemctl --user restart "$SERVICE_NAME"
sleep 2
if systemctl --user is-active --quiet "$SERVICE_NAME"; then
    ok "Server restarted"
else
    err "Server failed to restart — check: systemctl --user status $SERVICE_NAME"
    exit 1
fi

# Step 4: Update extension
info "Updating extension..."
EXT_DEST="$APP_DIR/extension"
rm -rf "$EXT_DEST/chrome" "$EXT_DEST/firefox"
[ -d "$REPO_DIR/extension/chrome" ] && cp -r "$REPO_DIR/extension/chrome" "$EXT_DEST/chrome" || warn "Chrome extension not found in repo"
[ -d "$REPO_DIR/extension/firefox" ] && cp -r "$REPO_DIR/extension/firefox" "$EXT_DEST/firefox" || warn "Firefox extension not found in repo"
ok "Extension files updated"

# Step 5: Update setup/kill scripts
cp "$REPO_DIR/killport.sh" "$APP_DIR/killport.sh" 2>/dev/null || true
chmod +x "$APP_DIR/killport.sh" 2>/dev/null || true

# Step 6: Update systemd service file if changed
cp "$REPO_DIR/$APP_NAME.service" "$HOME/.config/systemd/user/$APP_NAME.service" 2>/dev/null && \
    systemctl --user daemon-reload && ok "Systemd unit updated" || true

# Summary
echo ""
echo -e "${GREEN}============== Update Complete ==============${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} Server updated and restarted"
echo -e "  ${GREEN}✓${NC} Extension files updated"
echo ""
echo -e "  ${YELLOW}⚠${NC} Reload the extension in your browser:"
echo -e "     Chrome:  chrome://extensions → Reload"
echo -e "     Firefox: about:debugging#/runtime/this-firefox → Reload"
echo ""
