"""Putting `orl` on PATH must work for the shell the user actually runs.

A fresh machine is the hard case: ~/.local/bin does not exist, so the standard
~/.profile block (conditional on the directory existing at login) has not run,
and the command is missing right after a successful setup.
"""
import importlib
import os

import pytest


@pytest.fixture
def setup_mod(valid_config):
    return importlib.import_module('odoo_runbot_local.commands.setup')


@pytest.mark.parametrize('shell,expected', [
    ('/usr/bin/zsh', '.zshrc'),
    ('/bin/bash', '.bashrc'),
    ('/usr/bin/fish', 'config.fish'),
    ('/bin/sh', '.profile'),
    ('/opt/weird/shell', '.profile'),
])
def test_rc_file_follows_the_login_shell(setup_mod, monkeypatch, shell, expected):
    monkeypatch.setenv('SHELL', shell)
    assert setup_mod.Setup._shell_rc().endswith(expected)


def test_unset_shell_falls_back_to_the_passwd_entry(setup_mod, monkeypatch):
    """$SHELL is absent in some non-login contexts; /etc/passwd still knows."""
    import pwd
    monkeypatch.delenv('SHELL', raising=False)
    real = pwd.getpwuid(os.getuid()).pw_shell
    assert setup_mod.Setup._login_shell() == os.path.basename(real)


def test_profile_used_when_the_shell_cannot_be_determined(setup_mod, monkeypatch):
    monkeypatch.delenv('SHELL', raising=False)
    monkeypatch.setattr(setup_mod.Setup, '_login_shell', staticmethod(lambda: ''))
    assert setup_mod.Setup._shell_rc().endswith('.profile')


def test_zsh_user_is_not_sent_to_profile(setup_mod, monkeypatch):
    """zsh never sources ~/.profile; suggesting it would be useless advice."""
    monkeypatch.setenv('SHELL', '/usr/bin/zsh')
    assert not setup_mod.Setup._shell_rc().endswith('.profile')


def test_nothing_written_when_already_on_path(setup_mod, monkeypatch, tmp_path):
    bin_dir = str(tmp_path / 'bin')
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}/usr/bin')
    monkeypatch.setenv('SHELL', '/bin/bash')
    rc = tmp_path / '.bashrc'
    rc.write_text('# untouched\n')

    assert setup_mod.Setup._ensure_on_path(bin_dir) is True
    assert rc.read_text() == '# untouched\n'


def test_appends_export_when_missing(setup_mod, monkeypatch, tmp_path, home):
    bin_dir = str(home / '.local' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('SHELL', '/bin/bash')
    monkeypatch.setattr('odoo_runbot_local.ui.ASSUME_YES', True)
    rc = home / '.bashrc'
    rc.write_text('# existing content\n')

    assert setup_mod.Setup._ensure_on_path(bin_dir) is True
    content = rc.read_text()
    assert '# existing content' in content, 'existing rc content was clobbered'
    assert f'export PATH="{bin_dir}:$PATH"' in content


def test_fish_uses_its_own_syntax(setup_mod, monkeypatch, home):
    bin_dir = str(home / '.local' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('SHELL', '/usr/bin/fish')
    monkeypatch.setattr('odoo_runbot_local.ui.ASSUME_YES', True)
    rc = home / '.config' / 'fish' / 'config.fish'
    rc.parent.mkdir(parents=True)
    rc.write_text('')

    setup_mod.Setup._ensure_on_path(bin_dir)
    content = rc.read_text()
    assert 'fish_add_path' in content
    assert 'export PATH' not in content, 'bash syntax written into a fish config'


def test_commented_out_entry_does_not_count_as_configured(setup_mod, monkeypatch, home):
    """A commented line is exactly the trap on this machine's own ~/.zshrc."""
    bin_dir = str(home / '.local' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('SHELL', '/usr/bin/zsh')
    monkeypatch.setattr('odoo_runbot_local.ui.ASSUME_YES', True)
    rc = home / '.zshrc'
    rc.write_text('# export PATH=$HOME/bin:$HOME/.local/bin:/usr/local/bin:$PATH\n')

    setup_mod.Setup._ensure_on_path(bin_dir)
    lines = [line for line in rc.read_text().splitlines()
             if line.strip() and not line.strip().startswith('#')]
    assert lines, 'a commented-out entry was mistaken for a working one'


def test_existing_uncommented_entry_is_left_alone(setup_mod, monkeypatch, home):
    bin_dir = str(home / '.local' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('SHELL', '/bin/bash')
    rc = home / '.bashrc'
    original = f'export PATH="{bin_dir}:$PATH"\n'
    rc.write_text(original)

    assert setup_mod.Setup._ensure_on_path(bin_dir) is True
    assert rc.read_text() == original, 'a duplicate PATH entry was appended'


def test_declining_leaves_the_rc_untouched(setup_mod, monkeypatch, home):
    bin_dir = str(home / '.local' / 'bin')
    monkeypatch.setenv('PATH', '/usr/bin')
    monkeypatch.setenv('SHELL', '/bin/bash')
    monkeypatch.setattr('odoo_runbot_local.ui.confirm', lambda *a, **k: False)
    rc = home / '.bashrc'
    rc.write_text('# mine\n')

    assert setup_mod.Setup._ensure_on_path(bin_dir) is False
    assert rc.read_text() == '# mine\n'
