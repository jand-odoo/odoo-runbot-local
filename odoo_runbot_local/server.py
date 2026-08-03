"""HTTP layer: the local endpoint the browser extension and the CLI talk to.

All process and database work lives in instance.py; this module is only
transport, authorisation and logging.
"""
import logging
import logging.handlers
import os
import re
import sys

from flask import Flask, jsonify, request

from . import config as cfg
from . import instance
from .platform import ports

# Extension origins are set by the browser and cannot be forged by page JS.
EXTENSION_ORIGIN_RE = re.compile(
    r'^(chrome-extension|moz-extension|safari-web-extension)://[A-Za-z0-9._-]+$')
# Requiring this header means a drive-by request must be preflighted, and the
# preflight is refused for any origin outside the allowlist.
GUARD_HEADER = 'X-Runbot-Local'

logger = logging.getLogger('odoo-runbot-local')
app = Flask(__name__)

CONF, CONFIG_PROBLEMS = cfg.load()


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
    """Error response when a mutating call is not from our extension or CLI.

    An Origin header is present on every cross-origin request a web page can
    make; its absence means the call did not come from page JavaScript (the
    extension's own background fetch, or the CLI). When present it must be
    trusted.
    """
    origin = request.headers.get('Origin')
    if origin and not origin_allowed(origin):
        logger.warning('Rejected %s from disallowed origin %s', request.path, origin)
        return jsonify({'error': 'Origin not allowed'}), 403
    if request.headers.get(GUARD_HEADER) != '1':
        logger.warning('Rejected %s without the %s header', request.path, GUARD_HEADER)
        return jsonify({'error': f'Missing {GUARD_HEADER} header'}), 403
    return None


def _preflight():
    return ('', 204) if origin_allowed(request.headers.get('Origin')) else ('', 403)


# ─── Endpoints ───────────────────────────────────────────────

@app.route('/checkout', methods=['POST', 'OPTIONS'])
def checkout():
    if request.method == 'OPTIONS':
        return _preflight()
    rejection = reject_untrusted_caller()
    if rejection:
        return rejection

    payload = request.get_json(silent=True) or {}
    branch = payload.get('branch') or request.args.get('branch', '')
    commit_odoo = payload.get('commit_odoo') or request.args.get('commit_odoo', '')
    commit_enterprise = (payload.get('commit_enterprise')
                         or request.args.get('commit_enterprise', ''))

    if not instance.BRANCH_RE.match(branch or ''):
        return jsonify({'error': f'Invalid branch name: {branch!r}'}), 400
    if not instance.COMMIT_RE.match(commit_odoo or ''):
        return jsonify({'error': 'Invalid odoo commit'}), 400
    if not instance.COMMIT_RE.match(commit_enterprise or ''):
        return jsonify({'error': 'Invalid enterprise commit'}), 400

    # Deliberately excludes worktree state: a checkout is what repairs it.
    problems = instance.collect_problems(CONF, include_worktree_state=False)
    if problems:
        return jsonify({'error': problems[0], 'problems': problems}), 503

    lock = instance.Lock()
    if not lock.acquire():
        return jsonify({'error': 'A checkout is already in progress'}), 429

    try:
        result = instance.checkout_and_start(CONF, branch, commit_odoo, commit_enterprise)
        return jsonify(result)
    except instance.StartError as exc:
        logger.error('%s: %s', exc.message, exc.detail)
        return jsonify({'error': exc.message, 'detail': exc.detail}), exc.status
    finally:
        instance.progress.set(None)
        lock.release()


@app.route('/status', methods=['GET'])
def status():
    return jsonify(instance.status(CONF))


@app.route('/health', methods=['GET'])
def health():
    problems = CONFIG_PROBLEMS + instance.collect_problems(CONF)
    return jsonify({'ok': not problems, 'problems': problems}), (200 if not problems else 503)


@app.route('/stop', methods=['POST', 'OPTIONS'])
def stop():
    if request.method == 'OPTIONS':
        return _preflight()
    rejection = reject_untrusted_caller()
    if rejection:
        return rejection
    problems = instance.stop(CONF)
    if problems:
        return jsonify({'status': 'error', 'problems': problems}), 500
    return jsonify({'status': 'stopped'})


# ─── Entry point ─────────────────────────────────────────────

def setup_logging():
    os.makedirs(cfg.LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        cfg.server_log(), maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    logger.setLevel(logging.INFO)


def main():
    setup_logging()
    logger.info('Starting %s server (config %s)', cfg.APP_NAME, cfg.CONFIG_FILE)
    for problem in CONFIG_PROBLEMS + instance.collect_problems(CONF):
        logger.warning('Prerequisite: %s', problem)

    # A server restart must not destroy a working Odoo: with Restart=always a
    # single hiccup would otherwise kill the user's branch and drop its
    # database. Reattach when the recorded instance is still genuinely ours.
    running = instance.read_running()
    if running and instance.is_our_process(running):
        logger.info('Reattached to running instance pid=%s branch=%s db=%s',
                    running.get('pid'), running.get('branch'), running.get('db_name'))
    else:
        instance.stop(CONF)

    port = CONF['server_port']
    if not ports.is_free(port):
        logger.error('Port %s is already in use — refusing to start.', port)
        sys.exit(1)
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
