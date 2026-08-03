"""Branch resolution and commit pairing, against real throwaway git repos.

The interesting case is a branch that exists in only one repository: the other
must be pinned to what it looked like when the branch diverged, not to whatever
its base branch has drifted to since.
"""
import importlib
import os
import subprocess

import pytest

BASE_TIME = 1700000000  # arbitrary fixed epoch so dates are deterministic


def _git(repo, *args, when=None):
    env = dict(os.environ)
    env.update({
        'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 't@example.com',
        'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 't@example.com',
    })
    if when is not None:
        env['GIT_AUTHOR_DATE'] = f'{when} +0000'
        env['GIT_COMMITTER_DATE'] = f'{when} +0000'
    return subprocess.run(['git', *args], cwd=repo, env=env,
                          capture_output=True, text=True, check=True)


def _commit(repo, message, when):
    # One file per commit, so rebases in these fixtures never conflict.
    name = message.replace(' ', '_') + '.txt'
    with open(os.path.join(repo, name), 'w') as fh:
        fh.write(message + '\n')
    _git(repo, 'add', name, when=when)
    _git(repo, 'commit', '-m', message, when=when)
    return _git(repo, 'rev-parse', 'HEAD').stdout.strip()


def _init(path):
    os.makedirs(path)
    _git(path, 'init', '-q', '-b', 'master')
    return path


@pytest.fixture
def world(valid_config, tmp_path):
    """Two upstream repos plus local clones, wired as origin.

    odoo has master with a feature branch forked at an early point; enterprise
    has master only, with commits both before and long after that fork.
    """
    cfg = valid_config
    conf, _ = cfg.load()
    checkout = conf['checkout_path']

    upstream = tmp_path / 'upstream'
    upstream.mkdir()

    shas = {}

    # ── odoo upstream: master, then a feature branch forked at t+100 ──
    odoo_up = _init(str(upstream / 'odoo'))
    _commit(odoo_up, 'odoo base', BASE_TIME)
    shas['odoo_fork_point'] = _commit(odoo_up, 'odoo at fork', BASE_TIME + 100)
    _git(odoo_up, 'checkout', '-q', '-b', 'master-feature-abc')
    shas['odoo_branch'] = _commit(odoo_up, 'feature work', BASE_TIME + 5000)
    _git(odoo_up, 'checkout', '-q', 'master')
    shas['odoo_master_tip'] = _commit(odoo_up, 'odoo moved on', BASE_TIME + 9000)

    # ── enterprise upstream: master only, with commits straddling both fork
    #    points so the right one is distinguishable from the tip ──
    ent_up = _init(str(upstream / 'enterprise'))
    _commit(ent_up, 'ent base', BASE_TIME)
    shas['ent_at_fork'] = _commit(ent_up, 'ent at fork', BASE_TIME + 50)
    shas['ent_mid'] = _commit(ent_up, 'ent midway', BASE_TIME + 8000)
    shas['ent_tip'] = _commit(ent_up, 'ent much later', BASE_TIME + 9500)

    # ── a "dev" fork whose master is an abandoned stub ──
    # This mirrors odoo-dev/odoo, whose master is a 2014 tombstone commit
    # reading "Please use the right repo". Preferring it for version branches
    # silently checks out a decade-old tree.
    dev = tmp_path / 'dev'
    dev.mkdir()
    for name in ('odoo', 'enterprise'):
        fork = _init(str(dev / name))
        shas[f'{name}_dev_stub'] = _commit(
            fork, 'Please use the right repo not this one', BASE_TIME - 100000)

    # ── local clones, with the fork wired up as "dev" ──
    for name in ('odoo', 'enterprise'):
        target = os.path.join(checkout, name)
        if os.path.exists(target):
            import shutil
            shutil.rmtree(target)
        subprocess.run(['git', 'clone', '-q', str(upstream / name), target],
                       check=True, capture_output=True)
        _git(target, 'remote', 'add', 'dev', str(dev / name))
        _git(target, 'fetch', '-q', 'dev')

    return conf, shas


@pytest.fixture
def resolve(valid_config):
    return importlib.import_module('odoo_runbot_local.resolve')


# ─── Base version parsing ────────────────────────────────────

@pytest.mark.parametrize('branch,expected', [
    ('master', 'master'),
    ('master-l10n_cn-tax_report-jand', 'master'),
    ('18.0', '18.0'),
    ('18.0-something-abc', '18.0'),
    ('saas-18.4', 'saas-18.4'),
    ('saas-18.4-fix-xyz', 'saas-18.4'),
    ('no-version-here', None),
    ('feature/thing', None),
])
def test_base_version_parsing(valid_config, branch, expected):
    from odoo_runbot_local.platform import git
    assert git.base_version(branch) == expected


# ─── Resolution ──────────────────────────────────────────────

