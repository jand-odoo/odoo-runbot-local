#!/bin/bash
# Interactive installer. Idempotent: safe to re-run after fixing a failed step.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

REQUIREMENTS_TXT="$SCRIPT_DIR/requirements.txt"
SERVER_PY="$SCRIPT_DIR/server.py"
SERVICE_FILE="$SCRIPT_DIR/$APP_NAME.service"
DESKTOP_FILE="$SCRIPT_DIR/$APP_NAME.desktop"
SETUP_LOG="${TMPDIR:-/tmp}/$APP_NAME-setup.log"

TOTAL_STEPS=10

usage() {
    cat <<EOF
Usage: bash setup.sh [options]

  --yes         Non-interactive; take the default for every prompt.
  --doctor      Run diagnostics only, change nothing, exit non-zero on problems.
  --help        Show this message.

Environment:
  RUNBOT_LOCAL_CHECKOUT   Force the checkout directory instead of discovering it.
  ODOO_PORT               Port for the Odoo instance (default $DEFAULT_ODOO_PORT).

Setup is idempotent — completed steps are detected and skipped.
Verbose command output is written to $SETUP_LOG.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)  ASSUME_YES=1 ;;
        --doctor)  exec bash "$SCRIPT_DIR/doctor.sh" ;;
        --help|-h) usage; exit 0 ;;
        *) err "Unknown option: $1"; usage; exit 2 ;;
    esac
    shift
done

# run_logged <description> <command...> — captures output, shows it only on failure.
run_logged() {
    local desc="$1"; shift
    echo "=== $desc ===" >> "$SETUP_LOG"
    if "$@" >> "$SETUP_LOG" 2>&1; then
        return 0
    fi
    err "$desc failed. Last 20 lines:"
    tail -20 "$SETUP_LOG" | sed 's/^/    /' >&2
    return 1
}

# ─── Resolved during the run ─────────────────────────────────
CHECKOUT_DIR=""
MODE=""            # worktree | clone
GIT_PROTOCOL=""
DB_USER="$(current_user)"
ODOO_PORT="${ODOO_PORT:-$DEFAULT_ODOO_PORT}"
SERVER_PORT="$DEFAULT_SERVER_PORT"

: > "$SETUP_LOG"

echo ""
echo -e "${CYAN}========== ${GREEN}$APP_NAME — Setup${CYAN} ==========${NC}"
echo ""
echo -e "  Sets up everything needed to run Odoo branches from runbot.odoo.com locally."
echo ""
echo -e "  ${CYAN}*${NC} Some steps need sudo (system packages, PostgreSQL role)."
echo -e "  ${CYAN}*${NC} You need GitHub access to ${BOLD}odoo/odoo${NC}, ${BOLD}odoo/enterprise${NC} and ${BOLD}odoo-dev${NC}."
echo -e "  ${CYAN}*${NC} Detailed output goes to $SETUP_LOG"
echo ""
if [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
    read -r -p "Press Enter to begin, or Ctrl+C to abort. "
fi

# ────────────────────────────────────────────────────────────
step_system_packages() {
    step_header 1 "$TOTAL_STEPS" "System Packages"

    local mgr deps dep bin pkg missing_pkgs="" missing_bins=""
    mgr=$(detect_pkg_manager)
    deps="postgresql postgresql-client libpq-dev python3 python3-venv python3-pip git curl iproute2"

    if [ "$mgr" = "unknown" ]; then
        warn "Unrecognised package manager — checking for the binaries instead."
        for dep in $deps; do
            bin=$(dep_binary "$dep")
            [ -n "$bin" ] || continue
            command -v "$bin" >/dev/null 2>&1 || missing_bins="$missing_bins $bin"
        done
        if [ -n "$missing_bins" ]; then
            err "Missing required tools:$missing_bins"
            info "Install them with your distribution's package manager, then re-run setup."
            return 1
        fi
        ok "All required tools are present"
        return 0
    fi

    info "Package manager: $mgr"
    for dep in $deps; do
        dep_present "$mgr" "$dep" && continue
        pkg=$(pkg_name_for "$mgr" "$dep")
        [ -n "$pkg" ] || continue
        case " $missing_pkgs " in *" $pkg "*) ;; *) missing_pkgs="$missing_pkgs $pkg" ;; esac
    done

    if [ -z "$missing_pkgs" ]; then
        ok "All system packages already installed"
        return 0
    fi

    info "Missing packages:$missing_pkgs"
    if ! prompt_yes_no "Install them now?"; then
        err "Cannot continue without these packages."
        return 1
    fi
    # shellcheck disable=SC2086
    run_logged "Installing packages" pkg_install "$mgr" $missing_pkgs || return 1
    ok "Packages installed"
}

