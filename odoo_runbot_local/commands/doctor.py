"""Read-only diagnosis: answers "why isn't it working" without changing anything."""
import json
import os
import urllib.error
import urllib.request

from .. import config as cfg
from .. import legacy, ui
from ..platform import git, ports, postgres, proc, systemd


class Report:
    def __init__(self):
        self.problems = 0

    def ok(self, message, indent=''):
        print(f'  {indent}{ui.GREEN}✓{ui.NC} {message}')

    def fail(self, message, indent=''):
        print(f'  {indent}{ui.RED}✗{ui.NC} {message}')
        self.problems += 1

    def note(self, message, indent=''):
        print(f'  {indent}{ui.YELLOW}·{ui.NC} {message}')


def _http_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.HTTPError,) as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return None
    except Exception:
        return None


def _check_tools(report):
    ui.heading('Required tools')
    for binary in ('git', 'python3', 'psql', 'createdb', 'dropdb', 'systemctl'):
        if proc.which(binary):
            report.ok(binary)
        else:
            report.fail(f'{binary} not found on PATH')
    if proc.which('ss') or proc.which('lsof'):
        report.ok('port inspection (ss or lsof)')
    else:
        report.note('neither ss nor lsof found — port cleanup uses the recorded pid only')


def _check_postgres(report, conf):
    ui.heading('PostgreSQL')
    if not postgres.ready(conf):
        report.fail('server is not accepting connections')
        unit = postgres.unit_name()
        if unit:
            report.note(f'try: sudo systemctl start {unit}')
        else:
            report.note('no postgresql systemd unit found — is the server package installed?')
        return
    report.ok('server is accepting connections')

    connected, error = postgres.can_connect(conf)
    if connected:
        report.ok(f"role '{conf['db_user']}' can connect")
    else:
        report.fail(f"role '{conf['db_user']}' cannot connect: {error.splitlines()[0] if error else ''}")
        report.note(f"create it: sudo -u postgres createuser -d -R -S {conf['db_user']}")


def _check_config(report, conf, problems):
    ui.heading('Configuration')
    if problems:
        for problem in problems:
            report.fail(problem)
    else:
        report.ok(f'{cfg.CONFIG_FILE} parses, schema version {conf["version"]}')

    report.ok(f'checkout_path = {conf["checkout_path"]}  (odoo + enterprise repositories)')
    for key in ('python', 'db_user', 'server_port', 'odoo_port'):
        report.ok(f'{key} = {conf[key]}')

    if not os.access(conf['python'], os.X_OK):
        report.fail(f'configured python "{conf["python"]}" is not executable — re-run setup')

    # The unit runs the server straight from the repository, so a moved or
    # deleted checkout breaks the service with an obscure import error.
    repo = conf.get('repo_path')
    if not repo:
        report.note('repo_path is not recorded — run setup to set it')
    elif not os.path.exists(os.path.join(repo, 'odoo_runbot_local', 'server.py')):
        report.fail(f'repo_path {repo} no longer contains this tool — the service '
                    f'cannot start.')
        report.note('the folder you cloned was moved or deleted; run "bash setup.sh" '
                    'from wherever it lives now', indent='  ')
    else:
        report.ok(f'repo_path = {repo}  (this tool)')


def _check_repos(report, conf, deep=True):
    ui.heading('Repositories')
    for repo in cfg.REPOS:
        path = cfg.repo_path(conf, repo)
        if not os.path.exists(os.path.join(path, '.git')):
            report.fail(f'{repo} missing at {path}')
            continue
        if not git.is_repo(path):
            report.fail(f'{path} exists but is not a usable git repository')
            report.note(f"remove it and re-run setup: rm -rf '{path}'", indent='  ')
            continue

        kind = 'worktree' if git.is_worktree(path) else 'clone'
        report.ok(f'{repo} at {path} ({kind})')

        names = git.remotes(path)
        if not names:
            report.fail('no git remotes — commits can never be fetched', indent='  ')
            continue
        report.ok(f'remotes: {" ".join(names)}', indent='  ')

        if 'dev' not in names:
            report.fail('no "dev" remote — odoo-dev branches cannot resolve', indent='  ')
            report.note('re-run setup to add it', indent='    ')

        if deep:
            for remote in names:
                url = git.remote_url(path, remote)
                if git.reachable(url):
                    report.ok(f'{remote} reachable ({url})', indent='  ')
                else:
                    report.fail(f'{remote} UNREACHABLE ({url})', indent='  ')


