"""Cleaning up after earlier versions of this tool.

Old installs copied shell scripts into the app directory and, before the rename,
used a different directory entirely. Those leftovers are not merely untidy: the
stale update.sh still tries to copy a repo-root server.py that no longer exists,
so running it fails in a confusing way.
"""
import os
import shutil

from . import config as cfg

# Files only ever placed in the app directory by our own earlier setup, so
# removing them cannot destroy anything the user put there.
STALE_APP_FILES = (
    'server.py',          # the service now runs the package from the checkout
    'setup.sh',
    'update.sh',
    'doctor.sh',
    'lib.sh',
    'killport.sh',
    'build-extension.sh',
    'sign-extension.sh',
)

# The app directory used before the project was renamed.
LEGACY_APP_DIR = os.path.join(cfg.HOME, '.runbot-local')
LEGACY_UNIT = os.path.join(cfg.HOME, '.config', 'systemd', 'user',
                           'runbot-local.service')


def stale_files():
    """Leftover shell scripts still sitting in the app directory."""
    return [os.path.join(cfg.APP_DIR, name) for name in STALE_APP_FILES
            if os.path.exists(os.path.join(cfg.APP_DIR, name))]


def clean_stale_files():
    """Remove them. Returns the paths removed."""
    removed = []
    for path in stale_files():
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def legacy_dir():
    """The pre-rename app directory and its size in bytes, or None."""
    if not os.path.isdir(LEGACY_APP_DIR):
        return None
    total = 0
    for root, _dirs, files in os.walk(LEGACY_APP_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return LEGACY_APP_DIR, total


def human_size(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


def remove_legacy_dir():
    shutil.rmtree(LEGACY_APP_DIR, ignore_errors=True)
    if os.path.exists(LEGACY_UNIT):
        os.remove(LEGACY_UNIT)
    return not os.path.exists(LEGACY_APP_DIR)
