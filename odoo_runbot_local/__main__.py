"""CLI entry point.

Stdlib only, deliberately: this must run before setup has created the
virtualenv. Third-party packages (flask, psycopg2) belong to the server alone.
"""
import argparse
import sys

from . import __version__, ui
from .config import APP_NAME


def build_parser():
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description='Run any Odoo branch locally, from runbot or by branch name.',
    )
    parser.add_argument('--version', action='version',
                        version=f'{APP_NAME} {__version__}')
    parser.add_argument('-y', '--yes', action='store_true',
                        help='non-interactive: take the default for every prompt')

    subparsers = parser.add_subparsers(dest='command', metavar='COMMAND')

    # Ordered roughly by how often they are reached for.
    from .commands import config_cmd, doctor, extension, logs
    from .commands import run as run_command
    from .commands import service, setup, uninstall, update

    for module in (run_command, service, logs, config_cmd,
                   setup, update, doctor, uninstall, extension):
        module.add_parser(subparsers)

    return parser


def split_passthrough(argv):
    """Split argv at the first bare `--`, returning (ours, theirs).

    Done before argparse sees it. argparse.REMAINDER looks like the obvious
    tool but swallows any option that follows the positional, so
    `run BRANCH --dry-run` would silently start Odoo instead of resolving.
    """
    if '--' not in argv:
        return argv, []
    index = argv.index('--')
    return argv[:index], argv[index + 1:]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, passthrough = split_passthrough(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    args.odoo_args = passthrough

    if not getattr(args, 'func', None):
        parser.print_help()
        return 0

    ui.ASSUME_YES = args.yes

    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print()
        ui.warn('Interrupted.')
        return 130


if __name__ == '__main__':
    sys.exit(main())