def _check_ports(report, conf):
    ui.heading('Ports')
    server_port = conf['server_port']
    odoo_port = conf['odoo_port']

    if ports.is_free(server_port):
        report.fail(f'nothing listening on {server_port} — the server is not running')
    elif _http_json(f'http://127.0.0.1:{server_port}/health') is not None:
        report.ok(f'{server_port} is served by this server')
    elif _http_json(f'http://127.0.0.1:{server_port}/status') is not None:
        report.fail(f'{server_port} is held by an OLD version of this server — run: '
                    f'{cfg.APP_NAME} update')
    else:
        report.fail(f'{server_port} is held by an unrelated process')

    if ports.is_free(odoo_port):
        report.ok(f'{odoo_port} is free')
    else:
        holders = ports.listeners(odoo_port)
        marker = cfg.odoo_bin(conf)
        ours = [pid for pid in holders if any(marker in arg for arg in ports.cmdline(pid))]
        if ours:
            report.note(f'{odoo_port} is in use by our Odoo instance (pid {ours[0]})')
        elif holders:
            report.note(f'{odoo_port} is in use by another process (pid '
                        f'{", ".join(map(str, holders))}) — set odoo_port to change')
        else:
            report.note(f'{odoo_port} is in use')


def _check_service(report, conf):
    ui.heading('Service')
    if os.path.exists(cfg.UNIT_FILE):
        report.ok(f'unit installed at {cfg.UNIT_FILE}')
        with open(cfg.UNIT_FILE) as fh:
            if '%h' in fh.read():
                report.fail('unit contains an unexpanded %h — re-run setup or update',
                            indent='  ')
    else:
        report.fail(f'unit missing at {cfg.UNIT_FILE} — run: {cfg.APP_NAME} setup')

    # An install from before the rename still runs under the old unit name and
    # would fight this one for the port.
    if systemd.is_active('runbot-local'):
        report.fail("a legacy 'runbot-local' service is still running and holds the port")
        report.note('systemctl --user disable --now runbot-local', indent='  ')

    if systemd.is_active(cfg.APP_NAME):
        report.ok(f'{cfg.APP_NAME} is active')
    else:
        report.fail(f'{cfg.APP_NAME} is not active — start it: '
                    f'systemctl --user restart {cfg.APP_NAME}')
        log = cfg.server_log()
        if os.path.exists(log):
            print()
            report.note(f'last 20 lines of {log}:')
            with open(log) as fh:
                ui.detail(''.join(fh.readlines()[-20:]), indent='      ')

    if systemd.lingering(cfg._default_user()):
        report.ok('lingering enabled (service survives logout)')
    else:
        report.note('lingering disabled — the service stops when you log out')


def _check_leftovers(report):
    stale = legacy.stale_files()
    found = legacy.legacy_dir()
    if not stale and not found:
        return

    ui.heading('Leftovers from earlier versions')
    if stale:
        report.fail(f'{len(stale)} stale shell script(s) in {cfg.APP_DIR}')
        for path in stale:
            report.note(os.path.basename(path), indent='  ')
        report.note(f'remove them: {cfg.APP_NAME} update', indent='  ')
    if found:
        path, size = found
        report.note(f'{path} ({legacy.human_size(size)}) is unused — '
                    f'"{cfg.APP_NAME} update" offers to remove it')


def _check_health(report, conf):
    ui.heading('Server health')
    health = _http_json(f'http://127.0.0.1:{conf["server_port"]}/health')
    if health is None:
        report.fail(f'could not reach GET /health on port {conf["server_port"]}')
        return
    problems = health.get('problems') or []
    if not problems:
        report.ok('server reports no problems')
    for problem in problems:
        report.fail(problem)


def run(args):
    conf, problems = cfg.load()
    report = Report()

    print()
    print(f'{ui.BOLD}{cfg.APP_NAME} — doctor{ui.NC}')

    _check_tools(report)
    _check_postgres(report, conf)
    _check_config(report, conf, problems)
    _check_repos(report, conf, deep=not args.offline)
    _check_ports(report, conf)
    _check_service(report, conf)
    _check_leftovers(report)
    _check_health(report, conf)

    print()
    if report.problems == 0:
        print(f'{ui.GREEN}All checks passed.{ui.NC}')
        print()
        return 0
    print(f'{ui.RED}{report.problems} problem(s) found.{ui.NC} '
          f'Fix the ✗ lines above, then run doctor again.')
    print()
    return 1


def add_parser(subparsers):
    parser = subparsers.add_parser(
        'doctor', help='diagnose an install without changing anything')
    parser.add_argument(
        '--offline', action='store_true',
        help='skip network checks (git ls-remote against each remote)')
    parser.set_defaults(func=run)
    return parser
