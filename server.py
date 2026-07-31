#!/usr/bin/env python3
import os
import sys
import json
import time
import signal
import subprocess
import logging
import re
import shutil
from pathlib import Path
import psycopg2
from flask import Flask, request, jsonify

HOME = os.path.expanduser('~')
APP_DIR = os.path.join(HOME, '.odoo-runbot-local')
CHECKOUT_DIR = os.path.join(HOME, 'odoo', 'repositories', 'localdev', 'odoo-runbot-local')
LOG_DIR = os.path.join(APP_DIR, 'logs')
PID_FILE = os.path.join(APP_DIR, 'pid')
RUNNING_FILE = os.path.join(APP_DIR, 'running.json')
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
LOCK_FILE = os.path.join(APP_DIR, 'checkout.lock')
ODOO_PORT = 8072
SERVER_HOST = '127.0.0.1'
SERVER_PORT = 8765
BRANCH_RE = re.compile(r'^[a-zA-Z0-9_./-]+$')
COMMIT_RE = re.compile(r'^[a-f0-9]{7,40}$')

logger = logging.getLogger('odoo-runbot-local')

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        'checkout_path': CHECKOUT_DIR,
        'python': sys.executable,
    }


config = load_config()


def slugify_branch(branch):
    return branch.replace('/', '_')[:63]


def read_running():
    if os.path.exists(RUNNING_FILE):
        with open(RUNNING_FILE) as f:
            return json.load(f)
    return {}


def write_running(data):
    os.makedirs(os.path.dirname(RUNNING_FILE), exist_ok=True)
    with open(RUNNING_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def acquire_lock():
    if os.path.exists(LOCK_FILE):
        return False
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


def kill_process_on_port(port):
    try:
        result = subprocess.run(
            ['lsof', '-t', f'-i:{port}'],
            capture_output=True, text=True, timeout=10
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            if not pid:
                continue
            try:
                pid_int = int(pid)
                os.kill(pid_int, signal.SIGTERM)
                time.sleep(1)
                os.kill(pid_int, signal.SIGKILL)
                time.sleep(0.5)
            except (ProcessLookupError, ValueError, OSError):
                pass
    except Exception as e:
        logger.warning(f"Failed to kill process on port {port}: {e}")


def kill_process_group(pid):
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGINT)
    except (ProcessLookupError, OSError):
        pass


def drop_running_database(running):
    db_name = running.get('db_name')
    if db_name:
        try:
            subprocess.run(['dropdb', '--if-exists', db_name], capture_output=True, timeout=30)
            logger.info(f"Dropped database {db_name}")
        except Exception as e:
            logger.warning(f"Failed to drop database {db_name}: {e}")


def kill_existing_instance():
    running = read_running()
    pid = running.get('pid')
    if pid:
        drop_running_database(running)
        kill_process_group(pid)
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(1)
            except ProcessLookupError:
                break
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        time.sleep(1)
    kill_process_on_port(ODOO_PORT)
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)
    if os.path.exists(RUNNING_FILE):
        os.remove(RUNNING_FILE)


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = logging.FileHandler(os.path.join(LOG_DIR, 'server.log'))
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(fh)
    logger.setLevel(logging.INFO)


def check_prerequisites():
    try:
        subprocess.run(['psql', '-c', 'SELECT 1'], capture_output=True, timeout=5)
    except Exception as e:
        logger.error(f"PostgreSQL not reachable: {e}")
        return False
    for repo in ['odoo', 'enterprise']:
        path = os.path.join(config['checkout_path'], repo)
        git_dir = os.path.join(path, '.git')
        if not os.path.exists(git_dir):
            logger.error(f"Repo not found at {path}")
            return False
    return True


