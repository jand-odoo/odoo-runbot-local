"""Instance lifecycle: database naming, pid identity, and port safety."""
import importlib
import os
import subprocess
import sys
import time

import pytest


@pytest.fixture
def inst(valid_config):
    return importlib.import_module('odoo_runbot_local.instance')


@pytest.fixture
def conf(valid_config):
    return valid_config.load()[0]


# ─── Database naming ─────────────────────────────────────────

@pytest.mark.parametrize('branch', [
    '--maintenance-db=pwned', '-rf', '../../etc/passwd', '$(whoami)',
    "a'b\"c;d", '18.0', 'master-l10n_cn-tax_report-jand', '',
])
def test_slug_can_never_be_read_as_an_option(inst, branch):
    slug = inst.slugify_branch(branch)
    assert slug
    assert slug[0].isalpha(), f'{branch!r} -> {slug!r} could be parsed as a flag'
    assert slug.replace('_', 'a').isalnum()
    assert len(slug) <= 63


def test_numeric_version_branches_stay_distinct(inst):
    names = ['18.0', '17.0', '16.0', 'saas-18.4', 'master']
    slugs = {inst.slugify_branch(name) for name in names}
    assert len(slugs) == len(names)


def test_branch_regex_rejects_leading_dash(inst):
    assert not inst.BRANCH_RE.match('--maintenance-db=x')
    assert not inst.BRANCH_RE.match('-rf')
    assert inst.BRANCH_RE.match('master-l10n_cn-tax_report-jand')


# ─── Process identity ────────────────────────────────────────

def test_is_our_process_rejects_a_dead_pid(inst):
    assert not inst.is_our_process({'pid': 999999, 'start_time': 12345})


def test_is_our_process_rejects_a_recycled_pid(inst):
    # Same pid, different start time: the pid was reused by something else.
    assert not inst.is_our_process({'pid': os.getpid(), 'start_time': 1})


def test_is_our_process_accepts_the_real_thing(inst):
    from odoo_runbot_local.platform import ports
    running = {'pid': os.getpid(), 'start_time': ports.start_time(os.getpid())}
    assert inst.is_our_process(running)


def test_is_our_process_rejects_empty_state(inst):
    assert not inst.is_our_process({})


def test_process_is_ours_only_for_our_checkout(inst, conf, tmp_path):
    odoo_bin = os.path.join(conf['checkout_path'], 'odoo', 'odoo-bin')
    with open(odoo_bin, 'w') as fh:
        fh.write('import time; time.sleep(30)\n')

    ours = subprocess.Popen([sys.executable, odoo_bin])
    stranger = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    time.sleep(0.8)
    try:
        assert inst.process_is_ours(conf, ours.pid)
        assert not inst.process_is_ours(conf, stranger.pid)
    finally:
        for process in (ours, stranger):
            process.kill()
            process.wait()


# ─── Port safety ─────────────────────────────────────────────

def test_stop_never_kills_a_foreign_process_on_the_odoo_port(inst, conf):
    """A developer's own Odoo on this port must survive."""
    port = conf['odoo_port']
    victim = subprocess.Popen([
        sys.executable, '-c',
        f'import socket,time\n'
        f's=socket.socket();s.setsockopt(1,2,1);s.bind(("127.0.0.1",{port}));s.listen(1)\n'
        f'time.sleep(30)',
    ])
    time.sleep(1.2)
    try:
        assert victim.poll() is None, 'listener failed to start'
        problems = inst.stop(conf)

        time.sleep(0.5)
        assert victim.poll() is None, 'a foreign process was killed'
        assert any('in use by another process' in problem for problem in problems)
        assert any(str(victim.pid) in problem for problem in problems)
    finally:
        victim.kill()
        victim.wait()


def test_stop_is_clean_when_nothing_is_running(inst, conf):
    assert inst.stop(conf) == []


# ─── Running state ───────────────────────────────────────────

def test_running_state_round_trips(inst):
    inst.write_running({'pid': 1, 'branch': 'master'})
    assert inst.read_running()['branch'] == 'master'
    inst.clear_running()
    assert inst.read_running() == {}


def test_read_running_tolerates_corruption(inst, valid_config):
    with open(valid_config.RUNNING_FILE, 'w') as fh:
        fh.write('{ broken')
    assert inst.read_running() == {}


def test_status_without_an_instance(inst, conf):
    assert inst.status(conf) == {'running': False, 'phase': None}


# ─── Prerequisites ───────────────────────────────────────────

def test_collect_problems_reports_missing_repos(inst, conf):
    problems = inst.collect_problems(conf)
    assert any('Repository not found' in problem or 'no git remotes' in problem
               for problem in problems)


def test_lock_is_exclusive(inst):
    first, second = inst.Lock(), inst.Lock()
    assert first.acquire()
    try:
        assert not second.acquire(), 'two checkouts could run at once'
    finally:
        first.release()
    assert second.acquire()
    second.release()


# ─── Self-repair ─────────────────────────────────────────────

def test_checkout_is_not_blocked_by_a_bad_current_commit(inst, conf):
    """One bad commit pair must not leave the tool unable to check out a good one."""
    blocking = inst.collect_problems(conf, include_worktree_state=False)
    assert not any('odoo-bin' in problem for problem in blocking), (
        'a missing odoo-bin blocks /checkout — the tool would brick itself')


def test_missing_odoo_bin_is_still_reported_for_diagnosis(inst, conf):
    reported = inst.collect_problems(conf, include_worktree_state=True)
    assert any('odoo-bin' in problem for problem in reported)
