"""Run a branch locally by name, without needing runbot."""
import threading
import time

from .. import client
from .. import config as cfg
from .. import resolve, ui


def _pick_branch(conf, fragment):
    """Offer matching branches when the exact name was not found."""
    ui.info(f'Searching remotes for branches matching "{fragment}"...')
    matches = resolve.search(conf, fragment)
    if not matches:
        return None

    labels = [f'{name}  {ui.DIM}({", ".join(sorted(repos))}){ui.NC}'
              for name, repos in matches]
    chosen = ui.choose('Matching branches:', labels)
    if chosen is None:
        return None
    return matches[labels.index(chosen)][0]


def _describe(resolution):
    print()
    ui.info(f'Branch: {resolution.branch}')
    for repo in cfg.REPOS:
        ui.info(f'  {repo:<11} {resolution.commits[repo][:12]}')
    for note in resolution.notes:
        ui.detail(note, indent='  ')
    if not resolution.exact:
        # Be explicit that this is inference, not runbot's recorded pairing.
        ui.detail('This pairing is inferred. Runbot records what was actually '
                  'built together; this is a best-effort reconstruction.', indent='  ')


def _follow_progress(conf, stop_event):
    """Print server-side phases while the (possibly long) checkout runs."""
    last = None
    while not stop_event.is_set():
        try:
            state = client.get(conf, '/status', timeout=5)
        except Exception:
            state = {}
        phase = state.get('phase')
        if phase and phase != last:
            ui.info(f'  {phase}...')
            last = phase
        stop_event.wait(2)


def run(args):
    conf, problems = cfg.load()

    branch = args.branch
    if not branch:
        if not ui.interactive():
            ui.err('No branch given. Pass one as an argument.')
            return 2
        branch = ui.ask('Branch name').strip()
    if not branch:
        ui.err('No branch given.')
        return 2

    # Resolve before touching the server: a bad branch should cost nothing.
    try:
        resolution = resolve.resolve(conf, branch, args.odoo_commit,
                                     args.enterprise_commit)
    except resolve.ResolutionError as exc:
        ui.err(exc.message)
        for line in exc.searched:
            ui.detail(f'searched {line}')

        picked = _pick_branch(conf, branch) if ui.interactive() else None
        if not picked:
            return 1
        try:
            resolution = resolve.resolve(conf, picked, args.odoo_commit,
                                         args.enterprise_commit)
        except resolve.ResolutionError as exc2:
            ui.err(exc2.message)
            return 1

    _describe(resolution)

    if args.dry_run:
        print()
        ui.ok('Dry run — nothing started.')
        return 0

    if not resolution.exact and not ui.confirm('\nStart Odoo with this pair?'):
        ui.info('Cancelled.')
        return 0

    print()
    ui.info('Starting — this can take several minutes on a cold fetch.')
    stop_event = threading.Event()
    watcher = threading.Thread(target=_follow_progress, args=(conf, stop_event),
                               daemon=True)
    watcher.start()

    started = time.time()
    try:
        result = client.post(conf, '/checkout', {
            'branch': resolution.branch,
            'commit_odoo': resolution.commits['odoo'],
            'commit_enterprise': resolution.commits['enterprise'],
        })
    except client.ServerUnreachable as exc:
        stop_event.set()
        ui.err(str(exc))
        return 1
    except client.ServerError as exc:
        stop_event.set()
        ui.err(exc.message)
        if exc.detail:
            ui.detail(exc.detail)
        return 1
    finally:
        stop_event.set()
        watcher.join(timeout=3)

    elapsed = int(time.time() - started)
    print()
    ui.ok(f'Odoo is running at {result["url"]}  (database: {result["db"]}, {elapsed}s)')
    ui.info(f'Stop it with: {cfg.APP_NAME} stop')
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser(
        'run', help='run a branch locally by name (no runbot needed)')
    parser.add_argument('branch', nargs='?',
                        help='branch name, e.g. master-l10n_cn-tax_report-jand')
    parser.add_argument('--odoo-commit', help='override the odoo commit')
    parser.add_argument('--enterprise-commit', help='override the enterprise commit')
    parser.add_argument('--dry-run', action='store_true',
                        help='resolve the commit pair and stop')
    parser.set_defaults(func=run)
    return parser