def run_git(repo_path, *args):
    try:
        result = subprocess.run(
            ['git'] + list(args),
            cwd=repo_path,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error(f"git {' '.join(args)} in {repo_path}: {result.stderr.strip()}")
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        logger.error(f"git command timed out in {repo_path}: {' '.join(args)}")
        return False, '', 'Timeout'
    except Exception as e:
        logger.error(f"git error in {repo_path}: {e}")
        return False, '', str(e)


def ensure_commit(repo_path, commit):
    ok, _, _ = run_git(repo_path, 'cat-file', '-e', commit)
    if ok:
        return True
    logger.info(f"Fetching {commit} in {repo_path}")
    for remote in get_remotes(repo_path):
        ok, _, _ = run_git(repo_path, 'fetch', remote)
        if ok:
            ok, _, _ = run_git(repo_path, 'cat-file', '-e', commit)
            if ok:
                return True
    # Shallow clones (--depth 1) need --unshallow to reach old commits
    ok, _, _ = run_git(repo_path, 'fetch', '--unshallow')
    if ok:
        ok, _, _ = run_git(repo_path, 'cat-file', '-e', commit)
        if ok:
            return True
    return False


def get_remotes(repo_path):
    ok, out, _ = run_git(repo_path, 'remote')
    if ok and out:
        return [r.strip() for r in out.split('\n') if r.strip()]
    return ['origin']


def is_db_initialized(db_name):
    try:
        conn = psycopg2.connect(dbname=db_name, user=os.environ.get('USER', 'odoo'))
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'ir_module_module'")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return result is not None
    except Exception:
        return False


@app.route('/checkout', methods=['GET'])
def checkout():
    branch = request.args.get('branch', '')
    commit_odoo = request.args.get('commit_odoo', '')
    commit_enterprise = request.args.get('commit_enterprise', '')

    if not branch or not BRANCH_RE.match(branch):
        return jsonify({'error': 'Invalid branch name'}), 400
    if not commit_odoo or not COMMIT_RE.match(commit_odoo):
        return jsonify({'error': 'Invalid odoo commit'}), 400
    if not commit_enterprise or not COMMIT_RE.match(commit_enterprise):
        return jsonify({'error': 'Invalid enterprise commit'}), 400

    if not check_prerequisites():
        return jsonify({'error': 'Server not ready — check logs'}), 500

    if not acquire_lock():
        return jsonify({'error': 'Checkout already in progress'}), 429

    try:
        slug = slugify_branch(branch)
        checkout_path = config['checkout_path']

        for repo, commit in [('odoo', commit_odoo), ('enterprise', commit_enterprise)]:
            repo_path = os.path.join(checkout_path, repo)
            if not ensure_commit(repo_path, commit):
                return jsonify({'error': f'Failed to fetch commit {commit} in {repo}'}), 500
            ok, _, _ = run_git(repo_path, 'checkout', '-f', commit)
            if not ok:
                return jsonify({'error': f'Failed to checkout commit {commit} in {repo}'}), 500

        kill_existing_instance()

        try:
            subprocess.run(['createdb', slug], capture_output=True, timeout=30)
        except Exception as e:
            logger.error(f"Database error: {e}")
            return jsonify({'error': f'Database setup failed: {e}'}), 500

        addons_paths = [
            os.path.join(checkout_path, 'odoo', 'addons'),
            os.path.join(checkout_path, 'odoo', 'odoo', 'addons'),
            os.path.join(checkout_path, 'enterprise'),
        ]

        odoo_log = os.path.join(LOG_DIR, 'odoo-8072.log')
        cmd = [
            config['python'],
            os.path.join(checkout_path, 'odoo', 'odoo-bin'),
            '-d', slug,
            '--db_user', os.environ.get('USER', 'odoo'),
            '--http-port', str(ODOO_PORT),
            '--http-interface', '127.0.0.1',
            '--addons-path', ','.join(addons_paths),
            '--logfile', odoo_log,
            '--without-demo', 'all',
        ]

        if not is_db_initialized(slug):
            cmd.extend(['-i', 'base'])
            logger.info(f"Database {slug} not initialized, adding -i base")

        try:
            odoo_log_handle = open(odoo_log, 'a')
            proc = subprocess.Popen(
                cmd,
                stdout=odoo_log_handle,
                stderr=subprocess.STDOUT,
                preexec_fn=lambda: os.setpgid(0, 0),
            )
            odoo_log_handle.close()
            running = {
                'pid': proc.pid,
                'branch': branch,
                'db_name': slug,
                'started_at': time.time(),
                'odoo_commit': commit_odoo,
                'enterprise_commit': commit_enterprise,
            }
            write_running(running)
            logger.info(f"Started odoo (pid={proc.pid}) for {branch} on port {ODOO_PORT}")
            return jsonify({
                'url': f'http://localhost:{ODOO_PORT}',
                'pid': proc.pid,
                'db': slug,
            })
        except Exception as e:
            logger.error(f"Failed to start odoo: {e}")
            return jsonify({'error': f'Failed to start odoo: {e}'}), 500
    finally:
        release_lock()


@app.route('/status', methods=['GET'])
def status():
    running = read_running()
    if not running:
        return jsonify({'running': False})
    uptime = time.time() - running.get('started_at', time.time())
    running['uptime_seconds'] = int(uptime)
    running['running'] = True
    alive = True
    pid = running.get('pid')
    if pid:
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
    running['alive'] = alive
    return jsonify(running)


@app.route('/stop', methods=['GET'])
def stop():
    kill_existing_instance()
    return jsonify({'status': 'stopped'})


if __name__ == '__main__':
    setup_logging()
    logger.info("Starting odoo-runbot-local server")
    kill_existing_instance()
    if not check_prerequisites():
        logger.warning("Prerequisites not met — server will reject checkouts")
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
