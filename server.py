#!/usr/bin/env python3
"""Local HTTP server that checks out runbot commits and runs Odoo against them."""
import errno
import fcntl
import getpass
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import psycopg2
from flask import Flask, jsonify, request

HOME = os.path.expanduser('~')
APP_DIR = os.path.join(HOME, '.odoo-runbot-local')
LOG_DIR = os.path.join(APP_DIR, 'logs')
RUNNING_FILE = os.path.join(APP_DIR, 'running.json')
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
LOCK_FILE = os.path.join(APP_DIR, 'checkout.lock')

CONFIG_VERSION = 2
DEFAULT_CHECKOUT = os.path.join(
    os.environ.get('XDG_DATA_HOME') or os.path.join(HOME, '.local', 'share'),
    'odoo-runbot-local', 'checkout',
)

DEFAULTS = {
    'version': CONFIG_VERSION,
    'mode': 'clone',
    'checkout_path': DEFAULT_CHECKOUT,
    'python': sys.executable,
    'repo_path': None,
    'git_protocol': 'ssh',
    'server_port': 8765,
    'odoo_port': 8072,
    'db_user': getpass.getuser(),
    'db_host': None,
    'db_port': None,
    'allowed_origins': ['https://runbot.odoo.com'],
}

# Branch names must start alphanumeric so they can never be read as an option
# by createdb/dropdb, and commits are plain hex.
BRANCH_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$')
COMMIT_RE = re.compile(r'^[a-f0-9]{7,40}$')
# Extension origins are set by the browser and cannot be forged by page JS.
EXTENSION_ORIGIN_RE = re.compile(r'^(chrome-extension|moz-extension|safari-web-extension)://[A-Za-z0-9._-]+$')
# Requiring this header means a drive-by request must be preflighted, and the
# preflight is refused for any origin outside the allowlist.
GUARD_HEADER = 'X-Runbot-Local'

logger = logging.getLogger('odoo-runbot-local')

app = Flask(__name__)

# Progress of an in-flight checkout, surfaced through /status so the extension
# can show what the (potentially multi-minute) request is doing.
_progress = {'phase': None, 'since': None}
_progress_lock = threading.Lock()

CONFIG_PROBLEMS = []


def load_config():
    """Merge the config file over defaults.

    A missing key must not raise, and malformed JSON must not crash the process:
    the unit would then crash-loop forever and take the diagnostics down with it.
    """
    config = dict(DEFAULTS)
    if not os.path.exists(CONFIG_FILE):
        CONFIG_PROBLEMS.append(
            f'{CONFIG_FILE} does not exist — run setup.sh. Using built-in defaults.')
        return config
    try:
        with open(CONFIG_FILE) as fh:
            loaded = json.load(fh)
    except (ValueError, OSError) as exc:
        CONFIG_PROBLEMS.append(
            f'{CONFIG_FILE} could not be read ({exc}) — using built-in defaults.')
        return config
    if not isinstance(loaded, dict):
        CONFIG_PROBLEMS.append(
            f'{CONFIG_FILE} is not a JSON object — using built-in defaults.')
        return config

    missing = [k for k in DEFAULTS if k not in loaded]
    if missing:
        CONFIG_PROBLEMS.append(
            'Config is missing key(s): %s — defaults used. Run update.sh to migrate.'
            % ', '.join(sorted(missing)))
    if loaded.get('version') != CONFIG_VERSION:
        CONFIG_PROBLEMS.append(
            'Config schema is version %s, expected %s — run update.sh.'
            % (loaded.get('version'), CONFIG_VERSION))

    config.update({k: v for k, v in loaded.items() if v is not None or k in ('db_host', 'db_port')})

    # A hand-edited config must not be able to break request handling.
    if not isinstance(config['allowed_origins'], list):
        CONFIG_PROBLEMS.append('"allowed_origins" must be a list — using the default.')
        config['allowed_origins'] = list(DEFAULTS['allowed_origins'])
    for key in ('server_port', 'odoo_port'):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError):
            CONFIG_PROBLEMS.append(f'"{key}" is not a number — using {DEFAULTS[key]}.')
            config[key] = DEFAULTS[key]

    return config


CONF = load_config()


# ─── Origin / method guards ──────────────────────────────────

