#!/bin/bash
# Shared constants and helpers for setup.sh / update.sh / uninstall.sh.
# Sourced, never executed directly.
#
# Constants below are consumed by the sourcing scripts, so shellcheck cannot see
# their uses from here.
# shellcheck disable=SC2034

APP_NAME="odoo-runbot-local"
APP_DIR="$HOME/.$APP_NAME"
CONFIG_FILE="$APP_DIR/config.json"
LOG_DIR="$APP_DIR/logs"
VENV_DIR="$APP_DIR/venv"
RUNNING_FILE="$APP_DIR/running.json"
UNIT_FILE="$HOME/.config/systemd/user/$APP_NAME.service"
DESKTOP_DEST="$HOME/.local/share/applications/$APP_NAME.desktop"

# No assumption about the user's repo layout — XDG data dir by default.
DEFAULT_CHECKOUT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_NAME/checkout"

DEFAULT_SERVER_PORT=8765
DEFAULT_ODOO_PORT=8072
CONFIG_VERSION=2

# Never prompt for git credentials: a missing credential must fail, not hang.
export GIT_TERMINAL_PROMPT=0

if [ -t 1 ]; then
    GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
    GREEN=''; YELLOW=''; RED=''; CYAN=''; BOLD=''; NC=''
fi

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
err()   { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# ─── Interaction ─────────────────────────────────────────────
# ASSUME_YES=1 makes every prompt take its default without reading stdin.
ASSUME_YES="${ASSUME_YES:-0}"

prompt_yes_no() {
    local msg="$1" default="${2:-Y}" yn
    if [ "$ASSUME_YES" = "1" ]; then
        [ "$default" = "Y" ]
        return $?
    fi
    if [ ! -t 0 ]; then
        warn "No terminal available; assuming '$default' for: $msg"
        [ "$default" = "Y" ]
        return $?
    fi
    if [ "$default" = "Y" ]; then
        read -r -p "$msg [Y/n] " yn; yn="${yn:-Y}"
    else
        read -r -p "$msg [y/N] " yn; yn="${yn:-N}"
    fi
    case "$yn" in [Yy]*) return 0;; *) return 1;; esac
}

prompt_value() {
    # prompt_value <message> <default> -> echoes the answer
    local msg="$1" default="$2" ans
    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        echo "$default"; return 0
    fi
    read -r -p "$msg [$default] " ans
    echo "${ans:-$default}"
}

# ─── Config access ───────────────────────────────────────────
# Reads one key from config.json without assuming python-json is importable
# in any particular interpreter beyond the system python3.
read_config_key() {
    local key="$1" default="${2:-}"
    [ -f "$CONFIG_FILE" ] || { echo "$default"; return 0; }
    CONFIG_FILE="$CONFIG_FILE" KEY="$key" DEFAULT="$default" python3 - <<'PY' 2>/dev/null || echo "$default"
import json, os, sys
try:
    with open(os.environ["CONFIG_FILE"]) as fh:
        value = json.load(fh).get(os.environ["KEY"])
except Exception:
    value = None
print(os.environ["DEFAULT"] if value is None else value)
PY
}

# ─── Environment probes ──────────────────────────────────────
current_user() {
    # $USER is unset in a lingering systemd user unit; /etc/passwd is not.
    id -un 2>/dev/null || echo "${USER:-}"
}

detect_pkg_manager() {
    for mgr in apt-get dnf pacman zypper; do
        if command -v "$mgr" >/dev/null 2>&1; then echo "$mgr"; return 0; fi
    done
    echo "unknown"
}

