"""config, logs and the HTTP client."""
import importlib
import json
import os

import pytest


@pytest.fixture
def config_cmd(valid_config):
    return importlib.import_module('odoo_runbot_local.commands.config_cmd')


@pytest.fixture
def logs_cmd(valid_config):
    return importlib.import_module('odoo_runbot_local.commands.logs')


class Args:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ─── config ──────────────────────────────────────────────────

def test_config_get_prints_a_value(config_cmd, capsys):
    assert config_cmd.run(Args(action='get', key='odoo_port')) == 0
    assert capsys.readouterr().out.strip() == '8099'


def test_config_get_rejects_unknown_key(config_cmd):
    assert config_cmd.run(Args(action='get', key='nonsense')) == 2


def test_config_set_coerces_int(config_cmd, valid_config):
    assert config_cmd.run(Args(action='set', key='odoo_port', value='9001')) == 0
    assert json.load(open(valid_config.CONFIG_FILE))['odoo_port'] == 9001


def test_config_set_rejects_non_numeric_port(config_cmd):
    assert config_cmd.run(Args(action='set', key='odoo_port', value='eighty')) == 2


def test_config_set_parses_a_list(config_cmd, valid_config):
    config_cmd.run(Args(action='set', key='allowed_origins',
                        value='https://a.test,https://b.test'))
    saved = json.load(open(valid_config.CONFIG_FILE))['allowed_origins']
    assert saved == ['https://a.test', 'https://b.test']


def test_config_set_accepts_json_list(config_cmd, valid_config):
    config_cmd.run(Args(action='set', key='allowed_origins', value='["https://x.test"]'))
    assert json.load(open(valid_config.CONFIG_FILE))['allowed_origins'] == \
        ['https://x.test']


def test_config_set_nullable_key(config_cmd, valid_config):
    config_cmd.run(Args(action='set', key='db_host', value='null'))
    assert json.load(open(valid_config.CONFIG_FILE))['db_host'] is None


def test_config_set_refuses_non_nullable_null(config_cmd):
    assert config_cmd.run(Args(action='set', key='odoo_port', value='null')) == 2


def test_config_refuses_to_edit_derived_keys(config_cmd, valid_config):
    """repo_path and version are setup's business; editing them only breaks things."""
    before = json.load(open(valid_config.CONFIG_FILE))
    assert config_cmd.run(Args(action='set', key='repo_path', value='/tmp/x')) == 2
    assert config_cmd.run(Args(action='set', key='version', value='99')) == 2
    assert json.load(open(valid_config.CONFIG_FILE)) == before


def test_config_show_lists_everything(config_cmd, valid_config, capsys):
    assert config_cmd.run(Args(action='show', key=None, value=None)) == 0
    output = capsys.readouterr().out
    for key in valid_config.defaults():
        assert key in output


# ─── logs ────────────────────────────────────────────────────

def test_logs_reads_the_server_log(logs_cmd, valid_config, capsys):
    os.makedirs(valid_config.LOG_DIR, exist_ok=True)
    with open(valid_config.server_log(), 'w') as fh:
        fh.write('line one\nline two\n')

    assert logs_cmd.run(Args(target='server', follow=False, lines=50, quiet=True)) == 0
    assert 'line two' in capsys.readouterr().out


def test_logs_picks_the_odoo_log_by_port(logs_cmd, valid_config, capsys):
    conf, _ = valid_config.load()
    os.makedirs(valid_config.LOG_DIR, exist_ok=True)
    with open(valid_config.odoo_log(conf), 'w') as fh:
        fh.write('odoo says hello\n')

    assert logs_cmd.run(Args(target='odoo', follow=False, lines=50, quiet=True)) == 0
    assert 'odoo says hello' in capsys.readouterr().out


def test_logs_spans_rotated_files(logs_cmd, valid_config, capsys):
    """History longer than the current file must reach into the rotated ones."""
    os.makedirs(valid_config.LOG_DIR, exist_ok=True)
    path = valid_config.server_log()
    with open(f'{path}.1', 'w') as fh:
        fh.write('older line\n')
    with open(path, 'w') as fh:
        fh.write('newer line\n')

    logs_cmd.run(Args(target='server', follow=False, lines=10, quiet=True))
    output = capsys.readouterr().out
    assert 'older line' in output and 'newer line' in output
    assert output.index('older line') < output.index('newer line'), 'wrong order'


def test_logs_reports_a_missing_file(logs_cmd, valid_config):
    assert logs_cmd.run(Args(target='odoo', follow=False, lines=10, quiet=True)) == 1


# ─── client ──────────────────────────────────────────────────

def test_client_reports_unreachable_clearly(valid_config):
    client = importlib.import_module('odoo_runbot_local.client')
    conf, _ = valid_config.load()
    conf['server_port'] = 1        # nothing listens here
    with pytest.raises(client.ServerUnreachable) as excinfo:
        client.get(conf, '/health', timeout=2)
    assert 'Cannot reach' in str(excinfo.value)


def test_client_reachable_is_false_when_down(valid_config):
    client = importlib.import_module('odoo_runbot_local.client')
    conf, _ = valid_config.load()
    conf['server_port'] = 1
    assert client.reachable(conf) is False