def origin_allowed(origin):
    if not origin:
        return False
    if origin in CONF['allowed_origins']:
        return True
    return bool(EXTENSION_ORIGIN_RE.match(origin))


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    if origin_allowed(origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = f'Content-Type, {GUARD_HEADER}'
        response.headers['Access-Control-Max-Age'] = '600'
    response.headers['Vary'] = 'Origin'
    return response


def reject_untrusted_caller():
    """Return an error response when a mutating call is not from our extension.

    An Origin header is present on every cross-origin request a web page can
    make; its absence means the call did not come from page JavaScript (the
    extension's own background fetch, or curl). When present it must be trusted.
    """
    origin = request.headers.get('Origin')
    if origin and not origin_allowed(origin):
        logger.warning('Rejected %s from disallowed origin %s', request.path, origin)
        return jsonify({'error': 'Origin not allowed'}), 403
    if request.headers.get(GUARD_HEADER) != '1':
        logger.warning('Rejected %s without the %s header', request.path, GUARD_HEADER)
        return jsonify({'error': f'Missing {GUARD_HEADER} header'}), 403
    return None


# ─── Small helpers ───────────────────────────────────────────

def set_phase(phase):
    with _progress_lock:
        _progress['phase'] = phase
        _progress['since'] = time.time() if phase else None
    if phase:
        logger.info('phase: %s', phase)


def get_phase():
    with _progress_lock:
        return dict(_progress)


def slugify_branch(branch):
    """Turn a bundle name into a safe PostgreSQL database name.

    Prefixing (rather than stripping) a leading non-letter keeps purely numeric
    bundle names such as "18.0" and "17.0" distinct, and guarantees the result
    can never be read as a command-line option.
    """
    slug = re.sub(r'[^A-Za-z0-9_]', '_', branch).lower()
    if not slug or not slug[0].isalpha():
        slug = 'b_' + slug
    return slug[:63]


def read_running():
    try:
        with open(RUNNING_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_running(data):
    os.makedirs(os.path.dirname(RUNNING_FILE), exist_ok=True)
    with open(RUNNING_FILE, 'w') as fh:
        json.dump(data, fh, indent=2)


def process_start_time(pid):
    """Kernel start time of a pid, used to detect pid reuse across reboots."""
    try:
        with open(f'/proc/{pid}/stat') as fh:
            data = fh.read()
        # comm may contain spaces and parentheses, so parse after the last ')'.
        return int(data[data.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def is_our_process(running):
    """True only if the recorded pid is alive *and* is the process we started."""
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
    return process_start_time(pid) == recorded


_lock_fd = None


def acquire_lock():
    global _lock_fd
    os.makedirs(APP_DIR, exist_ok=True)
    try:
        _lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        return True
    except OSError:
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False


def release_lock():
    global _lock_fd
    if _lock_fd:
        try:
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        _lock_fd.close()
        _lock_fd = None


# ─── Process management ──────────────────────────────────────

def pids_on_port(port):
    """Best effort: ss, then lsof. Neither is a hard dependency."""
    if shutil.which('ss'):
        try:
            result = subprocess.run(
                ['ss', '-ltnpH', f'sport = :{port}'],
                capture_output=True, text=True, timeout=10,
            )
            return {int(m) for m in re.findall(r'pid=(\d+)', result.stdout)}
        except (OSError, subprocess.SubprocessError):
            pass
    if shutil.which('lsof'):
        try:
            result = subprocess.run(
                ['lsof', '-tiTCP:%d' % port, '-sTCP:LISTEN'],
                capture_output=True, text=True, timeout=10,
            )
            return {int(p) for p in result.stdout.split()}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return set()


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def process_cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as fh:
            return fh.read().decode('utf-8', 'replace').split('\0')
    except OSError:
        return []


def process_is_ours(pid):
    """True only for an odoo-bin launched from our own checkout."""
    marker = os.path.join(CONF['checkout_path'], 'odoo', 'odoo-bin')
    return any(marker in arg for arg in process_cmdline(pid))


def terminate_group(pid, sig):
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except OSError:
        return False


def drop_database(db_name):
    if not db_name:
        return
    result = subprocess.run(
        ['dropdb', '--if-exists', '--', db_name],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 0:
        logger.info('Dropped database %s', db_name)
    else:
        logger.warning('Could not drop database %s: %s', db_name, result.stderr.strip())


def stop_running_instance():
    """Stop the instance we started. Returns a list of problems, empty on success."""
    problems = []
    running = read_running()

    if running and not is_our_process(running):
        # The pid died, or was reused by an unrelated process — never signal it.
        logger.info('Recorded pid %s is no longer our process; skipping kill',
                    running.get('pid'))
    elif running:
        pid = running['pid']
        terminate_group(pid, signal.SIGINT)
        for _ in range(15):
            if not is_our_process(running):
                break
            time.sleep(1)
        else:
            logger.warning('pid %s did not exit on SIGINT; sending SIGKILL', pid)
            terminate_group(pid, signal.SIGKILL)
            time.sleep(1)

    if running.get('db_name'):
        drop_database(running['db_name'])

    port = CONF['odoo_port']
    if not port_is_free(port):
        # Only ever signal a process we can prove is ours. Developers routinely
        # run their own Odoo on this port, and killing it would be unforgivable.
        foreign = []
        for pid in pids_on_port(port):
            if process_is_ours(pid):
                logger.warning('Port %s held by our orphaned pid %s; terminating it', port, pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            else:
                foreign.append(pid)
                logger.info('Port %s held by pid %s, which is not ours — leaving it alone',
                            port, pid)
        time.sleep(2)
        if not port_is_free(port):
            owner = f' (held by pid {", ".join(map(str, foreign))})' if foreign else ''
            problems.append(
                f'Port {port} is in use by another process{owner}. '
                f'Stop it, or set "odoo_port" to a free port in {CONFIG_FILE}.')

    if os.path.exists(RUNNING_FILE):
        os.remove(RUNNING_FILE)
    return problems


# ─── Diagnostics ─────────────────────────────────────────────

def collect_problems():
    """Everything that would stop a checkout from working, as plain sentences."""
    problems = list(CONFIG_PROBLEMS)

    for binary in ('git', 'psql', 'createdb', 'dropdb'):
        if not shutil.which(binary):
            problems.append(f"'{binary}' is not on PATH — install the PostgreSQL client / git.")

    result = None
    if shutil.which('psql'):
        try:
            result = subprocess.run(
                ['psql', '-d', 'postgres', '-c', 'SELECT 1'],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f'Could not run psql: {exc}')
    if result is not None and result.returncode != 0:
        problems.append(
            'PostgreSQL is not reachable as user %s: %s'
            % (CONF['db_user'], result.stderr.strip() or 'unknown error'))

    checkout = CONF['checkout_path']
    for repo in ('odoo', 'enterprise'):
        path = os.path.join(checkout, repo)
        if not os.path.exists(os.path.join(path, '.git')):
            problems.append(f'Repository not found at {path} — re-run setup.sh.')
            continue
        ok, out, _ = run_git(path, 'remote')
        if not ok or not out.strip():
            problems.append(f'{path} has no git remotes — commits can never be fetched.')

    python_bin = CONF['python']
    if not os.access(python_bin, os.X_OK):
        problems.append(f'Configured python "{python_bin}" is not executable — re-run setup.sh.')

    odoo_bin = os.path.join(checkout, 'odoo', 'odoo-bin')
    if not os.path.exists(odoo_bin):
        problems.append(f'{odoo_bin} not found — the odoo repository looks incomplete.')

    return problems


# ─── Git ─────────────────────────────────────────────────────

def git_env():
    env = dict(os.environ)
    # Never block on a credential prompt: fail fast and report it instead.
    env['GIT_TERMINAL_PROMPT'] = '0'
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new')
    return env


def run_git(repo_path, *args, timeout=600):
    try:
        result = subprocess.run(
            ['git', *args], cwd=repo_path, env=git_env(),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        message = f'git {" ".join(args)} timed out after {timeout}s in {repo_path}'
        logger.error(message)
        return False, '', message
    except OSError as exc:
        logger.error('git error in %s: %s', repo_path, exc)
        return False, '', str(exc)
    if result.returncode != 0:
        logger.error('git %s in %s: %s', ' '.join(args), repo_path, result.stderr.strip())
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def get_remotes(repo_path):
    ok, out, _ = run_git(repo_path, 'remote', timeout=30)
    if ok and out:
        remotes = [r.strip() for r in out.split('\n') if r.strip()]
        # origin first, then dev (runbot dev-branch bundles live in odoo-dev).
        remotes.sort(key=lambda r: (r != 'origin', r != 'dev', r))
        return remotes
    return ['origin']


def has_commit(repo_path, commit):
    ok, _, _ = run_git(repo_path, 'cat-file', '-e', f'{commit}^{{commit}}', timeout=30)
    return ok


def ensure_commit(repo_path, commit):
    """Make `commit` available locally. Returns (ok, message)."""
    if has_commit(repo_path, commit):
        return True, ''

    errors = []
    for remote in get_remotes(repo_path):
        # Fetching a bare sha needs no refspec: the objects stay reachable via
        # FETCH_HEAD, so nothing is written into the shared ref namespace.
        ok, _, stderr = run_git(repo_path, 'fetch', '--no-tags', remote, commit)
        if ok and has_commit(repo_path, commit):
            return True, ''
        if stderr:
            errors.append(f'{remote}: {stderr.splitlines()[-1]}')

        ok, _, stderr = run_git(repo_path, 'fetch', '--no-tags', remote)
        if ok and has_commit(repo_path, commit):
            return True, ''
        if stderr:
            errors.append(f'{remote}: {stderr.splitlines()[-1]}')

    ok, _, _ = run_git(repo_path, 'rev-parse', '--is-shallow-repository', timeout=30)
    if ok:
        run_git(repo_path, 'fetch', '--unshallow')
        if has_commit(repo_path, commit):
            return True, ''

    detail = '; '.join(errors[:3]) if errors else 'commit not found on any remote'
    return False, detail


# ─── Database ────────────────────────────────────────────────

def db_connect_kwargs(db_name):
    kwargs = {'dbname': db_name, 'user': CONF['db_user']}
    if CONF.get('db_host'):
        kwargs['host'] = CONF['db_host']
    if CONF.get('db_port'):
        kwargs['port'] = CONF['db_port']
    return kwargs


def is_db_initialized(db_name):
    try:
        with psycopg2.connect(**db_connect_kwargs(db_name)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'ir_module_module'"
                )
                return cur.fetchone() is not None
    except psycopg2.Error:
        return False


def create_database(db_name):
    """Returns (ok, message). A non-zero exit must not be mistaken for success."""
    cmd = ['createdb']
    if CONF.get('db_host'):
        cmd += ['-h', str(CONF['db_host'])]
    if CONF.get('db_port'):
        cmd += ['-p', str(CONF['db_port'])]
    cmd += ['--', db_name]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ''
    stderr = result.stderr.strip()
    if 'already exists' in stderr:
        return True, ''
    return False, stderr or f'createdb exited {result.returncode}'


# ─── Endpoints ───────────────────────────────────────────────

@app.route('/checkout', methods=['POST', 'OPTIONS'])
def checkout():
    if request.method == 'OPTIONS':
        return ('', 204) if origin_allowed(request.headers.get('Origin')) else ('', 403)
    rejection = reject_untrusted_caller()
    if rejection:
        return rejection

    payload = request.get_json(silent=True) or {}
    branch = payload.get('branch') or request.args.get('branch', '')
    commit_odoo = payload.get('commit_odoo') or request.args.get('commit_odoo', '')
    commit_enterprise = payload.get('commit_enterprise') or request.args.get('commit_enterprise', '')

    if not BRANCH_RE.match(branch or ''):
        return jsonify({'error': f'Invalid branch name: {branch!r}'}), 400
    if not COMMIT_RE.match(commit_odoo or ''):
        return jsonify({'error': 'Invalid odoo commit'}), 400
    if not COMMIT_RE.match(commit_enterprise or ''):
        return jsonify({'error': 'Invalid enterprise commit'}), 400

    problems = collect_problems()
    if problems:
        return jsonify({'error': problems[0], 'problems': problems}), 503

    if not acquire_lock():
        return jsonify({'error': 'A checkout is already in progress'}), 429

    try:
        return _do_checkout(branch, commit_odoo, commit_enterprise)
    finally:
        set_phase(None)
        release_lock()


def _do_checkout(branch, commit_odoo, commit_enterprise):
    slug = slugify_branch(branch)
    checkout_path = CONF['checkout_path']

    for repo, commit in (('odoo', commit_odoo), ('enterprise', commit_enterprise)):
        repo_path = os.path.join(checkout_path, repo)
        set_phase(f'fetching {repo}')
        found, detail = ensure_commit(repo_path, commit)
        if not found:
            return jsonify({
                'error': f'Could not fetch commit {commit[:12]} in {repo}',
                'detail': detail,
            }), 502
        set_phase(f'checking out {repo}')
        ok, _, stderr = run_git(repo_path, 'checkout', '--force', '--detach', commit)
        if not ok:
            return jsonify({
                'error': f'Could not check out {commit[:12]} in {repo}',
                'detail': stderr,
            }), 500

    set_phase('stopping previous instance')
    stop_problems = stop_running_instance()
    if stop_problems:
        return jsonify({'error': stop_problems[0], 'problems': stop_problems}), 409

    set_phase('creating database')
    created, detail = create_database(slug)
    if not created:
        return jsonify({'error': f'Could not create database {slug}', 'detail': detail}), 500

    set_phase('starting odoo')
    odoo_port = CONF['odoo_port']
    odoo_log = os.path.join(LOG_DIR, f'odoo-{odoo_port}.log')
    rotate_file(odoo_log)

    cmd = [
        CONF['python'],
        os.path.join(checkout_path, 'odoo', 'odoo-bin'),
        '-d', slug,
        '--db_user', CONF['db_user'],
        '--http-port', str(odoo_port),
        '--http-interface', '127.0.0.1',
        '--addons-path', ','.join([
            os.path.join(checkout_path, 'odoo', 'addons'),
            os.path.join(checkout_path, 'odoo', 'odoo', 'addons'),
            os.path.join(checkout_path, 'enterprise'),
        ]),
        '--logfile', odoo_log,
        '--without-demo', 'all',
    ]
    if CONF.get('db_host'):
        cmd += ['--db_host', str(CONF['db_host'])]
    if CONF.get('db_port'):
        cmd += ['--db_port', str(CONF['db_port'])]
    if not is_db_initialized(slug):
        cmd += ['-i', 'base']
        logger.info('Database %s is empty; installing base', slug)

    try:
        with open(odoo_log, 'a') as log_handle:
            proc = subprocess.Popen(
                cmd, stdout=log_handle, stderr=subprocess.STDOUT,
                # start_new_session is the fork-safe equivalent of preexec_fn,
                # which CPython documents as unsafe in a threaded process.
                start_new_session=True,
            )
    except OSError as exc:
        logger.error('Failed to start odoo: %s', exc)
        return jsonify({'error': f'Failed to start Odoo: {exc}'}), 500

    # A crash within the first couple of seconds means the log already has the
    # reason; reporting "started" there would be a lie.
    time.sleep(2)
    if proc.poll() is not None:
        return jsonify({
            'error': f'Odoo exited immediately (code {proc.returncode})',
            'detail': tail_file(odoo_log, 15),
        }), 500

    running = {
        'pid': proc.pid,
        'start_time': process_start_time(proc.pid),
        'branch': branch,
        'db_name': slug,
        'started_at': time.time(),
        'odoo_commit': commit_odoo,
        'enterprise_commit': commit_enterprise,
        'port': odoo_port,
    }
    write_running(running)
    logger.info('Started odoo pid=%s for %s on port %s', proc.pid, branch, odoo_port)

    return jsonify({
        # Odoo binds 127.0.0.1 only; "localhost" resolves to ::1 on some hosts.
        'url': f'http://127.0.0.1:{odoo_port}',
        'pid': proc.pid,
        'db': slug,
    })


@app.route('/status', methods=['GET'])
def status():
    progress = get_phase()
    running = read_running()
    if not running:
        return jsonify({'running': False, 'phase': progress['phase']})
    running['running'] = True
    running['alive'] = is_our_process(running)
    running['uptime_seconds'] = int(time.time() - running.get('started_at', time.time()))
    running['phase'] = progress['phase']
    return jsonify(running)


@app.route('/health', methods=['GET'])
def health():
    problems = collect_problems()
    return jsonify({'ok': not problems, 'problems': problems}), (200 if not problems else 503)


@app.route('/stop', methods=['POST', 'OPTIONS'])
def stop():
    if request.method == 'OPTIONS':
        return ('', 204) if origin_allowed(request.headers.get('Origin')) else ('', 403)
    rejection = reject_untrusted_caller()
    if rejection:
        return rejection
    problems = stop_running_instance()
    if problems:
        return jsonify({'status': 'error', 'problems': problems}), 500
    return jsonify({'status': 'stopped'})


# ─── Logging ─────────────────────────────────────────────────

def rotate_file(path, keep=3):
    """Roll path -> path.1 -> path.2 so Odoo logs stay bounded without logrotate."""
    if not os.path.exists(path):
        return
    for index in range(keep - 1, 0, -1):
        older, newer = f'{path}.{index + 1}', f'{path}.{index}'
        if os.path.exists(newer):
            os.replace(newer, older)
    os.replace(path, f'{path}.1')


def tail_file(path, lines):
    try:
        with open(path) as fh:
            return ''.join(fh.readlines()[-lines:])
    except OSError:
        return ''


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, 'server.log'), maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)


def main():
    setup_logging()
    logger.info('Starting odoo-runbot-local server (config %s)', CONFIG_FILE)
    for problem in collect_problems():
        logger.warning('Prerequisite: %s', problem)

    # A server restart must not destroy a working Odoo: with Restart=always a
    # single hiccup would otherwise kill the user's branch and drop its
    # database. Reattach when the recorded instance is still genuinely ours.
    running = read_running()
    if running and is_our_process(running):
        logger.info('Reattached to running instance pid=%s branch=%s db=%s',
                    running.get('pid'), running.get('branch'), running.get('db_name'))
    else:
        stop_running_instance()

    port = CONF['server_port']
    if not port_is_free(port):
        logger.error('Port %s is already in use — refusing to start.', port)
        sys.exit(1)
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
