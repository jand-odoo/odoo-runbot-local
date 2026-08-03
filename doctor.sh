#!/bin/bash
# Read-only diagnostic. Answers "why isn't it working" without changing anything.
# Run directly, or via `bash setup.sh --doctor`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PROBLEMS=0

pass()  { echo -e "  ${GREEN}✓${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; PROBLEMS=$((PROBLEMS + 1)); }
note()  { echo -e "  ${YELLOW}·${NC} $1"; }

section() { echo ""; echo -e "${BOLD}$1${NC}"; }

echo ""
echo -e "${BOLD}$APP_NAME — doctor${NC}"

# ─── Binaries ────────────────────────────────────────────────
section "Required tools"
for bin in git python3 psql createdb dropdb systemctl; do
    if command -v "$bin" >/dev/null 2>&1; then
        pass "$bin"
    else
        fail "$bin not found on PATH"
    fi
done
if command -v ss >/dev/null 2>&1 || command -v lsof >/dev/null 2>&1; then
    pass "port inspection (ss or lsof)"
else
    note "neither ss nor lsof found — port cleanup falls back to the recorded PID only"
fi

# ─── PostgreSQL ──────────────────────────────────────────────
section "PostgreSQL"
if pg_ready; then
    pass "server is accepting connections"
    DB_USER=$(read_config_key db_user "$(current_user)")
    if psql -U "$DB_USER" -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
        pass "role '$DB_USER' can connect"
    else
        fail "role '$DB_USER' cannot connect — create it: sudo -u postgres createuser -d -R -S $DB_USER"
    fi
else
    fail "server is not accepting connections (pg_isready failed)"
    if unit=$(pg_unit_name); then
        note "try: sudo systemctl start $unit"
    else
        note "no postgresql systemd unit found — is the server package installed?"
    fi
fi

# ─── Config ──────────────────────────────────────────────────
section "Configuration"
if [ ! -f "$CONFIG_FILE" ]; then
    fail "$CONFIG_FILE is missing — run: bash setup.sh"
    CHECKOUT_PATH=""
elif ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONFIG_FILE" >/dev/null 2>&1; then
    fail "$CONFIG_FILE is not valid JSON — the server is running on built-in defaults"
    CHECKOUT_PATH=""
else
    pass "$CONFIG_FILE parses"
    CFG_VERSION=$(read_config_key version "1")
    if [ "$CFG_VERSION" = "$CONFIG_VERSION" ]; then
        pass "schema version $CFG_VERSION"
    else
        fail "schema version $CFG_VERSION, expected $CONFIG_VERSION — run: bash update.sh"
    fi
    for key in checkout_path python db_user server_port odoo_port; do
        value=$(read_config_key "$key" "")
        if [ -n "$value" ]; then
            pass "$key = $value"
        else
            fail "$key is missing from config"
        fi
    done
    CHECKOUT_PATH=$(read_config_key checkout_path "")
    PYTHON_BIN=$(read_config_key python "")
    if [ -n "$PYTHON_BIN" ] && [ ! -x "$PYTHON_BIN" ]; then
        fail "configured python '$PYTHON_BIN' is not executable — re-run setup step 6"
    fi
fi

# ─── Repositories ────────────────────────────────────────────
section "Repositories"
if [ -z "${CHECKOUT_PATH:-}" ]; then
    note "skipped — no usable checkout_path in config"
else
    for repo in odoo enterprise; do
        path="$CHECKOUT_PATH/$repo"
        if [ ! -e "$path/.git" ]; then
            fail "$repo missing at $path"
            continue
        fi
        pass "$repo at $path"
        remotes=$(git -C "$path" remote 2>/dev/null | tr '\n' ' ')
        if [ -z "$remotes" ]; then
            fail "  no git remotes configured in $repo — commits can never be fetched"
        else
            pass "  remotes: $remotes"
            for remote in $remotes; do
                url=$(git -C "$path" remote get-url "$remote" 2>/dev/null)
                if repo_reachable "$url"; then
                    pass "  $remote reachable ($url)"
                else
                    fail "  $remote UNREACHABLE ($url) — check GitHub access for this repo"
                fi
            done
        fi
        if ! git -C "$path" rev-parse --verify HEAD >/dev/null 2>&1; then
            fail "  $repo has no valid HEAD"
        fi
    done
fi

# ─── Ports ───────────────────────────────────────────────────
section "Ports"
SERVER_PORT=$(read_config_key server_port "$DEFAULT_SERVER_PORT")
ODOO_PORT=$(read_config_key odoo_port "$DEFAULT_ODOO_PORT")

if port_in_use "$SERVER_PORT"; then
    if curl -fsS --max-time 5 "http://127.0.0.1:$SERVER_PORT/health" >/dev/null 2>&1; then
        pass "$SERVER_PORT is served by this server"
    elif curl -fsS --max-time 5 "http://127.0.0.1:$SERVER_PORT/status" >/dev/null 2>&1; then
        # /status but no /health means a pre-v2 server is still holding the port.
        fail "$SERVER_PORT is held by an OLD version of this server — run: bash update.sh"
    else
        fail "$SERVER_PORT is held by an unrelated process"
    fi
else
    fail "nothing listening on $SERVER_PORT — the server is not running"
fi

if port_in_use "$ODOO_PORT"; then
    note "$ODOO_PORT is in use (an Odoo instance is running, or another process holds it)"
else
    pass "$ODOO_PORT is free"
fi

# ─── Service ─────────────────────────────────────────────────
section "Service"
if [ -f "$UNIT_FILE" ]; then
    pass "unit installed at $UNIT_FILE"
    if grep -q '%h' "$UNIT_FILE"; then
        fail "  unit still contains an unexpanded %h — re-run setup or update"
    fi
else
    fail "unit missing at $UNIT_FILE — run: bash setup.sh"
fi

# An install from before the rename still runs under the old unit name and will
# fight this one for the port.
if systemctl --user is-active --quiet "runbot-local" 2>/dev/null; then
    fail "a legacy 'runbot-local' service is still running and holds the port"
    note "remove it: systemctl --user disable --now runbot-local && rm -f ~/.config/systemd/user/runbot-local.service"
    note "the old app directory ~/.runbot-local can be deleted once $APP_NAME works"
fi

if systemctl --user is-active --quiet "$APP_NAME" 2>/dev/null; then
    pass "$APP_NAME is active"
else
    fail "$APP_NAME is not active — start it: systemctl --user restart $APP_NAME"
    if [ -f "$LOG_DIR/server.log" ]; then
        echo ""
        note "last 20 lines of $LOG_DIR/server.log:"
        tail -20 "$LOG_DIR/server.log" | sed 's/^/      /'
    fi
fi

if loginctl show-user "$(current_user)" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
    pass "lingering enabled (service survives logout)"
else
    note "lingering disabled — the service stops when you log out"
fi

# ─── Health endpoint ─────────────────────────────────────────
section "Server health"
if health=$(curl -fsS --max-time 5 "http://127.0.0.1:$SERVER_PORT/health" 2>/dev/null); then
    problems=$(echo "$health" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("problems", [])))' 2>/dev/null)
    if [ -z "$problems" ]; then
        pass "server reports no problems"
    else
        while IFS= read -r line; do
            [ -n "$line" ] && fail "$line"
        done <<< "$problems"
    fi
else
    fail "could not reach GET /health on port $SERVER_PORT"
fi

# ─── Verdict ─────────────────────────────────────────────────
echo ""
if [ "$PROBLEMS" -eq 0 ]; then
    echo -e "${GREEN}All checks passed.${NC}"
    echo ""
    exit 0
fi
echo -e "${RED}$PROBLEMS problem(s) found.${NC} Fix the ✗ lines above, then re-run this doctor."
echo ""
exit 1
