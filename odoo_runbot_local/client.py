"""Talking to the local server over HTTP."""
import json
import urllib.error
import urllib.request

from . import config as cfg

GUARD_HEADER = 'X-Runbot-Local'


class ServerUnreachable(Exception):
    pass


class ServerError(Exception):
    def __init__(self, message, detail='', status=0):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


def _url(conf, path):
    return f'http://127.0.0.1:{conf["server_port"]}{path}'


def _request(conf, path, method='GET', payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {GUARD_HEADER: '1'}
    if data:
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(_url(conf, path), data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or '{}')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        raise ServerError(body.get('error') or f'HTTP {exc.code}',
                          body.get('detail', ''), exc.code) from exc
    except urllib.error.URLError as exc:
        raise ServerUnreachable(
            f'Cannot reach the local server on port {conf["server_port"]} ({exc.reason}).\n'
            f'Start it with: systemctl --user restart {cfg.APP_NAME}') from exc
    except (TimeoutError, OSError) as exc:
        raise ServerUnreachable(f'Cannot reach the local server: {exc}') from exc


def get(conf, path, timeout=30):
    return _request(conf, path, 'GET', timeout=timeout)


def post(conf, path, payload=None, timeout=3600):
    return _request(conf, path, 'POST', payload or {}, timeout=timeout)


def reachable(conf):
    try:
        get(conf, '/health', timeout=3)
        return True
    except ServerError:
        return True   # responded, just unhealthy
    except ServerUnreachable:
        return False
