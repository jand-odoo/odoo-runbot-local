#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="odoo-runbot-local"
APP_DIR="$HOME/.odoo-runbot-local"
CHECKOUT_DIR="$HOME/odoo/repositories/localdev/$APP_NAME"
REQUIREMENTS_TXT="$SCRIPT_DIR/requirements.txt"
SERVER_PY="$SCRIPT_DIR/server.py"
SERVICE_FILE="$SCRIPT_DIR/$APP_NAME.service"
DESKTOP_FILE="$SCRIPT_DIR/$APP_NAME.desktop"
EXTENSION_DIR="$SCRIPT_DIR/extension"
VENV_DIR="$APP_DIR/venv"
LOG_DIR="$APP_DIR/logs"
PYTHON="python3"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

prompt_yes_no() {
    local msg="$1"
    local default="${2:-Y}"
    local yn
    if [ "$default" = "Y" ]; then
        read -p "$msg [Y/n] " yn
        yn="${yn:-Y}"
    else
        read -p "$msg [y/N] " yn
        yn="${yn:-N}"
    fi
    case "$yn" in [Yy]* ) return 0;; * ) return 1;; esac
}

step() {
    local num=$1
    local title=$2
    echo ""
    echo -e "${CYAN}========== Step $num/$TOTAL_STEPS — $title ==========${NC}"
}

fail_step() {
    warn "Step failed. You can re-run setup.sh later — it's safe to skip completed steps."
}

# ─── Bootstrap: detect dev vs PO mode ────────────────────────
if [ -d "$HOME/odoo/repositories/odoo/odoo/.git" ] && [ -d "$HOME/odoo/repositories/odoo/enterprise/.git" ]; then
    DEV_MODE=true
    info "Detected dev setup — will create worktrees from ~/odoo/repositories/odoo/"
else
    DEV_MODE=false
    info "Detected fresh setup — will clone repos"
fi

# ─── Intro ───────────────────────────────────────────────────
clear
echo ""
echo -e "${CYAN}========== ${GREEN}$APP_NAME — Interactive Setup${CYAN} ==========${NC}"
echo ""
echo -e "  ${CYAN}*${NC} This script sets up everything needed to run Odoo"
echo -e "  ${CYAN}*${NC} branches locally from runbot.odoo.com."
echo ""
echo -e "  ${CYAN}*${NC} Some steps require sudo for installing packages."
echo -e "  ${CYAN}*${NC} You'll also need your GitHub SSH key ready."
echo ""
echo -e "  ${YELLOW}Press Enter to begin or Ctrl+C to abort.${NC}"
read -r

TOTAL_STEPS=10

# ────────────────────────────────────────────────────────────────
step 1 "System Packages"
# ────────────────────────────────────────────────────────────────
PKGS="postgresql postgresql-client libpq-dev python3 python3-pip python3-venv git curl"
missing=""
for pkg in $PKGS; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        missing="$missing $pkg"
    fi
done
if [ -n "$missing" ]; then
    info "Missing packages:$missing"
    if prompt_yes_no "Install missing packages?"; then
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends $missing
        ok "Packages installed"
    else
        warn "Skipping package installation. May cause issues later."
    fi
else
    ok "All system packages already installed"
fi

# ────────────────────────────────────────────────────────────────
step 2 "PostgreSQL Setup"
# ────────────────────────────────────────────────────────────────
if ! systemctl is-active --quiet postgresql 2>/dev/null; then
    info "Starting PostgreSQL..."
    sudo systemctl start postgresql || true
fi
sudo systemctl enable postgresql &>/dev/null || true

USER_NAME="${USER:-odoo}"
sudo -u postgres createuser -d -R -S "$USER_NAME" 2>/dev/null && ok "Postgres role '$USER_NAME' created" || ok "Postgres role '$USER_NAME' already exists"
createdb "$USER_NAME" 2>/dev/null && ok "Database '$USER_NAME' created" || ok "Database '$USER_NAME' already exists"

if psql -c 'SELECT 1' &>/dev/null; then
    ok "PostgreSQL is working"
else
    err "PostgreSQL not reachable. Check installation."
    fail_step
fi

# ────────────────────────────────────────────────────────────────
step 3 "GitHub SSH Access"
# ────────────────────────────────────────────────────────────────

# First check if already working
if ssh -T git@github.com 2>&1 | grep -q "successfully"; then
    ok "GitHub SSH access already configured"
    GITHUB_OK=true