def test_branch_in_both_repos_uses_both_tips(world, resolve):
    conf, shas = world
    subprocess.run(['git', 'checkout', '-q', '-b', 'master-feature-abc'],
                   cwd=os.path.join(conf['checkout_path'], 'enterprise'), check=True)
    subprocess.run(['git', 'push', '-q', 'origin', 'master-feature-abc'],
                   cwd=os.path.join(conf['checkout_path'], 'enterprise'), check=True)

    result = resolve.resolve(conf, 'master-feature-abc')
    assert result.strategy == 'both'
    assert result.exact
    assert result.commits['odoo'] == shas['odoo_branch']


def test_branch_in_one_repo_pairs_at_the_fork_point(world, resolve):
    """The heart of it: enterprise must land on the fork-point commit, not the tip."""
    conf, shas = world
    result = resolve.resolve(conf, 'master-feature-abc')

    assert result.strategy == 'paired'
    assert not result.exact
    assert result.commits['odoo'] == shas['odoo_branch']
    assert result.commits['enterprise'] == shas['ent_at_fork'], (
        'paired to the wrong enterprise commit — the branch tip or master tip '
        'was used instead of the fork point')
    assert result.commits['enterprise'] != shas['ent_tip']
    assert result.commits['enterprise'] != shas['ent_mid']


def test_pairing_survives_a_rebase(world, resolve):
    """After rebasing onto a newer master the pair must move forward with it."""
    conf, shas = world
    odoo = os.path.join(conf['checkout_path'], 'odoo')

    before = resolve.resolve(conf, 'master-feature-abc').commits['enterprise']
    assert before == shas['ent_at_fork']

    _git(odoo, 'fetch', '-q', 'origin')
    _git(odoo, 'checkout', '-q', '-B', 'master-feature-abc',
         'origin/master-feature-abc')
    _git(odoo, 'rebase', '-q', 'origin/master')
    _git(odoo, 'push', '-q', '-f', 'origin', 'master-feature-abc')

    after = resolve.resolve(conf, 'master-feature-abc').commits['enterprise']
    assert after != before, 'pairing did not follow the rebase'
    assert after == shas['ent_mid'], 'expected the commit contemporaneous with the '\
                                     'new fork point, not the enterprise tip'
    assert after != shas['ent_tip']


def test_version_branch_uses_each_tip(world, resolve):
    conf, shas = world
    result = resolve.resolve(conf, 'master')
    assert result.strategy in ('both', 'version')
    assert result.commits['odoo'] == shas['odoo_master_tip']
    assert result.commits['enterprise'] == shas['ent_tip']


def test_version_branch_ignores_the_dev_fork_stub(world, resolve):
    """odoo-dev's master is an abandoned tombstone; origin must win."""
    conf, shas = world
    result = resolve.resolve(conf, 'master')
    assert result.commits['odoo'] != shas['odoo_dev_stub'], (
        'resolved master to the dev fork — this checks out a 2014 tree with '
        'no odoo-bin in it')
    assert result.commits['enterprise'] != shas['enterprise_dev_stub']


def test_feature_branch_still_prefers_the_dev_fork(world, resolve):
    """The opposite preference: feature branches live in odoo-dev."""
    from odoo_runbot_local.platform import git as gitmod
    conf, _ = world
    odoo = os.path.join(conf['checkout_path'], 'odoo')

    # Put the same branch name on both remotes with different commits.
    _git(odoo, 'checkout', '-q', '-b', 'master-shared-abc', 'origin/master')
    _git(odoo, 'push', '-q', 'origin', 'master-shared-abc')
    dev_sha = _commit(odoo, 'dev only work', BASE_TIME + 6000)
    _git(odoo, 'push', '-q', 'dev', 'master-shared-abc')

    remote, sha = gitmod.find_branch(odoo, 'master-shared-abc', prefer='dev')
    assert remote == 'dev'
    assert sha == dev_sha


def test_unknown_branch_reports_what_was_searched(world, resolve):
    conf, _ = world
    with pytest.raises(resolve.ResolutionError) as excinfo:
        resolve.resolve(conf, 'master-does-not-exist-xyz')
    assert 'not found' in excinfo.value.message
    assert excinfo.value.searched


def test_unparseable_branch_name_explains_itself(world, resolve):
    conf, _ = world
    odoo = os.path.join(conf['checkout_path'], 'odoo')
    subprocess.run(['git', 'checkout', '-q', '-b', 'weirdname'], cwd=odoo, check=True)
    subprocess.run(['git', 'push', '-q', 'origin', 'weirdname'], cwd=odoo, check=True)

    with pytest.raises(resolve.ResolutionError) as excinfo:
        resolve.resolve(conf, 'weirdname')
    assert 'version' in excinfo.value.message
    assert '--odoo-commit' in excinfo.value.message


def test_explicit_commits_bypass_resolution(world, resolve):
    conf, _ = world
    result = resolve.resolve(conf, 'anything', odoo_commit='a' * 40,
                             enterprise_commit='b' * 40)
    assert result.commits == {'odoo': 'a' * 40, 'enterprise': 'b' * 40}
    assert result.exact


def test_search_finds_partial_matches(world, resolve):
    conf, _ = world
    matches = resolve.search(conf, 'feature')
    names = [name for name, _ in matches]
    assert 'master-feature-abc' in names
