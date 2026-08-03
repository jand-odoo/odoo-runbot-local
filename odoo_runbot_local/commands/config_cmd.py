"""Read and edit config.json with validation."""
import json

from .. import config as cfg
from .. import ui

# Keys a user should not edit by hand: they are derived by setup and editing
# them here would only produce a broken install.
DERIVED = ('version', 'repo_path')


def _coerce(key, raw):
    """Turn a command-line string into the type the schema expects."""
    default = cfg.defaults()[key]
    if raw.lower() in ('null', 'none', ''):
        if key in cfg.NULLABLE:
            return None
        raise ValueError(f'{key} cannot be null')
    if isinstance(default, bool):
        return raw.lower() in ('1', 'true', 'yes', 'on')
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, list):
        try:
            value = json.loads(raw)
        except ValueError:
            value = [item.strip() for item in raw.split(',') if item.strip()]
        if not isinstance(value, list):
            raise ValueError(f'{key} must be a list')
        return value
    return raw


def run(args):
    conf, problems = cfg.load()

    if args.action in (None, 'show'):
        for problem in problems:
            ui.warn(problem)
        print()
        for key in sorted(conf):
            value = conf[key]
            print(f'  {key:<16} {json.dumps(value)}')
        print()
        ui.info(f'File: {cfg.CONFIG_FILE}')
        return 0

    if args.action == 'get':
        if args.key not in conf:
            ui.err(f'Unknown key: {args.key}')
            ui.info(f'Known keys: {", ".join(sorted(conf))}')
            return 2
        value = conf[args.key]
        print(value if isinstance(value, str) else json.dumps(value))
        return 0

    # set
    if args.key not in cfg.defaults():
        ui.err(f'Unknown key: {args.key}')
        ui.info(f'Known keys: {", ".join(sorted(cfg.defaults()))}')
        return 2
    if args.key in DERIVED:
        ui.err(f'"{args.key}" is managed by setup and should not be edited by hand.')
        return 2

    try:
        value = _coerce(args.key, args.value)
    except ValueError as exc:
        ui.err(f'Invalid value for {args.key}: {exc}')
        return 2

    previous = conf[args.key]
    if previous == value:
        ui.ok(f'{args.key} is already {json.dumps(value)}')
        return 0

    conf[args.key] = value
    cfg.save(conf)
    ui.ok(f'{args.key}: {json.dumps(previous)} → {json.dumps(value)}')

    if args.key in ('server_port', 'odoo_port', 'db_user', 'db_host', 'db_port',
                    'allowed_origins', 'checkout_path', 'python'):
        ui.info(f'Restart to apply: {cfg.APP_NAME} restart')
    if args.key == 'server_port':
        ui.warn('The browser extension has 8765 hardcoded — changing this port '
                'requires editing the extension too.')
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('config', help='show or change configuration')
    actions = parser.add_subparsers(dest='action', metavar='ACTION')

    get_parser = actions.add_parser('get', help='print one value')
    get_parser.add_argument('key')

    set_parser = actions.add_parser('set', help='change one value')
    set_parser.add_argument('key')
    set_parser.add_argument('value')

    actions.add_parser('show', help='print the whole configuration (default)')

    parser.set_defaults(func=run, key=None, value=None)
    return parser