else
    info "GitHub SSH access not configured yet. Setting up..."
    SSH_KEY="$HOME/.ssh/id_ed25519"
    if [ ! -f "$SSH_KEY" ]; then
        if prompt_yes_no "Generate a new SSH key?"; then
            read -p "Enter your GitHub email (or press Enter to skip): " SSH_EMAIL
            if [ -z "$SSH_EMAIL" ]; then
                ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "$USER@$(hostname)"
            else
                ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "$SSH_EMAIL"
            fi
            ok "SSH key generated"
        else
            warn "SSH key required for GitHub access."
        fi
    else
        ok "SSH key found at $SSH_KEY"
    fi

    for keyfile in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
        if [ -f "$keyfile" ]; then
            echo ""
            info "Your public key:"
            cat "$keyfile"
            break
        fi
    done

    if prompt_yes_no "Open GitHub SSH settings in browser?"; then
        xdg-open "https://github.com/settings/ssh/new" 2>/dev/null || true
    fi
    echo ""
    info "1. Click 'New SSH Key'"
    info "2. Paste the key above"
    info "3. Click 'Add SSH Key'"
    echo ""
    read -p "Press Enter after adding the key to continue..."

    for i in 1 2 3; do
        if ssh -T git@github.com 2>&1 | grep -q "successfully"; then
            break
        fi
        if [ "$i" -lt 3 ]; then
            warn "Not working yet. Let's try again (attempt $i/3)"
            read -p "Press Enter when ready..."
        fi
    done

    if ssh -T git@github.com 2>&1 | grep -q "successfully"; then
        ok "GitHub SSH access confirmed"
        GITHUB_OK=true
    else
        err "GitHub SSH access failed. Check your key and try again later."
        info "You can re-run setup.sh — it will skip completed steps."
        GITHUB_OK=false
    fi
fi

# ────────────────────────────────────────────────────────────────
step 4 "Clone / Create Worktrees"
# ────────────────────────────────────────────────────────────────
mkdir -p "$CHECKOUT_DIR"

if [ "$DEV_MODE" = true ]; then
    info "Dev mode: creating worktrees from ~/odoo/repositories/odoo/"
    BASE="$HOME/odoo/repositories/odoo"
    for repo in odoo enterprise; do
        target="$CHECKOUT_DIR/$repo"
        if [ -d "$target/.git" ]; then
            ok "Worktree $repo already exists at $target"
        else
            BASE_BRANCH="master"
            for try_branch in master main 18.0 saas-18.1; do
                if git -C "$BASE/$repo" rev-parse --verify "$try_branch" &>/dev/null; then
                    BASE_BRANCH="$try_branch"
                    break
                fi
            done
            if git -C "$BASE/$repo" worktree add -f "$target" "$BASE_BRANCH" 2>/dev/null; then
                ok "Created worktree for $repo at $target (branch: $BASE_BRANCH)"
            else
                warn "Could not create worktree for $repo"
            fi
        fi
    done
else
    info "PO mode: cloning repos"
    if [ ! -d "$CHECKOUT_DIR/odoo/.git" ]; then
        info "Cloning odoo/odoo..."
        git clone git@github.com:odoo/odoo.git "$CHECKOUT_DIR/odoo" 2>&1 | tail -5
        ok "odoo/odoo cloned"
    else
        ok "odoo already cloned"
    fi

    if [ ! -d "$CHECKOUT_DIR/enterprise/.git" ]; then
        info "Cloning odoo/enterprise..."
        git clone git@github.com:odoo/enterprise.git "$CHECKOUT_DIR/enterprise" 2>&1 | tail -5
        ok "odoo/enterprise cloned"
    else
        ok "enterprise already cloned"
    fi
fi

# ────────────────────────────────────────────────────────────────
step 5 "Odoo System Dependencies"
# ────────────────────────────────────────────────────────────────
if [ -f "$CHECKOUT_DIR/odoo/setup/debinstall.sh" ]; then
    info "Running odoo's debinstall.sh (requires sudo)..."
    DEB_SCRIPT="$CHECKOUT_DIR/odoo/setup/debinstall.sh"
    if prompt_yes_no "Run 'sudo $DEB_SCRIPT'?"; then
        sudo "$DEB_SCRIPT" -q
        ok "Odoo system dependencies installed"
    else
        warn "Skipping debinstall.sh — some Odoo features may not work"
    fi
else
    warn "debinstall.sh not found at $CHECKOUT_DIR/odoo/setup/debinstall.sh"
fi

# ────────────────────────────────────────────────────────────────
step 6 "Python Virtual Environment"
# ────────────────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created"
else
    ok "Virtual environment already exists"
fi

info "Installing Python packages..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_TXT"
"$VENV_DIR/bin/pip" install -r "$CHECKOUT_DIR/odoo/requirements.txt" 2>&1 | tail -3
ok "Python packages installed"

