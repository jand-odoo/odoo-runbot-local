#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="odoo-runbot-local"
APP_DIR="$HOME/.odoo-runbot-local"
CHECKOUT_DIR="$HOME/odoo/repositories/localdev/$APP_NAME"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }

prompt_yes_no() {
    local msg="$1"
    local default="${2:-N}"
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

clear
echo ""
echo -e "${RED}============== odoo-runbot-local — Uninstall ==============${NC}"
echo ""
echo -e "  ${YELLOW}This will remove the local branch runner and${NC}"
echo -e "  ${YELLOW}clean up associated files.${NC}"
echo ""
echo -e "  ${CYAN}The following are NOT removed:${NC}"
echo -e "  ${CYAN}*${NC} PostgreSQL (packages + databases)"
echo -e "  ${CYAN}*${NC} System packages (git, python, etc.)"
echo -e "  ${CYAN}*${NC} SSH keys and GitHub config"
echo -e "  ${CYAN}*${NC} Chrome extension (unload manually)"
echo ""

if ! prompt_yes_no "Continue with uninstall?"; then
    info "Cancelled."
    exit 0
fi

# Step 1: Stop running instance
echo ""
info "Stopping server..."
if systemctl --user is-active --quiet "$APP_NAME" 2>/dev/null; then
    systemctl --user stop "$APP_NAME" && ok "Server stopped"
else
    ok "Server not running"
fi

# Kill any process on port 8072
if command -v lsof &>/dev/null; then
    PIDS=$(lsof -t -i:8072 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        kill $PIDS 2>/dev/null || true
        ok "Killed process on port 8072"
    fi
fi

# Step 2: Remove systemd service
echo ""
info "Removing systemd service..."
systemctl --user disable "$APP_NAME" 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/$APP_NAME.service"
systemctl --user daemon-reload
ok "Systemd service removed"

# Step 3: Remove app data
echo ""
if prompt_yes_no "Remove $APP_DIR/ (logs, config, venv, server)?"; then
    rm -rf "$APP_DIR"
    ok "Removed $APP_DIR"
else
    warn "Skipped removing $APP_DIR"
fi

# Step 4: Remove checkout repos
echo ""
if [ -d "$CHECKOUT_DIR" ]; then
    if prompt_yes_no "Remove $CHECKOUT_DIR/ (worktrees/clones)?"; then
        rm -rf "$CHECKOUT_DIR"
        ok "Removed $CHECKOUT_DIR"
    else
        warn "Skipped removing $CHECKOUT_DIR"
    fi
fi

# Step 5: Remove desktop shortcut
echo ""
DESKTOP_DEST="$HOME/.local/share/applications/$APP_NAME.desktop"
if [ -f "$DESKTOP_DEST" ]; then
    rm -f "$DESKTOP_DEST"
    ok "Desktop shortcut removed"
fi

# Summary
echo ""
echo -e "${GREEN}============== Uninstall Complete ==============${NC}"
echo ""
echo -e "  ${RED}✗${NC} Server stopped and removed"
echo -e "  ${RED}✗${NC} Systemd service removed"
echo -e "  ${RED}✗${NC} App data cleaned"
echo -e "  ${RED}✗${NC} Desktop shortcut removed"
echo ""
echo -e "  ${YELLOW}Remaining (not removed):${NC}"
echo -e "  ${YELLOW}*${NC} PostgreSQL (packages + user/db)"
echo -e "  ${YELLOW}*${NC} System packages (git, python, etc.)"
echo -e "  ${YELLOW}*${NC} SSH keys and GitHub config"
echo -e "  ${YELLOW}*${NC} Chrome extension (unload in chrome://extensions)"
