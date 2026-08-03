"""Configuration schema, paths and migration.

Single source of truth: imported by both the CLI and the server. Previously the
same defaults were written out in setup.sh, update.sh and server.py, which is
three copies free to drift apart.
"""
import getpass
import json
import os

APP_NAME = 'odoo-runbot-local'
CONFIG_VERSION = 2

# The checkout this code is running from. The systemd unit points at it, so it
# must keep existing — doctor verifies that.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HOME = os.path.expanduser('~')
APP_DIR = os.path.join(HOME, '.' + APP_NAME)
LOG_DIR = os.path.join(APP_DIR, 'logs')
VENV_DIR = os.path.join(APP_DIR, 'venv')
CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
RUNNING_FILE = os.path.join(APP_DIR, 'running.json')
LOCK_FILE = os.path.join(APP_DIR, 'checkout.lock')
EXTENSION_DIR = os.path.join(APP_DIR, 'extension')

UNIT_FILE = os.path.join(HOME, '.config', 'systemd', 'user', APP_NAME + '.service')
DESKTOP_FILE = os.path.join(HOME, '.local', 'share', 'applications', APP_NAME + '.desktop')

# No assumption about the user's repo layout.
DEFAULT_CHECKOUT = os.path.join(
    os.environ.get('XDG_DATA_HOME') or os.path.join(HOME, '.local', 'share'),
    APP_NAME, 'checkout',
)

REPOS = ('odoo', 'enterprise')
UPSTREAM = 'odoo'      # github org holding the canonical repos
DEV_ORG = 'odoo-dev'   # github org holding developer feature branches


def _default_user():
    """$USER is unset in a lingering systemd user unit; /etc/passwd is not.

    Never guesses a name. If the user cannot be determined the empty string is
    returned, so the failure surfaces as a visible "role '' cannot connect"
    rather than silently running as somebody else.
    """
    try:
        return getpass.getuser()
    except Exception:
        pass
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ''


def defaults():
    return {
        'version': CONFIG_VERSION,
        'mode': 'clone',                 # or 'worktree'
        'checkout_path': DEFAULT_CHECKOUT,
        'python': os.path.join(VENV_DIR, 'bin', 'python'),
        'repo_path': None,
        'git_protocol': 'ssh',
        'server_port': 8765,
        'odoo_port': 8072,
        'db_user': _default_user(),
        'db_host': None,
        'db_port': None,
        'allowed_origins': ['https://runbot.odoo.com'],
        # Directories appended to the addons path, for repositories this tool
        # does not manage: design-themes, iap-apps, your own addons.
        'extra_addons_paths': [],
    }


# Keys whose value is legitimately None and must not be replaced by the default.
NULLABLE = ('db_host', 'db_port', 'repo_path')


def load(path=CONFIG_FILE):
    """Return (config, problems).

    Never raises: a missing key falls back to its default and malformed JSON is
    reported rather than propagated, so a bad file cannot crash-loop the unit.
    """
    config = defaults()
    problems = []

    if not os.path.exists(path):
        problems.append(f'{path} does not exist — run: {APP_NAME} setup')
        return config, problems

    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except (ValueError, OSError) as exc:
        problems.append(f'{path} could not be read ({exc}) — using built-in defaults.')
        return config, problems

    if not isinstance(loaded, dict):
        problems.append(f'{path} is not a JSON object — using built-in defaults.')
        return config, problems

    missing = [key for key in config if key not in loaded]
    if missing:
        problems.append(
            'Config is missing key(s): %s — defaults used. Run: %s update'
            % (', '.join(sorted(missing)), APP_NAME))
    if loaded.get('version') != CONFIG_VERSION:
        problems.append(
            'Config schema is version %s, expected %s — run: %s update'
            % (loaded.get('version'), CONFIG_VERSION, APP_NAME))

    config.update({
        key: value for key, value in loaded.items()
        if value is not None or key in NULLABLE
    })

    problems.extend(_coerce(config))
    return config, problems


def _coerce(config):
    """Force hand-edited values into usable types. Returns a list of problems."""
    problems = []
    base = defaults()

    if not isinstance(config['allowed_origins'], list):
        problems.append('"allowed_origins" must be a list — using the default.')
        config['allowed_origins'] = list(base['allowed_origins'])

    for key in ('server_port', 'odoo_port'):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError):
            problems.append(f'"{key}" is not a number — using {base[key]}.')
            config[key] = base[key]

    return problems


def save(config, path=CONFIG_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(config, fh, indent=2)
        fh.write('\n')
    os.replace(tmp, path)   # atomic: a crash mid-write cannot truncate the config


def migrate(path=CONFIG_FILE):
    """Bring an older config up to the current schema. Returns (config, changed)."""
    existing = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                existing = loaded
        except (ValueError, OSError):
            pass  # unreadable: rebuild from defaults rather than fail

    before = json.dumps(existing, sort_keys=True)
    config = defaults()
    config.update({
        key: value for key, value in existing.items()
        if value is not None or key in NULLABLE
    })
    config['version'] = CONFIG_VERSION
    _coerce(config)

    changed = json.dumps(config, sort_keys=True) != before
    return config, changed


# ─── Derived paths ───────────────────────────────────────────

def repo_path(config, repo):
    return os.path.join(config['checkout_path'], repo)


def odoo_bin(config):
    return os.path.join(config['checkout_path'], 'odoo', 'odoo-bin')


def addons_paths(config, extra=()):
    """The managed checkouts, then anything the user has added.

    Order matters: Odoo takes the first match, so the managed repositories win
    and an extra directory cannot silently shadow a core module.
    """
    checkout = config['checkout_path']
    paths = [
        os.path.join(checkout, 'odoo', 'addons'),
        os.path.join(checkout, 'odoo', 'odoo', 'addons'),
        os.path.join(checkout, 'enterprise'),
    ]
    for path in list(config.get('extra_addons_paths') or ()) + list(extra or ()):
        resolved = os.path.abspath(os.path.expanduser(path))
        if resolved not in paths:
            paths.append(resolved)
    return paths


def server_log(config=None):
    return os.path.join(LOG_DIR, 'server.log')


def odoo_log(config):
    return os.path.join(LOG_DIR, f"odoo-{config['odoo_port']}.log")


def repo_url(protocol, org, name):
    if protocol == 'https':
        return f'https://github.com/{org}/{name}.git'
    return f'git@github.com:{org}/{name}.git'