# Generic dependency name -> distro package name.
# Echoes nothing when the dependency does not exist / is not needed there.
pkg_name_for() {
    local mgr="$1" dep="$2"
    case "$mgr:$dep" in
        apt-get:postgresql)      echo "postgresql" ;;
        apt-get:postgresql-client) echo "postgresql-client" ;;
        apt-get:libpq-dev)       echo "libpq-dev" ;;
        apt-get:python3)         echo "python3" ;;
        apt-get:python3-venv)    echo "python3-venv" ;;
        apt-get:python3-pip)     echo "python3-pip" ;;
        apt-get:git)             echo "git" ;;
        apt-get:curl)            echo "curl" ;;
        apt-get:iproute2)        echo "iproute2" ;;

        dnf:postgresql)          echo "postgresql-server" ;;
        dnf:postgresql-client)   echo "postgresql" ;;
        dnf:libpq-dev)           echo "libpq-devel" ;;
        dnf:python3)             echo "python3" ;;
        dnf:python3-venv)        echo "" ;;   # bundled with python3
        dnf:python3-pip)         echo "python3-pip" ;;
        dnf:git)                 echo "git" ;;
        dnf:curl)                echo "curl" ;;
        dnf:iproute2)            echo "iproute" ;;

        pacman:postgresql)       echo "postgresql" ;;
        pacman:postgresql-client) echo "postgresql-libs" ;;
        pacman:libpq-dev)        echo "postgresql-libs" ;;
        pacman:python3)          echo "python" ;;
        pacman:python3-venv)     echo "" ;;
        pacman:python3-pip)      echo "python-pip" ;;
        pacman:git)              echo "git" ;;
        pacman:curl)             echo "curl" ;;
        pacman:iproute2)         echo "iproute2" ;;

        zypper:postgresql)       echo "postgresql-server" ;;
        zypper:postgresql-client) echo "postgresql" ;;
        zypper:libpq-dev)        echo "postgresql-devel" ;;
        zypper:python3)          echo "python3" ;;
        zypper:python3-venv)     echo "" ;;
        zypper:python3-pip)      echo "python3-pip" ;;
        zypper:git)              echo "git" ;;
        zypper:curl)             echo "curl" ;;
        zypper:iproute2)         echo "iproute2" ;;

        *) echo "" ;;
    esac
}

pkg_install() {
    # pkg_install <mgr> <pkg...>
    local mgr="$1"; shift
    [ $# -gt 0 ] || return 0
    case "$mgr" in
        apt-get) sudo apt-get update && sudo apt-get install -y --no-install-recommends "$@" ;;
        dnf)     sudo dnf install -y "$@" ;;
        pacman)  sudo pacman -S --needed --noconfirm "$@" ;;
        zypper)  sudo zypper --non-interactive install "$@" ;;
        *)       return 1 ;;
    esac
}

# The binary each generic dependency provides, so an unknown package manager can
# still report exactly what is missing. Empty means "no binary to probe" —
# libpq-dev and python3-venv install headers and modules, not commands.
dep_binary() {
    case "$1" in
        postgresql-client) echo "psql" ;;
        python3)           echo "python3" ;;
        python3-pip)       echo "pip3" ;;
        git)               echo "git" ;;
        curl)              echo "curl" ;;
        iproute2)          echo "ss" ;;
        *)                 echo "" ;;
    esac
}

pkg_installed() {
    local mgr="$1" pkg="$2"
    case "$mgr" in
        apt-get)       dpkg -s "$pkg" >/dev/null 2>&1 ;;
        dnf|zypper)    rpm -q "$pkg" >/dev/null 2>&1 ;;
        pacman)        pacman -Q "$pkg" >/dev/null 2>&1 ;;
        *)             return 1 ;;
    esac
}

# A dependency counts as present if its binary is on PATH, or the package
# manager says the package is installed. Probing only the binary would keep
# reinstalling packages that ship no command (libpq-dev) or install their
# binaries outside PATH (the postgresql server on Debian).
dep_present() {
    local mgr="$1" dep="$2" bin pkg
    bin=$(dep_binary "$dep")
    if [ -n "$bin" ] && command -v "$bin" >/dev/null 2>&1; then
        return 0
    fi
    if [ "$dep" = "postgresql" ] && pg_ready; then
        return 0
    fi
    pkg=$(pkg_name_for "$mgr" "$dep")
    [ -n "$pkg" ] && pkg_installed "$mgr" "$pkg"
}

pg_ready() {
    if command -v pg_isready >/dev/null 2>&1; then
        pg_isready -q 2>/dev/null
    else
        psql -c 'SELECT 1' >/dev/null 2>&1
    fi
}

