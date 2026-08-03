"""Passing extra arguments through to odoo-bin.

The `--` split happens before argparse. argparse.REMAINDER looks like the right
tool but swallows options that follow the positional, which silently turned
`run BRANCH --dry-run` into a real start.
"""
import importlib

import pytest


@pytest.fixture
def main_mod(valid_config):
    return importlib.import_module('odoo_runbot_local.__main__')


@pytest.fixture
def inst(valid_config):
    return importlib.import_module('odoo_runbot_local.instance')


# ─── Splitting ───────────────────────────────────────────────

@pytest.mark.parametrize('argv,ours,theirs', [
    (['run', 'master'], ['run', 'master'], []),
    (['run', 'master', '--', '-i', 'sale'], ['run', 'master'], ['-i', 'sale']),
    (['run', 'master', '--dry-run', '--', '-u', 'account'],
     ['run', 'master', '--dry-run'], ['-u', 'account']),
    (['run', '--', '--dev', 'all'], ['run'], ['--dev', 'all']),
    # A second -- belongs to odoo-bin, not to us.
    (['run', 'master', '--', '-i', 'sale', '--', 'x'],
     ['run', 'master'], ['-i', 'sale', '--', 'x']),
])
def test_split_passthrough(main_mod, argv, ours, theirs):
    assert main_mod.split_passthrough(argv) == (ours, theirs)


@pytest.mark.parametrize('argv', [
    ['run', 'master', '--dry-run'],
    ['run', 'master', '--dry-run', '--', '-i', 'sale'],
    ['run', '--dry-run', 'master', '--', '-i', 'sale'],
])
def test_our_own_flags_are_never_swallowed(main_mod, argv):
    """The regression: --dry-run after the branch used to be eaten."""
    ours, _ = main_mod.split_passthrough(argv)
    args = main_mod.build_parser().parse_args(ours)
    assert args.dry_run is True, 'a flag of ours was consumed as passthrough'
    assert args.branch == 'master'


def test_passthrough_reaches_the_command(main_mod):
    ours, theirs = main_mod.split_passthrough(['run', 'master', '--', '-i', 'sale'])
    args = main_mod.build_parser().parse_args(ours)
    args.odoo_args = theirs
    assert args.odoo_args == ['-i', 'sale']


# ─── Validation ──────────────────────────────────────────────

@pytest.mark.parametrize('arg', [
    '-d', '--database', '--http-port', '--addons-path', '--logfile',
    '--http-interface', '--db_user',
])
def test_reserved_flags_are_refused(inst, arg):
    message = inst.check_extra_args([arg, 'value'])
    assert message and arg in message


def test_reserved_flags_refused_in_equals_form(inst):
    assert inst.check_extra_args(['--http-port=9000'])


def test_reserved_flag_message_points_somewhere_useful(inst):
    assert 'config set odoo_port' in inst.check_extra_args(['--http-port', '9000'])


@pytest.mark.parametrize('args', [
    ['-i', 'sale'],
    ['-u', 'account,sale'],
    ['--dev', 'all'],
    ['--log-level=debug'],
    ['--workers', '2'],
    [],
])
def test_ordinary_arguments_are_allowed(inst, args):
    assert inst.check_extra_args(args) is None


def test_null_bytes_refused(inst):
    assert inst.check_extra_args(['--dev\0evil'])


# ─── Interaction with automatic -i base ──────────────────────

@pytest.mark.parametrize('args,expected', [
    (['-i', 'sale'], True),
    (['--init', 'sale'], True),
    (['-i=sale'], True),
    (['--init=sale'], True),
    (['-u', 'sale'], False),
    (['--dev', 'all'], False),
    ([], False),
])
def test_detects_a_user_supplied_install(inst, args, expected):
    """Our own -i base must stand down when the caller supplies -i, because
    odoo-bin honours only the last one."""
    assert inst._installs_modules(args) is expected


# ─── Extra addons directories ────────────────────────────────

def test_managed_paths_come_first(valid_config, tmp_path):
    """Odoo takes the first match, so an extra directory must not be able to
    shadow a core module."""
    extra = tmp_path / 'mine'
    extra.mkdir()
    conf, _ = valid_config.load()
    paths = valid_config.addons_paths(conf, [str(extra)])
    assert paths[-1] == str(extra)
    assert paths[0].endswith('odoo/addons')


def test_config_and_per_run_paths_both_apply(valid_config, tmp_path):
    from_config, from_run = tmp_path / 'a', tmp_path / 'b'
    from_config.mkdir()
    from_run.mkdir()
    conf, _ = valid_config.load()
    conf['extra_addons_paths'] = [str(from_config)]
    paths = valid_config.addons_paths(conf, [str(from_run)])
    assert str(from_config) in paths and str(from_run) in paths


def test_duplicate_paths_are_not_repeated(valid_config, tmp_path):
    extra = tmp_path / 'dup'
    extra.mkdir()
    conf, _ = valid_config.load()
    conf['extra_addons_paths'] = [str(extra)]
    paths = valid_config.addons_paths(conf, [str(extra)])
    assert paths.count(str(extra)) == 1


def test_user_home_is_expanded(valid_config, home):
    extra = home / 'addons'
    extra.mkdir()
    conf, _ = valid_config.load()
    paths = valid_config.addons_paths(conf, ['~/addons'])
    assert str(extra) in paths
    assert not any(p.startswith('~') for p in paths)


def test_missing_directory_is_rejected(inst, tmp_path):
    """Odoo silently ignores a bad addons dir, so a typo would surface much
    later as a module that cannot be found."""
    message = inst.check_addons_paths([str(tmp_path / 'nope')])
    assert message and 'Not a directory' in message


def test_existing_directory_accepted(inst, tmp_path):
    assert inst.check_addons_paths([str(tmp_path)]) is None
