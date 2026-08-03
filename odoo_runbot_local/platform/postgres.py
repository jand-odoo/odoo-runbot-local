"""PostgreSQL readiness, role and database management."""
from . import proc


def _conn_args(config):
    args = []
    if config.get('db_host'):
        args += ['-h', str(config['db_host'])]
    if config.get('db_port'):
        args += ['-p', str(config['db_port'])]
    return args


def ready(config=None):
    """True when the server accepts connections.

    pg_isready is preferred: it distinguishes 'server down' from 'no such role',
    which a bare psql call cannot.
    """
    args = _conn_args(config or {})
    if proc.which('pg_isready'):
        return proc.run(['pg_isready', '-q', *args], timeout=10).ok
    return proc.run(['psql', *args, '-d', 'postgres', '-c', 'SELECT 1'], timeout=10).ok


def can_connect(config, database='postgres'):
    """True when the configured role can actually connect."""
    result = proc.run(
        ['psql', *_conn_args(config), '-d', database, '-c', 'SELECT 1'], timeout=15)
    return result.ok, result.stderr


def unit_name():
    """The systemd unit for postgres, which differs across distributions."""
    result = proc.run(['systemctl', 'list-unit-files', '--no-legend'], timeout=20)
    if not result.ok:
        return None
    candidates = []
    for line in result.stdout.splitlines():
        name = line.split()[0] if line.split() else ''
        if name.startswith('postgresql'):
            candidates.append(name)
    for name in candidates:
        if name in ('postgresql.service', 'postgresql'):
            return name.removesuffix('.service')
    for name in candidates:
        # Versioned units: postgresql@16-main.service, postgresql-16.service
        if name[10:11] in ('@', '-'):
            return name.removesuffix('.service')
    return None


def create_role(name):
    return proc.run(['sudo', '-u', 'postgres', 'createuser', '-d', '-R', '-S', name],
                    timeout=60)


def create_database(name, config=None):
    """Returns (ok, message). 'already exists' counts as success."""
    result = proc.run(['createdb', *_conn_args(config or {}), '--', name], timeout=60)
    if result.ok or 'already exists' in result.stderr:
        return True, ''
    return False, result.stderr or f'createdb exited {result.returncode}'


def drop_database(name, config=None):
    if not name:
        return True, ''
    result = proc.run(
        ['dropdb', '--if-exists', *_conn_args(config or {}), '--', name], timeout=60)
    return result.ok, result.stderr


def initdb_hint(manager):
    return {
        'dnf': 'sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql',
        'pacman': ('sudo -u postgres initdb -D /var/lib/postgres/data && '
                   'sudo systemctl enable --now postgresql'),
        'zypper': 'sudo systemctl enable --now postgresql',
    }.get(manager, 'sudo systemctl start postgresql')