pg_unit_name() {
    local unit
    for unit in postgresql postgresql.service postgresql@.service; do
        if systemctl list-unit-files "$unit" >/dev/null 2>&1 \
           && systemctl list-unit-files --no-legend "$unit" 2>/dev/null | grep -q .; then
            echo "${unit%.service}"; return 0
        fi
    done
    # Versioned units: postgresql@16-main.service on Debian, postgresql-16 on Fedora.
    unit=$(systemctl list-unit-files --no-legend 2>/dev/null \
           | awk '$1 ~ /^postgresql[@-][0-9]/ {print $1; exit}')
    [ -n "$unit" ] && { echo "${unit%.service}"; return 0; }
    return 1
}

port_in_use() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN
    elif command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    else
        # No tool available: assume free rather than block setup on it.
        return 1
    fi
}

# ─── Git / GitHub ────────────────────────────────────────────
GIT_SSH_PROBE_OPTS=(-o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10)

github_ssh_works() {
    # `ssh -T git@github.com` exits 1 even when authentication succeeds, so the
    # banner is the only reliable signal. It must be captured before grepping:
    # under `set -o pipefail` a pipeline would inherit ssh's exit 1 and report
    # failure on a working key.
    # accept-new + BatchMode is what stops the invisible host-key prompt hang.
    local output
    output=$(timeout 25 ssh "${GIT_SSH_PROBE_OPTS[@]}" -T git@github.com 2>&1) || true
    case "$output" in
        *"successfully authenticated"*) return 0 ;;
        *) return 1 ;;
    esac
}

github_https_works() {
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        return 0
    fi
    # A configured credential helper answers without a terminal. Captured for
    # the same pipefail reason as above.
    local output
    output=$(printf 'protocol=https\nhost=github.com\n\n' \
             | timeout 15 git credential fill 2>/dev/null) || true
    case "$output" in
        *"password="*) return 0 ;;
        *) return 1 ;;
    esac
}

# repo_url <protocol> <owner/name>  ->  a clone URL
repo_url() {
    case "$1" in
        ssh)   echo "git@github.com:$2.git" ;;
        https) echo "https://github.com/$2.git" ;;
        *)     return 1 ;;
    esac
}

repo_reachable() {
    timeout 60 git ls-remote --exit-code "$1" HEAD >/dev/null 2>&1
}

# ─── Step tracking ───────────────────────────────────────────
# run_step records the real outcome of every step so the final summary
# cannot claim success for something that failed.
STEP_NAMES=()
STEP_STATES=()   # ok | fail | skip

record_step() { STEP_NAMES+=("$1"); STEP_STATES+=("$2"); }

step_header() {
    echo ""
    echo -e "${CYAN}========== Step $1/$2 — $3 ==========${NC}"
}

# run_step <name> <function> — runs the function, records ok/fail, never aborts.
run_step() {
    local name="$1" fn="$2"
    if "$fn"; then
        record_step "$name" ok
        return 0
    fi
    record_step "$name" fail
    warn "Step '$name' failed. Fix the cause above and re-run — setup is idempotent."
    return 1
}

any_step_failed() {
    local state
    for state in "${STEP_STATES[@]}"; do
        [ "$state" = "fail" ] && return 0
    done
    return 1
}

print_summary() {
    local i state symbol
    echo ""
    echo -e "${BOLD}============== Summary ==============${NC}"
    for i in "${!STEP_NAMES[@]}"; do
        state="${STEP_STATES[$i]}"
        case "$state" in
            ok)   symbol="${GREEN}✓${NC}" ;;
            fail) symbol="${RED}✗${NC}" ;;
            *)    symbol="${YELLOW}⊘${NC}" ;;
        esac
        echo -e "  $symbol ${STEP_NAMES[$i]}"
    done
    echo ""
}

# Renders the systemd unit, expanding %h ourselves so the installed copy is
# identical whether it came from setup.sh or update.sh.
install_unit_file() {
    local src="$1"
    mkdir -p "$(dirname "$UNIT_FILE")"
    HOME_DIR="$HOME" python3 - "$src" "$UNIT_FILE" <<'PY'
import os, sys
src, dest = sys.argv[1], sys.argv[2]
with open(src) as fh:
    content = fh.read()
with open(dest, "w") as fh:
    fh.write(content.replace("%h", os.environ["HOME_DIR"]))
PY
}
