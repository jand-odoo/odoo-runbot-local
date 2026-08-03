"""Pull the latest code and redeploy.

Much simpler than it used to be: the service runs straight from the repository,
so there is no copy to keep in sync — only dependencies, config and the unit.
"""
import os
import time
import urllib.request

from .. import config as cfg
from .. import legacy, ui
from ..platform import git, proc, systemd
from . import extension


def _health(port, attempts=8):
    url = f'http://127.0.0.1:{port}/health'
    for _ in range(attempts):
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:
            continue
    return False


def run(args):
    repo = cfg.REPO_ROOT
    print()
    ui.info(f'Updating {cfg.APP_NAME} from {repo}')
    print()

    # ─── Pull ────────────────────────────────────────────────
    if not os.path.isdir(os.path.join(repo, '.git')):
        ui.err(f'No git repository at {repo}')
        return 1

    if args.no_pull:
        ui.info('Skipping git pull (--no-pull)')
    else:
        ui.info('Pulling latest code...')
        result = git.git_stream(repo, 'pull', '--ff-only')
        if not result.ok:
            ui.err('git pull failed — resolve it and re-run.')
            return 1
        ui.ok('Repo updated')

    # ─── Dependencies ────────────────────────────────────────
    pip = os.path.join(cfg.VENV_DIR, 'bin', 'pip')
    if not os.path.exists(pip):
        ui.err(f'No virtualenv at {cfg.VENV_DIR} — run: {cfg.APP_NAME} setup')
        return 1

    # Never skipped: new code may need a package the installed venv lacks.
    ui.info('Updating Python dependencies...')
    if not proc.run([pip, 'install', '-q', '-r',
                     os.path.join(repo, 'requirements.txt')], timeout=1800).ok:
        ui.err('Failed to install server requirements.')
        return 1
    ui.ok('Dependencies up to date')

    # ─── Retire earlier versions ─────────────────────────────
    removed = legacy.clean_stale_files()
    if removed:
        ui.ok(f'Removed {len(removed)} leftover file(s) from the old shell install')
        for path in removed:
            ui.detail(os.path.basename(path))

    found = legacy.legacy_dir()
    if found:
        path, size = found
        ui.warn(f'An old install is still on disk: {path} ({legacy.human_size(size)})')
        if ui.confirm('Remove it?', default=False):
            if legacy.remove_legacy_dir():
                ui.ok(f'Removed {path}')
            else:
                ui.warn(f'Could not fully remove {path}')
        else:
            ui.info('Left in place — nothing uses it.')

    # ─── Config migration ────────────────────────────────────
    ui.info('Checking configuration...')
    conf, changed = cfg.migrate()
    conf['repo_path'] = repo   # must track wherever the repo actually lives now
    cfg.save(conf)
    ui.ok(f'Config migrated to version {cfg.CONFIG_VERSION}' if changed
          else f'Config already at version {cfg.CONFIG_VERSION}')

    # ─── Unit ────────────────────────────────────────────────
    ui.info('Updating systemd unit...')
    systemd.install_unit(
        os.path.join(repo, f'{cfg.APP_NAME}.service'),
        cfg.UNIT_FILE,
        {'%REPO%': repo, '%h': cfg.HOME},
    )
    systemd.daemon_reload()
    ui.ok('Unit updated')

    # ─── Restart ─────────────────────────────────────────────
    ui.info(f'Restarting {cfg.APP_NAME}...')
    if not systemd.restart(cfg.APP_NAME).ok:
        ui.err(f'Restart failed — check: systemctl --user status {cfg.APP_NAME}')
        return 1

    if not _health(conf['server_port']):
        ui.err(f'Server did not report healthy on port {conf["server_port"]}.')
        ui.info(f'Run "{cfg.APP_NAME} doctor" for details.')
        log = cfg.server_log()
        if os.path.exists(log):
            with open(log) as fh:
                ui.detail(''.join(fh.readlines()[-20:]))
        return 1
    ui.ok('Server restarted and healthy')

    # ─── Extension ───────────────────────────────────────────
    ui.info('Rebuilding extension...')
    try:
        extension.build(cfg.EXTENSION_DIR)
    except (FileNotFoundError, OSError) as exc:
        ui.err(f'Extension build failed: {exc}')
        return 1
    ui.ok('Extension rebuilt')

    print()
    print(f'{ui.GREEN}============== Update Complete =============={ui.NC}')
    print()
    print(f'  {ui.GREEN}✓{ui.NC} Code pulled and dependencies updated')
    print(f'  {ui.GREEN}✓{ui.NC} Server restarted and healthy')
    print(f'  {ui.GREEN}✓{ui.NC} Extension rebuilt')
    print()
    print(f'  {ui.YELLOW}⚠{ui.NC} Reload the extension in your browser:')
    print('     Chrome:  chrome://extensions → Reload')
    print('     Firefox: about:debugging#/runtime/this-firefox → Reload')
    print()
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('update', help='pull the latest code and redeploy')
    parser.add_argument('--no-pull', action='store_true',
                        help='redeploy without pulling (useful with local changes)')
    parser.set_defaults(func=run)
    return parser
