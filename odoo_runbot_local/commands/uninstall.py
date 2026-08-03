"""Remove the service, app data and (optionally) the repositories."""
import os
import shutil

from .. import config as cfg
from .. import instance, ui
from ..platform import git, postgres, systemd


def run(args):
    conf, _ = cfg.load()

    print()
    print(f'{ui.RED}============== {cfg.APP_NAME} — Uninstall =============={ui.NC}')
    print()
    print(f'  {ui.CYAN}Will be removed:{ui.NC}')
    print(f'  {ui.CYAN}*{ui.NC} The systemd user service and its unit file')
    print(f'  {ui.CYAN}*{ui.NC} {cfg.APP_DIR} (venv, logs, config) — you will be asked')
    print(f'  {ui.CYAN}*{ui.NC} {conf["checkout_path"]} (repositories) — you will be asked')
    print(f'  {ui.CYAN}*{ui.NC} The desktop shortcut and CLI symlinks')
    print()
    print(f'  {ui.CYAN}Left untouched:{ui.NC}')
    print(f'  {ui.CYAN}*{ui.NC} PostgreSQL, its packages and your other databases')
    print(f'  {ui.CYAN}*{ui.NC} System packages, SSH keys and GitHub config')
    print(f'  {ui.CYAN}*{ui.NC} The browser extension (unload it manually)')
    print()

    if not ui.confirm('Continue with uninstall?', default=False):
        ui.info('Cancelled.')
        return 0

    # ─── Stop our instance ───────────────────────────────────
    # Only the process we recorded — never whatever holds the Odoo port, which
    # may well be the developer's own Odoo.
    print()
    ui.info('Stopping any running instance...')
    running = instance.read_running()
    if running:
        if instance.is_our_process(running):
            instance.stop(conf)
            ui.ok('Instance stopped')
        else:
            ui.ok('Recorded process is no longer ours — not signalling it')
            if running.get('db_name'):
                postgres.drop_database(running['db_name'], conf)
            instance.clear_running()
    else:
        ui.ok('No instance was running')

    # ─── Service ─────────────────────────────────────────────
    print()
    ui.info('Removing systemd service...')
    systemd.stop(cfg.APP_NAME)
    systemd.disable(cfg.APP_NAME)
    if os.path.exists(cfg.UNIT_FILE):
        os.remove(cfg.UNIT_FILE)
    systemd.daemon_reload()
    ui.ok('Systemd service removed')

    # ─── App data ────────────────────────────────────────────
    print()
    if os.path.isdir(cfg.APP_DIR):
        if ui.confirm(f'Remove {cfg.APP_DIR} (logs, config, venv)?', default=False):
            shutil.rmtree(cfg.APP_DIR, ignore_errors=True)
            ui.ok(f'Removed {cfg.APP_DIR}')
        else:
            ui.warn(f'Kept {cfg.APP_DIR}')

    # ─── Repositories ────────────────────────────────────────
    checkout = conf['checkout_path']
    print()
    if os.path.isdir(checkout):
        ui.warn(f'{checkout} may contain git worktrees of your main repositories.')
        if ui.confirm(f'Remove {checkout}?', default=False):
            for repo in cfg.REPOS:
                path = os.path.join(checkout, repo)
                # Detach worktrees properly so the parent keeps no stale entries.
                if git.is_worktree(path):
                    git.git(path, 'worktree', 'remove', '--force', path, timeout=120)
            shutil.rmtree(checkout, ignore_errors=True)
            ui.ok(f'Removed {checkout}')
            ui.info("Run 'git worktree prune' in your main repos if stale entries remain.")
        else:
            ui.warn(f'Kept {checkout}')

    # ─── Shortcuts ───────────────────────────────────────────
    print()
    if os.path.exists(cfg.DESKTOP_FILE):
        os.remove(cfg.DESKTOP_FILE)
        ui.ok('Desktop shortcut removed')

    bin_dir = os.path.join(cfg.HOME, '.local', 'bin')
    for name in (cfg.APP_NAME, 'orl'):
        link = os.path.join(bin_dir, name)
        if os.path.islink(link):
            os.remove(link)
            ui.ok(f'Removed {link}')

    print()
    print(f'{ui.GREEN}============== Uninstall Complete =============={ui.NC}')
    print()
    print(f'  {ui.YELLOW}Still installed:{ui.NC}')
    print(f'  {ui.YELLOW}*{ui.NC} PostgreSQL and your databases')
    print(f'  {ui.YELLOW}*{ui.NC} System packages, SSH keys and GitHub config')
    print(f'  {ui.YELLOW}*{ui.NC} The browser extension — unload it in chrome://extensions')
    print()
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('uninstall', help='remove the service and app data')
    parser.set_defaults(func=run)
    return parser
