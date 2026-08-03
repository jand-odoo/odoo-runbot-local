"""Lifecycle of the local Odoo instance: checkout, database, process.

Kept separate from the HTTP layer so the CLI can inspect and stop an instance
directly, without needing the server to be reachable.
"""
import errno
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time

from . import config as cfg
from .platform import git, ports, postgres, proc

logger = logging.getLogger('odoo-runbot-local')

# Branch names must start alphanumeric so they can never be read as an option
# by createdb/dropdb, and commits are plain hex.
BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$')
COMMIT_RE = re.compile(r'^[a-f0-9]{7,40}$')


def slugify_branch(branch):
    """Turn a branch name into a safe PostgreSQL database name.

    Prefixing (rather than stripping) a leading non-letter keeps purely numeric
    names such as "18.0" and "17.0" distinct, and guarantees the result can
    never be read as a command-line option.
    """
    slug = re.sub(r'[^A-Za-z0-9_]', '_', branch).lower()
    if not slug or not slug[0].isalpha():
        slug = 'b_' + slug
    return slug[:63]


# ─── Running state ───────────────────────────────────────────

def read_running():
    try:
        with open(cfg.RUNNING_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_running(data):
    os.makedirs(os.path.dirname(cfg.RUNNING_FILE), exist_ok=True)
    with open(cfg.RUNNING_FILE, 'w') as fh:
        json.dump(data, fh, indent=2)


def clear_running():
    if os.path.exists(cfg.RUNNING_FILE):
        os.remove(cfg.RUNNING_FILE)


def is_our_process(running):
    """True only if the recorded pid is alive and is the process we started."""
    pid = running.get('pid')
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return False  # exists but owned by someone else — never ours
        return False
    recorded = running.get('start_time')
    if recorded is None:
        return True  # written by an older version; best effort
    return ports.start_time(pid) == recorded


def process_is_ours(conf, pid):
    """True only for an odoo-bin launched from our own checkout."""
    marker = cfg.odoo_bin(conf)
    return any(marker in arg for arg in ports.cmdline(pid))


# ─── Exclusive access ────────────────────────────────────────

class Lock:
    """Advisory lock held for the duration of a checkout.

    flock is released automatically if the process dies, so a crash cannot
    leave a stale lock behind.
    """

    def __init__(self):
        self._fd = None

    def acquire(self):
        os.makedirs(cfg.APP_DIR, exist_ok=True)
        try:
            self._fd = open(cfg.LOCK_FILE, 'w')
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except OSError:
            if self._fd:
                self._fd.close()
                self._fd = None
            return False

    def release(self):
        if not self._fd:
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fd.close()
        self._fd = None


# ─── Progress ────────────────────────────────────────────────

class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self._phase = None
        self._since = None

    def set(self, phase):
        with self._lock:
            self._phase = phase
            self._since = time.time() if phase else None
        if phase:
            logger.info('phase: %s', phase)

    def get(self):
        with self._lock:
            return {'phase': self._phase, 'since': self._since}


progress = Progress()


# ─── Prerequisites ───────────────────────────────────────────

def collect_problems(conf, include_worktree_state=True):
    """Everything that would stop a checkout working, as plain sentences.

    include_worktree_state covers things that depend on which commit happens to
    be checked out right now. A checkout must not be gated on those: it is what
    fixes them. Blocking on a missing odoo-bin would mean one bad commit pair
    leaves the tool permanently unable to check out a good one.
    """
    problems = []

    for binary in ('git', 'psql', 'createdb', 'dropdb'):
        if not proc.which(binary):
            problems.append(f"'{binary}' is not on PATH — install git and the "
                            f"PostgreSQL client.")

    if proc.which('psql'):
        connected, error = postgres.can_connect(conf)
        if not connected:
            problems.append('PostgreSQL is not reachable as user %s: %s'
                            % (conf['db_user'], (error or 'unknown error').splitlines()[0]))

    for repo in cfg.REPOS:
        path = cfg.repo_path(conf, repo)
        if not os.path.exists(os.path.join(path, '.git')):
            problems.append(f'Repository not found at {path} — run setup.')
            continue
        if not git.remotes(path):
            problems.append(f'{path} has no git remotes — commits can never be fetched.')

    if not os.access(conf['python'], os.X_OK):
        problems.append(f'Configured python "{conf["python"]}" is not executable — run setup.')

    if include_worktree_state:
        binary = cfg.odoo_bin(conf)
        if not os.path.exists(binary):
            problems.append(
                f'{binary} not found at the currently checked-out commit. '
                f'Running another branch will replace it.')

    return problems


# ─── Stop ────────────────────────────────────────────────────

def _terminate_group(pid, sig):
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except OSError:
        return False


def stop(conf):
    """Stop the instance we started. Returns a list of problems, empty on success."""
    problems = []
    running = read_running()

    if running and not is_our_process(running):
        logger.info('Recorded pid %s is no longer our process; skipping kill',
                    running.get('pid'))
    elif running:
        pid = running['pid']
        _terminate_group(pid, signal.SIGINT)
        for _ in range(15):
            if not is_our_process(running):
                break
            time.sleep(1)
        else:
            logger.warning('pid %s did not exit on SIGINT; sending SIGKILL', pid)
            _terminate_group(pid, signal.SIGKILL)
            time.sleep(1)

    if running.get('db_name'):
        dropped, error = postgres.drop_database(running['db_name'], conf)
        if dropped:
            logger.info('Dropped database %s', running['db_name'])
        else:
            logger.warning('Could not drop database %s: %s', running['db_name'], error)

    port = conf['odoo_port']
    if not ports.is_free(port):
        # Only ever signal a process we can prove is ours. Developers routinely
        # run their own Odoo on this port, and killing it would be unforgivable.
        foreign = []
        for pid in ports.listeners(port):
            if process_is_ours(conf, pid):
                logger.warning('Port %s held by our orphaned pid %s; terminating it',
                               port, pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            else:
                foreign.append(pid)
                logger.info('Port %s held by pid %s, which is not ours — leaving it alone',
                            port, pid)
        time.sleep(2)
        if not ports.is_free(port):
            owner = f' (held by pid {", ".join(map(str, foreign))})' if foreign else ''
            problems.append(
                f'Port {port} is in use by another process{owner}. '
                f'Stop it, or set "odoo_port" to a free port in {cfg.CONFIG_FILE}.')

    clear_running()
    return problems


# ─── Start ───────────────────────────────────────────────────

def _rotate(path, keep=3):
    """Roll path -> path.1 -> path.2 so logs stay bounded without logrotate."""
    if not os.path.exists(path):
        return
    for index in range(keep - 1, 0, -1):
        older, newer = f'{path}.{index + 1}', f'{path}.{index}'
        if os.path.exists(newer):
            os.replace(newer, older)
    os.replace(path, f'{path}.1')


def _tail(path, lines):
    try:
        with open(path) as fh:
            return ''.join(fh.readlines()[-lines:])
    except OSError:
        return ''


def is_db_initialized(conf, db_name):
    try:
        import psycopg2
    except ImportError:
        return False
    kwargs = {'dbname': db_name, 'user': conf['db_user']}
    if conf.get('db_host'):
        kwargs['host'] = conf['db_host']
    if conf.get('db_port'):
        kwargs['port'] = conf['db_port']
    try:
        with psycopg2.connect(**kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM information_schema.tables "
                            "WHERE table_name = 'ir_module_module'")
                return cur.fetchone() is not None
    except Exception:
        return False


class StartError(Exception):
    def __init__(self, message, detail='', status=500):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


def checkout_and_start(conf, branch, commit_odoo, commit_enterprise):
    """Check out the commit pair and launch Odoo. Raises StartError on failure."""
    slug = slugify_branch(branch)

    for repo, commit in (('odoo', commit_odoo), ('enterprise', commit_enterprise)):
        path = cfg.repo_path(conf, repo)
        progress.set(f'fetching {repo}')
        found, detail = git.fetch_commit(path, commit)
        if not found:
            raise StartError(f'Could not fetch commit {commit[:12]} in {repo}', detail, 502)
        progress.set(f'checking out {repo}')
        done, error = git.checkout_commit(path, commit)
        if not done:
            raise StartError(f'Could not check out {commit[:12]} in {repo}', error, 500)

    # A commit can check out cleanly and still not be a usable Odoo tree — an
    # abandoned fork's branch, or a commit predating odoo-bin. Say so here
    # rather than let Popen fail with a bare "no such file".
    binary = cfg.odoo_bin(conf)
    if not os.path.exists(binary):
        raise StartError(
            f'{binary} does not exist at commit {commit_odoo[:12]}',
            'That commit is probably not from odoo/odoo — check the branch resolved '
            'to the repository you expected.', 500)

    progress.set('stopping previous instance')
    problems = stop(conf)
    if problems:
        raise StartError(problems[0], '', 409)

    progress.set('creating database')
    created, detail = postgres.create_database(slug, conf)
    if not created:
        raise StartError(f'Could not create database {slug}', detail, 500)

    progress.set('starting odoo')
    odoo_port = conf['odoo_port']
    log_path = cfg.odoo_log(conf)
    _rotate(log_path)
    os.makedirs(cfg.LOG_DIR, exist_ok=True)

    cmd = [
        conf['python'], cfg.odoo_bin(conf),
        '-d', slug,
        '--db_user', conf['db_user'],
        '--http-port', str(odoo_port),
        '--http-interface', '127.0.0.1',
        '--addons-path', ','.join(cfg.addons_paths(conf)),
        '--logfile', log_path,
        '--without-demo', 'all',
    ]
    if conf.get('db_host'):
        cmd += ['--db_host', str(conf['db_host'])]
    if conf.get('db_port'):
        cmd += ['--db_port', str(conf['db_port'])]
    if not is_db_initialized(conf, slug):
        cmd += ['-i', 'base']
        logger.info('Database %s is empty; installing base', slug)

    try:
        with open(log_path, 'a') as handle:
            process = subprocess.Popen(
                cmd, stdout=handle, stderr=subprocess.STDOUT,
                # start_new_session is the fork-safe equivalent of preexec_fn,
                # which CPython documents as unsafe in a threaded process.
                start_new_session=True,
            )
    except OSError as exc:
        raise StartError(f'Failed to start Odoo: {exc}') from exc

    # A crash in the first couple of seconds means the log already holds the
    # reason; reporting "started" there would be a lie.
    time.sleep(2)
    if process.poll() is not None:
        raise StartError(f'Odoo exited immediately (code {process.returncode})',
                         _tail(log_path, 15))

    running = {
        'pid': process.pid,
        'start_time': ports.start_time(process.pid),
        'branch': branch,
        'db_name': slug,
        'started_at': time.time(),
        'odoo_commit': commit_odoo,
        'enterprise_commit': commit_enterprise,
        'port': odoo_port,
    }
    write_running(running)
    logger.info('Started odoo pid=%s for %s on port %s', process.pid, branch, odoo_port)

    return {
        # Odoo binds 127.0.0.1 only; "localhost" resolves to ::1 on some hosts.
        'url': f'http://127.0.0.1:{odoo_port}',
        'pid': process.pid,
        'db': slug,
    }


def status(conf):
    """Current state, readable without the server running."""
    state = progress.get()
    running = read_running()
    if not running:
        return {'running': False, 'phase': state['phase']}
    running['running'] = True
    running['alive'] = is_our_process(running)
    running['uptime_seconds'] = int(time.time() - running.get('started_at', time.time()))
    running['phase'] = state['phase']
    return running