# ────────────────────────────────────────────────────────────
step_postgresql() {
    step_header 2 "$TOTAL_STEPS" "PostgreSQL"

    local unit
    if ! pg_ready; then
        if unit=$(pg_unit_name); then
            info "Starting $unit..."
            # shellcheck disable=SC2024  # $SETUP_LOG is owned by the invoking user
            sudo systemctl start "$unit" >> "$SETUP_LOG" 2>&1 || true
            # shellcheck disable=SC2024  # $SETUP_LOG is owned by the invoking user
            sudo systemctl enable "$unit" >> "$SETUP_LOG" 2>&1 || true
            sleep 2
        fi
    fi

    if ! pg_ready; then
        err "PostgreSQL is not accepting connections."
        case "$(detect_pkg_manager)" in
            dnf)    info "Fedora/RHEL needs one-time init: sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql" ;;
            pacman) info "Arch needs one-time init: sudo -u postgres initdb -D /var/lib/postgres/data && sudo systemctl enable --now postgresql" ;;
            zypper) info "openSUSE: sudo systemctl enable --now postgresql" ;;
            *)      info "Start it with: sudo systemctl start postgresql" ;;
        esac
        return 1
    fi
    ok "PostgreSQL is accepting connections"

    DB_USER="$(current_user)"
    if [ -z "$DB_USER" ]; then
        err "Could not determine the current username."
        return 1
    fi

    if psql -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
        ok "Role '$DB_USER' can connect"
    else
        info "Creating PostgreSQL role '$DB_USER' (needs sudo)..."
        # shellcheck disable=SC2024  # $SETUP_LOG is owned by the invoking user
        sudo -u postgres createuser -d -R -S "$DB_USER" >> "$SETUP_LOG" 2>&1 || true
        if ! psql -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
            err "Role '$DB_USER' still cannot connect to PostgreSQL."
            info "Create it manually: sudo -u postgres createuser -d -R -S $DB_USER"
            return 1
        fi
        ok "Role '$DB_USER' created"
    fi

    # odoo-bin expects a database named after the role to exist for maintenance queries.
    createdb "$DB_USER" >> "$SETUP_LOG" 2>&1 && ok "Database '$DB_USER' created" \
        || ok "Database '$DB_USER' already exists"
}

# ────────────────────────────────────────────────────────────
step_github_access() {
    step_header 3 "$TOTAL_STEPS" "GitHub Access"

    info "Probing SSH access to github.com..."
    if github_ssh_works; then
        GIT_PROTOCOL="ssh"
        ok "SSH access to GitHub works"
    else
        info "SSH did not authenticate. Checking for HTTPS credentials..."
        if github_https_works; then
            GIT_PROTOCOL="https"
            ok "HTTPS credentials found — will clone over HTTPS"
        else
            warn "Neither SSH nor HTTPS credentials are working yet."
            setup_ssh_key || return 1
            if github_ssh_works; then
                GIT_PROTOCOL="ssh"
                ok "SSH access to GitHub works"
            else
                err "Still cannot authenticate to GitHub."
                info "Either add an SSH key at https://github.com/settings/ssh/new"
                info "or authenticate over HTTPS with: gh auth login"
                return 1
            fi
        fi
    fi

    # Access must be verified, not assumed: enterprise is private and a silent
    # failure here is what makes the tool look broken much later.
    local repo url failed=0
    for repo in odoo/odoo odoo/enterprise odoo-dev/odoo odoo-dev/enterprise; do
        url=$(repo_url "$GIT_PROTOCOL" "$repo")
        if repo_reachable "$url"; then
            ok "$repo reachable"
        else
            err "$repo NOT reachable ($url)"
            failed=1
        fi
    done

    if [ "$failed" -eq 1 ]; then
        err "Your GitHub account is missing access to at least one required repository."
        info "odoo/enterprise and odoo-dev are Odoo-employee repositories — request access,"
        info "then re-run setup. Everything else is already configured."
        return 1
    fi
    ok "All required repositories are reachable over $GIT_PROTOCOL"
}

