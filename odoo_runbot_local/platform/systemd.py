"""systemd user-service management."""
import os

from . import proc


def user(*args, timeout=60):
    return proc.run(['systemctl', '--user', *args], timeout=timeout)


def is_active(unit):
    return user('is-active', '--quiet', unit, timeout=20).ok


def exists(unit):
    result = user('list-unit-files', '--no-legend', timeout=30)
    if not result.ok:
        return False
    return any(line.split() and line.split()[0].startswith(unit + '.service')
               for line in result.stdout.splitlines())


def daemon_reload():
    return user('daemon-reload')


def enable(unit):
    return user('enable', unit)


def restart(unit):
    return user('restart', unit)


def stop(unit):
    return user('stop', unit)


def disable(unit, now=False):
    args = ['disable', unit] + (['--now'] if now else [])
    return user(*args)


def install_unit(source, destination, replacements):
    """Render a unit file, expanding placeholders ourselves.

    systemd would expand %h itself, but expanding here means the installed file
    is identical whichever command wrote it, and readable without systemd.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(source) as fh:
        content = fh.read()
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    with open(destination, 'w') as fh:
        fh.write(content)
    return True


def lingering(username):
    result = proc.run(['loginctl', 'show-user', username, '-p', 'Linger'], timeout=20)
    return 'Linger=yes' in result.stdout


def enable_lingering(username):
    return proc.run(['sudo', 'loginctl', 'enable-linger', username], timeout=60)
