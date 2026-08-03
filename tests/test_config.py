"""Config must never raise: a bad file cannot be allowed to crash-loop the unit."""
import getpass
import json


def test_missing_file_uses_defaults_and_reports(cfg):
    conf, problems = cfg.load()
    assert conf == {**cfg.defaults(), **conf}
    assert set(conf) == set(cfg.defaults())
    assert any('does not exist' in problem for problem in problems)


def test_malformed_json_does_not_raise(cfg, write_config):
    write_config('not json at all {{{')
    conf, problems = cfg.load()
    assert conf['server_port'] == 8765
    assert any('could not be read' in problem for problem in problems)


def test_json_that_is_not_an_object(cfg, write_config):
    write_config('[1, 2, 3]')
    conf, problems = cfg.load()
    assert conf['odoo_port'] == 8072
    assert any('not a JSON object' in problem for problem in problems)


def test_partial_config_fills_defaults_and_names_missing_keys(cfg, write_config):
    write_config({'checkout_path': '/somewhere'})
    conf, problems = cfg.load()
    assert conf['checkout_path'] == '/somewhere'
    assert conf['server_port'] == 8765
    assert any('missing key' in problem for problem in problems)


def test_old_schema_version_is_reported(cfg, write_config):
    write_config({'version': 1, 'checkout_path': '/x'})
    _, problems = cfg.load()
    assert any('version 1' in problem for problem in problems)


def test_nullable_keys_stay_none(cfg, write_config):
    write_config({**cfg.defaults(), 'db_host': None, 'db_port': None})
    conf, _ = cfg.load()
    assert conf['db_host'] is None
    assert conf['db_port'] is None


def test_hand_edited_bad_types_are_coerced(cfg, write_config):
    write_config({**cfg.defaults(), 'server_port': 'eighty', 'allowed_origins': 'nope'})
    conf, problems = cfg.load()
    assert conf['server_port'] == 8765
    assert conf['allowed_origins'] == ['https://runbot.odoo.com']
    assert len(problems) >= 2


def test_db_user_derives_from_the_system_never_a_literal(cfg):
    assert cfg.defaults()['db_user'] == getpass.getuser()
    source = open(cfg.__file__).read()
    assert "environ.get('USER'" not in source
    assert '"odoo"' not in source.replace("'odoo'", '')


def test_migrate_preserves_existing_values(cfg, write_config):
    write_config({'checkout_path': '/keep/me', 'python': '/keep/py'})
    conf, changed = cfg.migrate()
    assert changed
    assert conf['version'] == 2
    assert conf['checkout_path'] == '/keep/me'
    assert conf['python'] == '/keep/py'
    assert 'allowed_origins' in conf


def test_migrate_is_idempotent(cfg, write_config):
    write_config({'checkout_path': '/keep/me'})
    conf, _ = cfg.migrate()
    cfg.save(conf)
    _, changed = cfg.migrate()
    assert not changed


def test_migrate_rebuilds_from_unreadable_file(cfg, write_config):
    write_config('}{ broken')
    conf, changed = cfg.migrate()
    assert changed
    assert conf['version'] == 2


def test_save_is_atomic_and_round_trips(cfg, tmp_path):
    conf = cfg.defaults()
    conf['odoo_port'] = 9999
    cfg.save(conf)
    assert json.load(open(cfg.CONFIG_FILE))['odoo_port'] == 9999
    assert not (tmp_path / '.odoo-runbot-local' / 'config.json.tmp').exists()