setup_ssh_key() {
    local key="$HOME/.ssh/id_ed25519" pub email
    if [ ! -f "$key" ]; then
        if ! prompt_yes_no "Generate a new SSH key at $key?"; then
            return 1
        fi
        email=$(prompt_value "GitHub email for the key comment" "$(current_user)@$(hostname)")
        mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
        ssh-keygen -t ed25519 -f "$key" -N "" -C "$email" >> "$SETUP_LOG" 2>&1 || return 1
        ok "SSH key generated"
    fi

    for pub in "$HOME/.ssh"/*.pub; do
        [ -f "$pub" ] || continue
        echo ""
        info "Public key ($pub):"
        cat "$pub"
        break
    done

    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        warn "Non-interactive mode: add the key above to GitHub, then re-run setup."
        return 1
    fi

    if prompt_yes_no "Open the GitHub SSH settings page?"; then
        xdg-open "https://github.com/settings/ssh/new" >/dev/null 2>&1 || true
    fi
    echo ""
    info "Add the key above at https://github.com/settings/ssh/new"
    read -r -p "Press Enter once the key has been added... "
    return 0
}

# ────────────────────────────────────────────────────────────
discover_checkout_dir() {
    # 1. Explicit override.
    if [ -n "${RUNBOT_LOCAL_CHECKOUT:-}" ]; then
        CHECKOUT_DIR="$RUNBOT_LOCAL_CHECKOUT"
        info "Using RUNBOT_LOCAL_CHECKOUT=$CHECKOUT_DIR"
        return 0
    fi

    # 2. A previous install.
    local existing
    existing=$(read_config_key checkout_path "")
    if [ -n "$existing" ]; then
        CHECKOUT_DIR="$existing"
        info "Reusing checkout path from config: $CHECKOUT_DIR"
        return 0
    fi

    CHECKOUT_DIR="$DEFAULT_CHECKOUT_DIR"
    return 0
}

# Finds directories that already hold both odoo/ and enterprise/ clones,
# so worktrees can be created instead of cloning multiple GB again.
find_base_repos() {
    local candidate
    for candidate in \
        "$HOME/odoo/repositories/odoo" \
        "$HOME/odev/worktrees"/* \
        "$HOME/src/odoo" \
        "$HOME/odoo" \
        "$HOME/Documents/odoo"
    do
        [ -d "$candidate" ] || continue
        if [ -e "$candidate/odoo/.git" ] && [ -e "$candidate/enterprise/.git" ]; then
            echo "$candidate"
        fi
    done
}

step_repositories() {
    step_header 4 "$TOTAL_STEPS" "Repositories"

    discover_checkout_dir

    if [ -e "$CHECKOUT_DIR/odoo/.git" ] && [ -e "$CHECKOUT_DIR/enterprise/.git" ]; then
        ok "Both repositories already present at $CHECKOUT_DIR"
        MODE=$(detect_mode)
        info "Detected $MODE mode"
        verify_repos || return 1
        ensure_dev_remotes || return 1
        return 0
    fi

    local bases base
    bases=$(find_base_repos | head -5)

    if [ -n "$bases" ]; then
        base=$(echo "$bases" | head -1)
        info "Found existing Odoo repositories at: $base"
        if prompt_yes_no "Create lightweight worktrees from them (fast, saves disk)?"; then
            MODE="worktree"
            create_worktrees "$base" || return 1
            ensure_dev_remotes || return 1
            return 0
        fi
    fi

    MODE="clone"
    clone_repos || return 1
    ensure_dev_remotes || return 1
}

# A worktree stores .git as a file pointing at the parent repo; a clone as a
# directory. Reading it from disk beats trusting a possibly stale config value.
detect_mode() {
    if [ -f "$CHECKOUT_DIR/odoo/.git" ]; then echo "worktree"; else echo "clone"; fi
}

# Existing repositories may be broken rather than merely present — a worktree
# whose parent repo moved, or an interrupted clone, both leave a .git behind.
verify_repos() {
    local repo path broken=0
    for repo in odoo enterprise; do
        path="$CHECKOUT_DIR/$repo"
        if ! git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
            err "$path exists but is not a usable git repository."
            if [ -f "$path/.git" ]; then
                info "It looks like a worktree whose parent repository moved or was deleted."
                info "Remove it and re-run setup to recreate it: rm -rf '$path'"
            else
                info "Remove it and re-run setup to re-clone: rm -rf '$path'"
            fi
            broken=1
        fi
    done
    [ "$broken" -eq 0 ]
}

create_worktrees() {
    local base="$1" repo target ref
    mkdir -p "$CHECKOUT_DIR"
    for repo in odoo enterprise; do
        target="$CHECKOUT_DIR/$repo"
        if [ -e "$target/.git" ]; then
            ok "Worktree $repo already exists"
            continue
        fi
        ref=""
        for try in master main 18.0 saas-18.4 HEAD; do
            if git -C "$base/$repo" rev-parse --verify --quiet "$try" >/dev/null 2>&1; then
                ref="$try"; break
            fi
        done
        if [ -z "$ref" ]; then
            err "No usable base ref found in $base/$repo"
            return 1
        fi
        # --detach: the server only ever checks out raw commits, and attaching a
        # branch here would lock that branch out of the user's main clone.
        if ! run_logged "Creating $repo worktree" \
             git -C "$base/$repo" worktree add --detach "$target" "$ref"; then
            return 1
        fi
        ok "Worktree $repo created at $target (detached at $ref)"
    done
}

clone_repos() {
    local repo url
    mkdir -p "$CHECKOUT_DIR"
    for repo in odoo enterprise; do
        if [ -e "$CHECKOUT_DIR/$repo/.git" ]; then
            ok "$repo already cloned"
            continue
        fi
        url=$(repo_url "$GIT_PROTOCOL" "odoo/$repo")
        info "Cloning odoo/$repo — this downloads several GB and takes a while..."
        if ! run_logged "Cloning odoo/$repo" \
             git clone --progress "$url" "$CHECKOUT_DIR/$repo"; then
            err "Clone of odoo/$repo failed. Setup cannot continue."
            return 1
        fi
        ok "odoo/$repo cloned"
    done
}

# Runbot bundles for dev branches reference commits that live in odoo-dev, not
# odoo. Without these remotes the server can never resolve those commits.
ensure_dev_remotes() {
    local repo path url proto="${GIT_PROTOCOL:-ssh}"
    for repo in odoo enterprise; do
        path="$CHECKOUT_DIR/$repo"
        [ -e "$path/.git" ] || continue
        if git -C "$path" remote get-url dev >/dev/null 2>&1; then
            ok "$repo already has a 'dev' remote"
            continue
        fi
        url=$(repo_url "$proto" "odoo-dev/$repo")
        if git -C "$path" remote add dev "$url" >> "$SETUP_LOG" 2>&1; then
            # Never auto-fetch: the server fetches individual commits on demand.
            ok "$repo: added 'dev' remote ($url)"
        else
            warn "$repo: could not add the 'dev' remote — dev-branch bundles may not resolve"
        fi
    done
}

# ────────────────────────────────────────────────────────────
step_odoo_deps() {
    step_header 5 "$TOTAL_STEPS" "Odoo System Dependencies"

    local script="$CHECKOUT_DIR/odoo/setup/debinstall.sh"
    if [ "$(detect_pkg_manager)" != "apt-get" ]; then
        warn "debinstall.sh is Debian-only; skipping on this distribution."
        info "Install Odoo's system libraries manually if wkhtmltopdf/lxml features misbehave."
        return 0
    fi
    if [ ! -f "$script" ]; then
        warn "debinstall.sh not found at $script — skipping."
        return 0
    fi
    # Optional step: never fail the whole run over it. In a non-interactive run
    # sudo cannot prompt, so skip rather than fail on a password we cannot give.
    if [ "$ASSUME_YES" = "1" ] && ! sudo -n true 2>/dev/null; then
        warn "Skipping debinstall.sh: non-interactive run and sudo needs a password."
        info "Run it yourself later if Odoo reports missing system libraries:"
        info "  sudo $script -q"
        return 0
    fi
    if ! prompt_yes_no "Run 'sudo $script -q' to install Odoo's system libraries?"; then
        warn "Skipped — some Odoo features may not work."
        return 0
    fi
    run_logged "Running debinstall.sh" sudo "$script" -q || return 1
    ok "Odoo system dependencies installed"
}

# ────────────────────────────────────────────────────────────
step_venv() {
    step_header 6 "$TOTAL_STEPS" "Python Virtual Environment"

    # A directory is not proof of a working venv: a system Python upgrade leaves
    # the interpreter symlink dangling, and every later pip call then fails.
    if [ -d "$VENV_DIR" ] && ! "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
        warn "Virtualenv at $VENV_DIR is broken (its interpreter does not run) — recreating."
        rm -rf "$VENV_DIR"
    fi

    if [ ! -d "$VENV_DIR" ]; then
        run_logged "Creating virtualenv" python3 -m venv "$VENV_DIR" || return 1
        ok "Virtual environment created"
    else
        ok "Virtual environment already usable"
    fi

    run_logged "Upgrading pip" "$VENV_DIR/bin/pip" install --upgrade pip || return 1
    run_logged "Installing server requirements" \
        "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS_TXT" || return 1

    if [ -f "$CHECKOUT_DIR/odoo/requirements.txt" ]; then
        if ! run_logged "Installing Odoo requirements" \
             "$VENV_DIR/bin/pip" install -r "$CHECKOUT_DIR/odoo/requirements.txt"; then
            err "Odoo's Python requirements failed to install — Odoo will not start."
            return 1
        fi
    else
        err "$CHECKOUT_DIR/odoo/requirements.txt not found — is the odoo repo complete?"
        return 1
    fi
    ok "Python packages installed"
}

# ────────────────────────────────────────────────────────────
step_config() {
    step_header 7 "$TOTAL_STEPS" "Configuration"

    mkdir -p "$APP_DIR"
    [ -n "$MODE" ] || MODE="clone"

    CONFIG_FILE="$CONFIG_FILE" \
    VERSION="$CONFIG_VERSION" MODE="$MODE" CHECKOUT="$CHECKOUT_DIR" \
    PYTHON="$VENV_DIR/bin/python" REPO="$SCRIPT_DIR" PROTOCOL="${GIT_PROTOCOL:-ssh}" \
    SERVER_PORT="$SERVER_PORT" ODOO_PORT="$ODOO_PORT" DB_USER="$DB_USER" \
    python3 - <<'PY' || return 1
import json, os
config = {
    "version": int(os.environ["VERSION"]),
    "mode": os.environ["MODE"],
    "checkout_path": os.environ["CHECKOUT"],
    "python": os.environ["PYTHON"],
    "repo_path": os.environ["REPO"],
    "git_protocol": os.environ["PROTOCOL"],
    "server_port": int(os.environ["SERVER_PORT"]),
    "odoo_port": int(os.environ["ODOO_PORT"]),
    "db_user": os.environ["DB_USER"],
    "db_host": None,
    "db_port": None,
    "allowed_origins": ["https://runbot.odoo.com"],
}
with open(os.environ["CONFIG_FILE"], "w") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")
PY
    ok "Config written to $CONFIG_FILE"
}

# ────────────────────────────────────────────────────────────
step_service() {
    step_header 8 "$TOTAL_STEPS" "Systemd Service"

    mkdir -p "$LOG_DIR" "$HOME/.config/systemd/user"
    cp "$SERVER_PY" "$APP_DIR/server.py" || return 1
    cp "$SCRIPT_DIR/update.sh" "$APP_DIR/update.sh" || return 1
    cp "$SCRIPT_DIR/lib.sh" "$APP_DIR/lib.sh" || return 1
    cp "$SCRIPT_DIR/doctor.sh" "$APP_DIR/doctor.sh" || return 1
    chmod +x "$APP_DIR/update.sh" "$APP_DIR/doctor.sh"

    # Installs made before the rename run as "runbot-local" and would keep
    # holding the server port, so retire that unit first.
    if systemctl --user list-unit-files --no-legend 2>/dev/null | grep -q '^runbot-local\.service'; then
        warn "Found a legacy 'runbot-local' service — disabling it."
        systemctl --user disable --now runbot-local >> "$SETUP_LOG" 2>&1 || true
        rm -f "$HOME/.config/systemd/user/runbot-local.service"
        info "The old directory $HOME/.runbot-local can be deleted once this works."
    fi

    install_unit_file "$SERVICE_FILE" || return 1

    systemctl --user daemon-reload || return 1
    systemctl --user enable "$APP_NAME" >> "$SETUP_LOG" 2>&1 || return 1
    systemctl --user restart "$APP_NAME" || return 1

    # Keeps the service alive after logout; harmless if it fails.
    # shellcheck disable=SC2024  # $SETUP_LOG is owned by the invoking user
    sudo loginctl enable-linger "$(current_user)" >> "$SETUP_LOG" 2>&1 \
        || warn "Could not enable lingering — the service will stop when you log out."

    for _ in 1 2 3 4 5; do
        sleep 1
        if curl -fsS --max-time 3 "http://127.0.0.1:$SERVER_PORT/health" >/dev/null 2>&1; then
            ok "Server responding on http://127.0.0.1:$SERVER_PORT"
            return 0
        fi
    done

    err "Server did not come up on port $SERVER_PORT."
    info "Check: systemctl --user status $APP_NAME"
    [ -f "$LOG_DIR/server.log" ] && tail -20 "$LOG_DIR/server.log" | sed 's/^/    /' >&2
    return 1
}

# ────────────────────────────────────────────────────────────
step_desktop() {
    step_header 9 "$TOTAL_STEPS" "Desktop Shortcut"
    mkdir -p "$(dirname "$DESKTOP_DEST")"
    cp "$DESKTOP_FILE" "$DESKTOP_DEST" || return 1
    chmod +x "$DESKTOP_DEST"
    ok "Desktop shortcut installed"
}

# ────────────────────────────────────────────────────────────
step_extension() {
    step_header 10 "$TOTAL_STEPS" "Browser Extension"
    run_logged "Building extension" bash "$SCRIPT_DIR/build-extension.sh" "$APP_DIR/extension" || return 1
    ok "Extension built at $APP_DIR/extension/{chrome,firefox}/"
}

# ────────────────────────────────────────────────────────────
run_step "System packages"        step_system_packages
run_step "PostgreSQL"             step_postgresql
run_step "GitHub access"          step_github_access

# The remaining steps are meaningless without repositories, so stop early
# rather than producing a cascade of confusing failures.
if any_step_failed; then
    print_summary
    err "Setup stopped early — the steps above are prerequisites for the rest."
    info "Fix them and re-run: bash setup.sh"
    exit 1
fi

run_step "Repositories"           step_repositories
if any_step_failed; then
    print_summary
    err "Setup stopped: without both repositories nothing downstream can work."
    exit 1
fi

run_step "Odoo system deps"       step_odoo_deps
run_step "Python virtualenv"      step_venv
run_step "Configuration"          step_config
run_step "Systemd service"        step_service
run_step "Desktop shortcut"       step_desktop
run_step "Browser extension"      step_extension

print_summary

if any_step_failed; then
    err "Setup finished with failures — the tool will not work until they are resolved."
    info "Run 'bash doctor.sh' for a full diagnosis. Details: $SETUP_LOG"
    exit 1
fi

echo -e "${GREEN}Setup complete.${NC}"
echo ""
echo -e "  ${CYAN}Next steps:${NC}"
echo -e "   1. Chrome:  chrome://extensions → Developer mode → Load unpacked →"
echo -e "               $APP_DIR/extension/chrome"
echo -e "      Firefox: about:debugging#/runtime/this-firefox → Load Temporary Add-on →"
echo -e "               $APP_DIR/extension/firefox/manifest.json"
echo -e "   2. Open https://runbot.odoo.com"
echo -e "   3. Click '▶ Run locally' on any bundle page"
echo ""
echo -e "  Diagnose problems any time with: ${BOLD}bash doctor.sh${NC}"
echo -e "  Uninstall with: bash $SCRIPT_DIR/uninstall.sh"
echo ""
