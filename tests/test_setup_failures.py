"""Setup must fail honestly.

The original shell installer structurally could not fail: piped clones hid their
exit status and a final `clear` wiped the warnings before printing hardcoded
checkmarks. These tests pin the opposite behaviour.
"""
import importlib

import pytest


@pytest.fixture
def setup_mod(valid_config):
    return importlib.import_module('odoo_runbot_local.commands.setup')


@pytest.fixture
def args(valid_config):
    class Args:
        checkout = None
        odoo_port = 8099
        server_port = 8765
        yes = True
    return Args()


def test_github_failure_stops_setup(setup_mod, args, monkeypatch, capsys):
    """No access to enterprise must abort, not warn and continue."""
    from odoo_runbot_local.platform import git
    monkeypatch.setattr(git, 'ssh_works', lambda: True)
    monkeypatch.setattr(git, 'reachable', lambda url, timeout=60: 'enterprise' not in url)

    setup = setup_mod.Setup(args)
    assert setup.github_access() is False

    output = capsys.readouterr()
    assert 'NOT reachable' in output.err or 'NOT reachable' in output.out
    assert 'request access' in (output.out + output.err)


def test_postgres_down_stops_setup(setup_mod, args, monkeypatch, capsys):
    from odoo_runbot_local.platform import postgres
    monkeypatch.setattr(postgres, 'ready', lambda config=None: False)
    monkeypatch.setattr(postgres, 'unit_name', lambda: None)

    setup = setup_mod.Setup(args)
    assert setup.postgresql() is False
    assert 'not accepting connections' in capsys.readouterr().err


def test_unknown_package_manager_reports_missing_binaries(setup_mod, args, monkeypatch):
    from odoo_runbot_local.platform import packages
    monkeypatch.setattr(packages, 'detect_manager', lambda: None)
    monkeypatch.setattr(packages, 'missing_binaries', lambda: ['psql', 'ss'])

    setup = setup_mod.Setup(args)
    assert setup.system_packages() is False


def test_unusable_repo_is_reported_not_silently_used(setup_mod, args, monkeypatch):
    from odoo_runbot_local.platform import git
    monkeypatch.setattr(git, 'is_repo', lambda path: False)
    monkeypatch.setattr(git, 'is_worktree', lambda path: True)

    setup = setup_mod.Setup(args)
    setup.checkout = '/tmp/does-not-matter'
    assert setup._verify_repos() is False


def test_broken_venv_is_recreated_not_reused(setup_mod, args, monkeypatch, tmp_path):
    """A directory is not proof of a working interpreter."""
    import odoo_runbot_local.config as cfg
    from odoo_runbot_local.platform import proc

    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    monkeypatch.setattr(cfg, 'VENV_DIR', str(venv))
    monkeypatch.setattr(setup_mod.cfg, 'VENV_DIR', str(venv))

    removed = []
    monkeypatch.setattr(proc, 'run', lambda *a, **k: proc.Result(1))  # interpreter broken
    monkeypatch.setattr(setup_mod.proc, 'run', lambda *a, **k: proc.Result(1))
    monkeypatch.setattr(setup_mod.shutil, 'rmtree',
                        lambda path, **k: removed.append(path))
    monkeypatch.setattr(setup_mod.proc, 'stream', lambda *a, **k: proc.Result(1))

    setup = setup_mod.Setup(args)
    setup.checkout = str(tmp_path / 'checkout')
    setup.virtualenv()
    assert removed, 'a broken virtualenv was reused instead of recreated'


# ─── Step accounting ─────────────────────────────────────────

def test_steps_summary_cannot_claim_success_after_a_failure():
    from odoo_runbot_local import ui
    steps = ui.Steps()
    steps.run('good', lambda: True)
    steps.run('bad', lambda: False)
    assert steps.failed()
    assert [state for _, state in steps.results] == ['ok', 'fail']


def test_steps_records_an_exception_as_failure():
    from odoo_runbot_local import ui

    def explode():
        raise RuntimeError('boom')

    steps = ui.Steps()
    assert steps.run('explodes', explode) is False
    assert steps.failed()


def test_skip_is_not_a_failure():
    from odoo_runbot_local import ui
    steps = ui.Steps()
    steps.run('optional', lambda: 'skip')
    assert not steps.failed()
    assert steps.results == [('optional', 'skip')]
