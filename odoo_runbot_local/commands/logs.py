"""Tail the server or Odoo log without having to remember the path."""
import os
import time

from .. import config as cfg
from .. import ui

TARGETS = ('server', 'odoo')


def _path(conf, target):
    return cfg.server_log() if target == 'server' else cfg.odoo_log(conf)


def _tail(path, count):
    """Last `count` lines, spanning rotated files when the current one is short."""
    lines = []
    for candidate in (path, f'{path}.1', f'{path}.2', f'{path}.3'):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, errors='replace') as fh:
                lines = fh.readlines() + lines
        except OSError:
            continue
        if len(lines) >= count:
            break
    return lines[-count:]


def _follow(path):
    """Poll for appended lines. Implemented here so `tail` is not a dependency."""
    handle = None
    inode = None
    try:
        while True:
            if handle is None:
                if not os.path.exists(path):
                    time.sleep(0.5)
                    continue
                handle = open(path, errors='replace')
                inode = os.fstat(handle.fileno()).st_ino
                handle.seek(0, os.SEEK_END)

            line = handle.readline()
            if line:
                print(line, end='', flush=True)
                continue

            # Rotation swaps the file under us; reopen when the inode changes.
            try:
                if os.stat(path).st_ino != inode:
                    handle.close()
                    handle = None
                    continue
            except OSError:
                handle.close()
                handle = None
                continue
            time.sleep(0.3)
    except KeyboardInterrupt:
        print()
    finally:
        if handle:
            handle.close()


def run(args):
    conf, _ = cfg.load()
    path = _path(conf, args.target)

    if not os.path.exists(path) and not args.follow:
        ui.warn(f'No log at {path}')
        if args.target == 'odoo':
            ui.info('Odoo has not been started yet on this port.')
        return 1

    if not args.quiet:
        ui.info(f'{path}')

    for line in _tail(path, args.lines):
        print(line, end='')

    if args.follow:
        _follow(path)
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('logs', help='show the server or Odoo log')
    parser.add_argument('target', nargs='?', default='server', choices=TARGETS,
                        help='which log to read (default: server)')
    parser.add_argument('-f', '--follow', action='store_true',
                        help='keep printing new lines until interrupted')
    parser.add_argument('-n', '--lines', type=int, default=50,
                        help='how many lines of history to show (default: 50)')
    parser.add_argument('-q', '--quiet', action='store_true',
                        help='omit the file-path header')
    parser.set_defaults(func=run)
    return parser