# ────────────────────────────────────────────────────────────────
step 7 "Configuration"
# ────────────────────────────────────────────────────────────────
mkdir -p "$APP_DIR"
cat > "$APP_DIR/config.json" <<EOF
{
  "checkout_path": "$CHECKOUT_DIR",
  "python": "$VENV_DIR/bin/python",
  "repo_path": "$SCRIPT_DIR"
}
EOF
ok "Config written to $APP_DIR/config.json"

# ────────────────────────────────────────────────────────────────
step 8 "Install Server as Systemd Service"
# ────────────────────────────────────────────────────────────────
mkdir -p "$APP_DIR/logs"
mkdir -p "$HOME/.config/systemd/user"

cp "$SERVER_PY" "$APP_DIR/server.py"
cp "$SCRIPT_DIR/killport.sh" "$APP_DIR/killport.sh"
cp "$SCRIPT_DIR/update.sh" "$APP_DIR/update.sh"
chmod +x "$APP_DIR/killport.sh" "$APP_DIR/update.sh"

sed "s|%h|$HOME|g" "$SERVICE_FILE" > "$HOME/.config/systemd/user/$APP_NAME.service"

systemctl --user daemon-reload
systemctl --user enable "$APP_NAME"
systemctl --user restart "$APP_NAME"
ok "Service installed and started"

sudo loginctl enable-linger "$USER" 2>/dev/null || true

sleep 2
if systemctl --user is-active --quiet "$APP_NAME"; then
    ok "Server is running on http://127.0.0.1:8765"
else
    warn "Service may not have started. Check: systemctl --user status $APP_NAME"
fi

# ────────────────────────────────────────────────────────────────
step 9 "Desktop Shortcut"
# ────────────────────────────────────────────────────────────────
DESKTOP_DEST="$HOME/.local/share/applications/$APP_NAME.desktop"
cp "$DESKTOP_FILE" "$DESKTOP_DEST"
chmod +x "$DESKTOP_DEST"
ok "Desktop shortcut created (visible in app menu)"

# ────────────────────────────────────────────────────────────────
step 10 "Browser Extension"
# ────────────────────────────────────────────────────────────────
EXT_SRC="$SCRIPT_DIR/extension"
EXT_CHROME="$APP_DIR/extension/chrome"
EXT_FIREFOX="$APP_DIR/extension/firefox"

rm -rf "$EXT_CHROME" "$EXT_FIREFOX"
[ -d "$EXT_SRC/chrome" ] && cp -r "$EXT_SRC/chrome" "$EXT_CHROME" || warn "Chrome extension source not found"
[ -d "$EXT_SRC/firefox" ] && cp -r "$EXT_SRC/firefox" "$EXT_FIREFOX" || warn "Firefox extension source not found"

ok "Extensions ready at $APP_DIR/extension/{chrome,firefox}/"

echo ""
info "To install the extension:"
info ""
info "  Chrome:"
info "    1. Open chrome://extensions"
info "    2. Enable 'Developer mode' (top right)"
info "    3. Click 'Load unpacked'"
info "    4. Select: $EXT_CHROME"
info ""
info "  Firefox:"
info "    1. Open the signed .xpi from the releases/ folder in the repo"
info "    2. Or temporarily from: $EXT_FIREFOX/manifest.json"
echo ""
# ────────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${GREEN}============== Setup Complete! ==============${NC}"
echo ""
echo -e "  ${GREEN}✓${NC} PostgreSQL ready"
echo -e "  ${GREEN}✓${NC} GitHub SSH access"
echo -e "  ${GREEN}✓${NC} Repos ready at $CHECKOUT_DIR"
echo -e "  ${GREEN}✓${NC} Odoo dependencies installed"
echo -e "  ${GREEN}✓${NC} Python venv ready"
echo -e "  ${GREEN}✓${NC} Server running on http://127.0.0.1:8765"
echo -e "  ${GREEN}✓${NC} Desktop shortcut created"
echo -e "  ${YELLOW}⚠${NC} Install the extension in your browser"
echo ""
echo -e "  ${CYAN}Next steps:${NC}"
echo -e "   1. Chrome: chrome://extensions → Load unpacked → $EXT_CHROME"
echo -e "      Firefox: open releases/odoo-runbot-local-*.xpi"
echo -e "   2. Open runbot.odoo.com"
echo -e "   3. Click '▶ Run locally' on any bundle page"
echo ""
echo -e "  ${YELLOW}To uninstall: bash $(dirname "$0")/uninstall.sh${NC}"
