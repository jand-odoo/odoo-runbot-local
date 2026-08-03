import importlib
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _reload_package(home):
    """Re-import the package with a fresh HOME so module-level paths follow it."""
    os.environ['HOME'] = str(home)
    for name in list(sys.modules):
        if name.startswith('odoo_runbot_local'):
            del sys.modules[name]
    return importlib.import_module('odoo_runbot_local.config')


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME so tests never touch the real install."""
    monkeypatch.setenv('HOME', str(tmp_path))
    app_dir = tmp_path / '.odoo-runbot-local'
    app_dir.mkdir()
    return tmp_path


@pytest.fixture
def cfg(home):
    return _reload_package(home)


@pytest.fixture
def write_config(home):
    def _write(data):
        path = home / '.odoo-runbot-local' / 'config.json'
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path
    return _write


@pytest.fixture
def valid_config(home, write_config):
    checkout = home / 'checkout'
    (checkout / 'odoo').mkdir(parents=True)
    (checkout / 'enterprise').mkdir(parents=True)
    write_config({
        'version': 2,
        'mode': 'clone',
        'checkout_path': str(checkout),
        'python': sys.executable,
        'repo_path': REPO,
        'git_protocol': 'ssh',
        'server_port': 8765,
        'odoo_port': 8099,
        'db_user': 'tester',
        'db_host': None,
        'db_port': None,
        'allowed_origins': ['https://runbot.odoo.com'],
    })
    return _reload_package(home)
