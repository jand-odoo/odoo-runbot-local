"""Cleanup of earlier installs.

The stale ~/.odoo-runbot-local/update.sh is the reason this matters: it is the
pre-rewrite shell script, which targets a layout that no longer exists, so
running it fails confusingly rather than harmlessly.
"""
import importlib
import os

import pytest


@pytest.fixture
def legacy(valid_config):
    return importlib.import_module('odoo_runbot_local.legacy')


def _drop(cfg, name, content='#!/bin/bash\n'):
    path = os.path.join(cfg.APP_DIR, name)
    with open(path, 'w') as fh:
        fh.write(content)
    return path


def test_finds_stale_shell_scripts(legacy, valid_config):
    _drop(valid_config, 'update.sh')
    _drop(valid_config, 'lib.sh')
    found = {os.path.basename(path) for path in legacy.stale_files()}
    assert found == {'update.sh', 'lib.sh'}


def test_removes_them(legacy, valid_config):
    for name in ('update.sh', 'doctor.sh', 'lib.sh', 'killport.sh', 'server.py'):
        _drop(valid_config, name)

    removed = legacy.clean_stale_files()
    assert len(removed) == 5
    assert legacy.stale_files() == []


def test_never_touches_files_we_did_not_create(legacy, valid_config):
    """Only names our own old setup wrote are removed."""
    keep = _drop(valid_config, 'my-notes.sh', 'important\n')
    config = os.path.join(valid_config.APP_DIR, 'config.json')
    _drop(valid_config, 'update.sh')

    legacy.clean_stale_files()

    assert os.path.exists(keep), 'removed a file the user put there'
    assert os.path.exists(config), 'removed the config'


def test_clean_is_idempotent(legacy, valid_config):
    _drop(valid_config, 'update.sh')
    assert len(legacy.clean_stale_files()) == 1
    assert legacy.clean_stale_files() == []


def test_no_legacy_dir_reported_when_absent(legacy):
    assert legacy.legacy_dir() is None


def test_legacy_dir_reported_with_size(legacy, home, monkeypatch):
    path = home / '.runbot-local'
    (path / 'venv').mkdir(parents=True)
    with open(path / 'venv' / 'blob', 'wb') as fh:
        fh.write(b'x' * 4096)
    monkeypatch.setattr(legacy, 'LEGACY_APP_DIR', str(path))

    found = legacy.legacy_dir()
    assert found is not None
    reported, size = found
    assert reported == str(path)
    assert size >= 4096


def test_removing_the_legacy_dir_also_removes_its_unit(legacy, home, monkeypatch):
    path = home / '.runbot-local'
    path.mkdir()
    unit = home / '.config' / 'systemd' / 'user' / 'runbot-local.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('[Service]\n')

    monkeypatch.setattr(legacy, 'LEGACY_APP_DIR', str(path))
    monkeypatch.setattr(legacy, 'LEGACY_UNIT', str(unit))

    assert legacy.remove_legacy_dir()
    assert not path.exists()
    assert not unit.exists()


@pytest.mark.parametrize('size,expected', [
    (512, '512 B'),
    (2048, '2.0 KB'),
    (5 * 1024 * 1024, '5.0 MB'),
])
def test_human_size(legacy, size, expected):
    assert legacy.human_size(size) == expected
