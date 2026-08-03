"""HTTP surface: the guards that stop an arbitrary website driving this server."""
import importlib

import pytest

GUARD = {'X-Runbot-Local': '1'}
RUNBOT = 'https://runbot.odoo.com'
HOSTILE = 'https://evil.example'
EXTENSION = 'chrome-extension://abcdefghijklmnopabcdefghijklmnop'


@pytest.fixture
def client(valid_config):
    server = importlib.import_module('odoo_runbot_local.server')
    server.app.config['TESTING'] = True
    return server.app.test_client()


@pytest.fixture
def srv(valid_config):
    return importlib.import_module('odoo_runbot_local.server')


# ─── Origin handling ─────────────────────────────────────────

@pytest.mark.parametrize('origin,allowed', [
    (RUNBOT, True),
    (EXTENSION, True),
    ('moz-extension://0123-4567', True),
    (HOSTILE, False),
    ('http://runbot.odoo.com', False),          # scheme must match
    ('https://runbot.odoo.com.evil.test', False),
    ('null', False),
    ('', False),
    (None, False),
])
def test_origin_allowlist(srv, origin, allowed):
    assert srv.origin_allowed(origin) is allowed


def test_no_cors_header_for_hostile_origin(client):
    response = client.post('/stop', headers={'Origin': HOSTILE, **GUARD})
    assert response.status_code == 403
    assert 'Access-Control-Allow-Origin' not in response.headers


def test_cors_header_reflects_allowed_origin(client):
    response = client.open('/stop', method='OPTIONS', headers={'Origin': RUNBOT})
    assert response.status_code == 204
    assert response.headers['Access-Control-Allow-Origin'] == RUNBOT


def test_hostile_preflight_is_refused(client):
    response = client.open('/stop', method='OPTIONS', headers={'Origin': HOSTILE})
    assert response.status_code == 403


def test_vary_origin_is_always_set(client):
    assert client.get('/status').headers['Vary'] == 'Origin'


# ─── Method and header guards ────────────────────────────────

@pytest.mark.parametrize('path', ['/stop', '/checkout'])
def test_mutating_endpoints_reject_get(client, path):
    assert client.get(path).status_code == 405


@pytest.mark.parametrize('path', ['/stop', '/checkout'])
def test_mutating_endpoints_require_the_guard_header(client, path):
    response = client.post(path)
    assert response.status_code == 403
    assert 'X-Runbot-Local' in response.get_json()['error']


def test_absent_origin_is_allowed_with_the_guard_header(client):
    """The CLI and the extension background fetch send no Origin."""
    response = client.post('/stop', headers=GUARD)
    assert response.status_code != 403


# ─── Input validation ────────────────────────────────────────

@pytest.mark.parametrize('branch', ['--maintenance-db=x', '-rf', '', 'a' * 200])
def test_option_like_branches_are_rejected(client, branch):
    response = client.post('/checkout', headers=GUARD, json={
        'branch': branch, 'commit_odoo': 'a' * 40, 'commit_enterprise': 'b' * 40,
    })
    assert response.status_code == 400


@pytest.mark.parametrize('commit', ['nothex', 'abc', '', '../../etc', 'A' * 40])
def test_bad_commits_are_rejected(client, commit):
    response = client.post('/checkout', headers=GUARD, json={
        'branch': 'master', 'commit_odoo': commit, 'commit_enterprise': 'b' * 40,
    })
    assert response.status_code == 400


def test_valid_request_stops_at_prerequisites_not_a_crash(client):
    response = client.post('/checkout', headers=GUARD, json={
        'branch': 'master', 'commit_odoo': 'a' * 40, 'commit_enterprise': 'b' * 40,
    })
    assert response.status_code == 503
    assert response.get_json()['problems']


# ─── Read-only endpoints ─────────────────────────────────────

def test_status_without_an_instance(client):
    assert client.get('/status').get_json() == {'running': False, 'phase': None}


def test_health_lists_problems(client):
    response = client.get('/health')
    body = response.get_json()
    assert response.status_code in (200, 503)
    assert isinstance(body['problems'], list)
    assert body['ok'] is (not body['problems'])
