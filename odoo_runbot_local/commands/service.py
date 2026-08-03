"""status / start / stop / restart."""
import time

from .. import client
from .. import config as cfg
from .. import instance, ui
from ..platform import systemd


def _format_uptime(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m {seconds}s'
    return f'{seconds}s'


def status(args):
    conf, _ = cfg.load()

    # Read the instance state directly rather than over HTTP: status must still
    # work when the server is down, which is exactly when it is asked for.
    state = instance.status(conf)
    active = systemd.is_active(cfg.APP_NAME)
    reachable = client.reachable(conf)

    ui.heading('Server')
    print(f"  service   {'active' if active else 'inactive'}")
    print(f"  responds  {'yes' if reachable else 'no'}  "
          f"(http://127.0.0.1:{conf['server_port']})")

    ui.heading('Odoo instance')
    if not state.get('running'):
        print('  nothing running')
        if state.get('phase'):
            print(f"  phase     {state['phase']}")
        print()
        return 0

    alive = state.get('alive')
    print(f"  branch    {state.get('branch')}")
    print(f"  database  {state.get('db_name')}")
    print(f"  url       http://127.0.0.1:{state.get('port', conf['odoo_port'])}")
    print(f"  pid       {state.get('pid')}  {'(alive)' if alive else '(DEAD)'}")
    print(f"  uptime    {_format_uptime(state.get('uptime_seconds', 0))}")
    if state.get('odoo_commit'):
        print(f"  commits   odoo {state['odoo_commit'][:12]}  "
              f"enterprise {state.get('enterprise_commit', '')[:12]}")
    if not alive:
        ui.warn('The recorded process is gone — run "stop" to clean up.')
    print()
    return 0


def start(args):
    conf, _ = cfg.load()
    if systemd.is_active(cfg.APP_NAME):
        ui.ok('Server is already running')
        return 0
    if not systemd.restart(cfg.APP_NAME).ok:
        ui.err(f'Could not start the service — check: systemctl --user status '
               f'{cfg.APP_NAME}')
        return 1
    for _ in range(8):
        time.sleep(1)
        if client.reachable(conf):
            ui.ok(f'Server running on http://127.0.0.1:{conf["server_port"]}')
            return 0
    ui.err('Service started but is not responding.')
    ui.info(f'Run "{cfg.APP_NAME} doctor" for details.')
    return 1


def stop(args):
    """Stops the Odoo instance, not the background server."""
    conf, _ = cfg.load()
    state = instance.status(conf)
    if not state.get('running'):
        ui.ok('No Odoo instance is running')
        return 0

    # Prefer the server so it stays the single owner of the lifecycle; fall
    # back to stopping directly when it is down.
    try:
        client.post(conf, '/stop', timeout=180)
        ui.ok(f'Stopped {state.get("branch")} and dropped {state.get("db_name")}')
        return 0
    except client.ServerUnreachable:
        ui.warn('Server unreachable — stopping the instance directly.')
    except client.ServerError as exc:
        ui.err(exc.message)
        return 1

    problems = instance.stop(conf)
    if problems:
        for problem in problems:
            ui.err(problem)
        return 1
    ui.ok('Instance stopped')
    return 0


def restart(args):
    """Restarts the background server, leaving a healthy instance alone."""
    conf, _ = cfg.load()
    if not systemd.restart(cfg.APP_NAME).ok:
        ui.err(f'Restart failed — check: systemctl --user status {cfg.APP_NAME}')
        return 1
    for _ in range(8):
        time.sleep(1)
        if client.reachable(conf):
            ui.ok('Server restarted')
            return 0
    ui.err('Server did not come back.')
    ui.info(f'Run "{cfg.APP_NAME} doctor" for details.')
    return 1


def add_parser(subparsers):
    parsers = []

    parser = subparsers.add_parser('status', help='show the server and Odoo instance')
    parser.set_defaults(func=status)
    parsers.append(parser)

    parser = subparsers.add_parser('start', help='start the background server')
    parser.set_defaults(func=start)
    parsers.append(parser)

    parser = subparsers.add_parser('stop', help='stop the running Odoo instance')
    parser.set_defaults(func=stop)
    parsers.append(parser)

    parser = subparsers.add_parser('restart', help='restart the background server')
    parser.set_defaults(func=restart)
    parsers.append(parser)

    return parsers
