#!/bin/bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h) echo "Usage: bash uninstall.sh [--yes]"; exit 0 ;;
        *) err "Unknown option: $1"; exit 2 ;;
    esac
    shift
done

CHECKOUT_DIR=$(read_config_key checkout_path "")

echo ""
echo -e "${RED}============== $APP_NAME — Uninstall ==============${NC}"
echo ""
echo -e "  ${CYAN}Will be removed:${NC}"
echo -e "  ${CYAN}*${NC} The systemd user service and its unit file"
echo -e "  ${CYAN}*${NC} $APP_DIR (server, venv, logs, config) — you will be asked"
[ -n "$CHECKOUT_DIR" ] && echo -e "  ${CYAN}*${NC} $CHECKOUT_DIR (repositories) — you will be asked"
echo -e "  ${CYAN}*${NC} The desktop shortcut"
echo ""
echo -e "  ${CYAN}Left untouched:${NC}"
echo -e "  ${CYAN}*${NC} PostgreSQL, its packages and your other databases"
echo -e "  ${CYAN}*${NC} System packages, SSH keys and GitHub config"
echo -e "  ${CYAN}*${NC} The browser extension (unload it manually)"
echo ""

if ! prompt_yes_no "Continue with uninstall?"; then
    info "Cancelled."
    exit 0
fi

# ─── Stop the Odoo instance we started ───────────────────────
# Only the pid we recorded — never whatever happens to hold the Odoo port,
# which may well be the developer's own Odoo.
echo ""
info "Stopping any running instance..."
if [ -f "$RUNNING_FILE" ]; then
    RUNNING_FILE="$RUNNING_FILE" python3 - <<'PY'
import json, os, signal
try:
    with open(os.environ["RUNNING_FILE"]) as fh:
        running = json.load(fh)
except Exception:
    raise SystemExit(0)

def start_time(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        return int(data[data.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None

pid, db = running.get("pid"), running.get("db_name")
if pid:
    recorded = running.get("start_time")
    # The pid may have been recycled since it was recorded; signalling it then
    # would kill an unrelated process.
    if recorded is not None and start_time(pid) != recorded:
        print(f"  pid {pid} now belongs to a different process — not signalling it")
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            print(f"  sent SIGTERM to process group {pid}")
        except OSError:
            print(f"  process {pid} was no longer running")
if db:
    print(f"  database to drop: {db}")
PY
    DB_NAME=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('db_name') or '')" "$RUNNING_FILE" 2>/dev/null)
    if [ -n "$DB_NAME" ]; then
        dropdb --if-exists -- "$DB_NAME" 2>/dev/null && ok "Dropped database $DB_NAME"
    fi
else
    ok "No instance was running"
fi

# ─── Service ─────────────────────────────────────────────────
echo ""
info "Removing systemd service..."
systemctl --user stop "$APP_NAME" 2>/dev/null || true
systemctl --user disable "$APP_NAME" 2>/dev/null || true
rm -f "$UNIT_FILE"
systemctl --user daemon-reload 2>/dev/null || true
ok "Systemd service removed"

# ─── App data ────────────────────────────────────────────────
echo ""
if [ -d "$APP_DIR" ]; then
    if prompt_yes_no "Remove $APP_DIR (logs, config, venv, server)?" N; then
        rm -rf "$APP_DIR"
        ok "Removed $APP_DIR"
    else
        warn "Kept $APP_DIR"
    fi
fi

# ─── Repositories ────────────────────────────────────────────
echo ""
if [ -n "$CHECKOUT_DIR" ] && [ -d "$CHECKOUT_DIR" ]; then
    warn "$CHECKOUT_DIR may contain git worktrees of your main repositories."
    if prompt_yes_no "Remove $CHECKOUT_DIR?" N; then
        # Detach worktrees properly so the parent repo does not keep stale entries.
        for repo in odoo enterprise; do
            if [ -f "$CHECKOUT_DIR/$repo/.git" ]; then
                git -C "$CHECKOUT_DIR/$repo" worktree remove --force "$CHECKOUT_DIR/$repo" 2>/dev/null \
                    || true
            fi
        done
        rm -rf "$CHECKOUT_DIR"
        ok "Removed $CHECKOUT_DIR"
        info "Run 'git worktree prune' in your main repos if any stale entries remain."
    else
        warn "Kept $CHECKOUT_DIR"
    fi
fi

# ─── Desktop shortcut ────────────────────────────────────────
echo ""
if [ -f "$DESKTOP_DEST" ]; then
    rm -f "$DESKTOP_DEST"
    ok "Desktop shortcut removed"
fi

echo ""
echo -e "${GREEN}============== Uninstall Complete ==============${NC}"
echo ""
echo -e "  ${YELLOW}Still installed (removed manually if you want them gone):${NC}"
echo -e "  ${YELLOW}*${NC} PostgreSQL and your databases"
echo -e "  ${YELLOW}*${NC} System packages, SSH keys and GitHub config"
echo -e "  ${YELLOW}*${NC} The browser extension — unload it in chrome://extensions"
echo ""
