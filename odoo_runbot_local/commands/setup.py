"""Interactive installer. Idempotent: safe to re-run after fixing a failed step."""
import glob
import os
import shutil

from .. import config as cfg
from .. import ui
from ..platform import git, packages, postgres, proc, systemd
from . import extension

TOTAL_STEPS = 10


class Setup:
    """Holds the state resolved as the steps run."""

    def __init__(self, args):
        self.args = args
        self.checkout = None
        self.mode = None
        self.protocol = None
        self.db_user = cfg._default_user()
        self.odoo_port = args.odoo_port
        self.server_port = args.server_port
        self.manager = packages.detect_manager()

    # ─── 1. System packages ──────────────────────────────────
    def system_packages(self):
        ui.step(1, TOTAL_STEPS, 'System Packages')

        if not self.manager:
            ui.warn('Unrecognised package manager — checking for the binaries instead.')
            missing = packages.missing_binaries()
            if missing:
                ui.err(f'Missing required tools: {" ".join(missing)}')
                ui.info("Install them with your distribution's package manager, "
                        'then re-run setup.')
                return False
            ui.ok('All required tools are present')
            return True

        ui.info(f'Package manager: {self.manager}')
        missing = packages.missing(self.manager, postgres_ready=postgres.ready())
        if not missing:
            ui.ok('All system packages already installed')
            return True

        ui.info(f'Missing packages: {" ".join(missing)}')
        if not ui.confirm('Install them now?'):
            ui.err('Cannot continue without these packages.')
            return False

        result = packages.install(self.manager, missing)
        if not result.ok:
            ui.err(f'Package installation failed: {result.stderr or result.returncode}')
            return False
        ui.ok('Packages installed')
        return True

    # ─── 2. PostgreSQL ───────────────────────────────────────
    def postgresql(self):
        ui.step(2, TOTAL_STEPS, 'PostgreSQL')

        if not postgres.ready():
            unit = postgres.unit_name()
            if unit:
                ui.info(f'Starting {unit}...')
                proc.run(['sudo', 'systemctl', 'start', unit], timeout=120)
                proc.run(['sudo', 'systemctl', 'enable', unit], timeout=120)
                import time
                time.sleep(2)

        if not postgres.ready():
            ui.err('PostgreSQL is not accepting connections.')
            ui.info(postgres.initdb_hint(self.manager))
            return False
        ui.ok('PostgreSQL is accepting connections')

        if not self.db_user:
            ui.err('Could not determine the current username.')
            return False

        conf = {'db_user': self.db_user}
        connected, _ = postgres.can_connect(conf)
        if connected:
            ui.ok(f"Role '{self.db_user}' can connect")
        else:
            ui.info(f"Creating PostgreSQL role '{self.db_user}' (needs sudo)...")
            postgres.create_role(self.db_user)
            connected, error = postgres.can_connect(conf)
            if not connected:
                ui.err(f"Role '{self.db_user}' still cannot connect to PostgreSQL.")
                ui.detail(error)
                ui.info(f'Create it manually: sudo -u postgres createuser -d -R -S '
                        f'{self.db_user}')
                return False
            ui.ok(f"Role '{self.db_user}' created")

        # odoo-bin expects a database named after the role for maintenance queries.
        created, _ = postgres.create_database(self.db_user)
        ui.ok(f"Database '{self.db_user}' ready" if created
              else f"Database '{self.db_user}' could not be created")
        return True

    # ─── 3. GitHub access ────────────────────────────────────
    def github_access(self):
        ui.step(3, TOTAL_STEPS, 'GitHub Access')

        ui.info('Probing SSH access to github.com...')
        if git.ssh_works():
            self.protocol = 'ssh'
            ui.ok('SSH access to GitHub works')
        else:
            ui.info('SSH did not authenticate. Checking for HTTPS credentials...')
            if git.https_works():
                self.protocol = 'https'
                ui.ok('HTTPS credentials found — will use HTTPS')
            else:
                ui.warn('Neither SSH nor HTTPS credentials are working yet.')
                if not self._setup_ssh_key():
                    return False
                if not git.ssh_works():
                    ui.err('Still cannot authenticate to GitHub.')
                    ui.info('Add an SSH key at https://github.com/settings/ssh/new')
                    ui.info('or authenticate over HTTPS with: gh auth login')
                    return False
                self.protocol = 'ssh'
                ui.ok('SSH access to GitHub works')

        # Access must be verified, not assumed: enterprise is private, and a
        # silent failure here is what makes the tool look broken much later.
        failed = False
        for org in (cfg.UPSTREAM, cfg.DEV_ORG):
            for name in cfg.REPOS:
                url = cfg.repo_url(self.protocol, org, name)
                if git.reachable(url):
                    ui.ok(f'{org}/{name} reachable')
                else:
                    ui.err(f'{org}/{name} NOT reachable ({url})')
                    failed = True

        if failed:
            ui.err('Your GitHub account is missing access to at least one required '
                   'repository.')
            ui.info(f'{cfg.UPSTREAM}/enterprise and {cfg.DEV_ORG} are Odoo-employee '
                    'repositories — request access, then re-run setup.')
            return False
        ui.ok(f'All required repositories are reachable over {self.protocol}')
        return True

    def _setup_ssh_key(self):
        key = os.path.join(cfg.HOME, '.ssh', 'id_ed25519')
        if not os.path.exists(key):
            if not ui.confirm(f'Generate a new SSH key at {key}?'):
                return False
            email = ui.ask('GitHub email for the key comment',
                           f'{self.db_user}@{os.uname().nodename}')
            os.makedirs(os.path.dirname(key), mode=0o700, exist_ok=True)
            result = proc.run(['ssh-keygen', '-t', 'ed25519', '-f', key, '-N', '',
                               '-C', email], timeout=120)
            if not result.ok:
                ui.err(f'ssh-keygen failed: {result.stderr}')
                return False
            ui.ok('SSH key generated')

        for pub in sorted(glob.glob(os.path.join(cfg.HOME, '.ssh', '*.pub'))):
            print()
            ui.info(f'Public key ({pub}):')
            with open(pub) as fh:
                print(fh.read().strip())
            break

        if not ui.interactive():
            ui.warn('Non-interactive: add the key above to GitHub, then re-run setup.')
            return False

        if ui.confirm('Open the GitHub SSH settings page?'):
            proc.run(['xdg-open', 'https://github.com/settings/ssh/new'], timeout=20)
        print()
        ui.info('Add the key above at https://github.com/settings/ssh/new')
        input('Press Enter once the key has been added... ')
        return True

    # ─── 4. Repositories ─────────────────────────────────────
    def repositories(self):
        ui.step(4, TOTAL_STEPS, 'Repositories')
        self.checkout = self._discover_checkout()

        present = all(os.path.exists(os.path.join(self.checkout, repo, '.git'))
                      for repo in cfg.REPOS)
        if present:
            ui.ok(f'Both repositories already present at {self.checkout}')
            self.mode = ('worktree'
                         if git.is_worktree(os.path.join(self.checkout, 'odoo'))
                         else 'clone')
            ui.info(f'Detected {self.mode} mode')
            if not self._verify_repos():
                return False
            return self._ensure_dev_remotes()

        bases = self._find_base_repos()
        if bases:
            base = bases[0]
            ui.info(f'Found existing Odoo repositories at: {base}')
            if ui.confirm('Create lightweight worktrees from them (fast, saves disk)?'):
                self.mode = 'worktree'
                if not self._create_worktrees(base):
                    return False
                return self._ensure_dev_remotes()

        self.mode = 'clone'
        if not self._clone_repos():
            return False
        return self._ensure_dev_remotes()

    def _discover_checkout(self):
        override = self.args.checkout or os.environ.get('RUNBOT_LOCAL_CHECKOUT')
        if override:
            ui.info(f'Using checkout path: {override}')
            return override
        existing, _ = cfg.load()
        if os.path.exists(cfg.CONFIG_FILE) and existing.get('checkout_path'):
            ui.info(f'Reusing checkout path from config: {existing["checkout_path"]}')
            return existing['checkout_path']
        return cfg.DEFAULT_CHECKOUT

    @staticmethod
    def _find_base_repos():
        """Directories already holding both repos, so worktrees can save GBs."""
        candidates = [
            os.path.join(cfg.HOME, 'odoo', 'repositories', 'odoo'),
            *sorted(glob.glob(os.path.join(cfg.HOME, 'odev', 'worktrees', '*'))),
            os.path.join(cfg.HOME, 'src', 'odoo'),
            os.path.join(cfg.HOME, 'odoo'),
        ]
        found = []
        for candidate in candidates:
            if all(os.path.exists(os.path.join(candidate, repo, '.git'))
                   for repo in cfg.REPOS):
                found.append(candidate)
        return found

    def _verify_repos(self):
        """Present is not the same as usable: a worktree whose parent moved,
        or an interrupted clone, both leave a .git behind."""
        ok = True
        for repo in cfg.REPOS:
            path = os.path.join(self.checkout, repo)
            if git.is_repo(path):
                continue
            ui.err(f'{path} exists but is not a usable git repository.')
            if git.is_worktree(path):
                ui.info('It looks like a worktree whose parent repository moved '
                        'or was deleted.')
            ui.info(f"Remove it and re-run setup: rm -rf '{path}'")
            ok = False
        return ok

    def _create_worktrees(self, base):
        os.makedirs(self.checkout, exist_ok=True)
        for repo in cfg.REPOS:
            target = os.path.join(self.checkout, repo)
            if os.path.exists(os.path.join(target, '.git')):
                ui.ok(f'Worktree {repo} already exists')
                continue

            source = os.path.join(base, repo)
            ref = next((candidate for candidate in
                        ('master', 'main', '18.0', 'saas-18.4', 'HEAD')
                        if git.git(source, 'rev-parse', '--verify', '--quiet',
                                   candidate, timeout=30).ok), None)
            if not ref:
                ui.err(f'No usable base ref found in {source}')
                return False

            # --detach: the server only ever checks out raw commits, and
            # attaching a branch here would lock it out of the user's main clone.
            result = git.git_stream(source, 'worktree', 'add', '--detach', target, ref)
            if not result.ok:
                ui.err(f'Could not create the {repo} worktree')
                return False
            ui.ok(f'Worktree {repo} created at {target} (detached at {ref})')
        return True

    def _clone_repos(self):
        os.makedirs(self.checkout, exist_ok=True)
        for repo in cfg.REPOS:
            target = os.path.join(self.checkout, repo)
            if os.path.exists(os.path.join(target, '.git')):
                ui.ok(f'{repo} already cloned')
                continue
            url = cfg.repo_url(self.protocol, cfg.UPSTREAM, repo)
            ui.info(f'Cloning {cfg.UPSTREAM}/{repo} — several GB, this takes a while...')
            result = proc.stream(['git', 'clone', '--progress', url, target],
                                 env=git.env(), timeout=7200)
            if not result.ok:
                ui.err(f'Clone of {cfg.UPSTREAM}/{repo} failed. Setup cannot continue.')
                return False
            ui.ok(f'{cfg.UPSTREAM}/{repo} cloned')
        return True

    def _ensure_dev_remotes(self):
        """Runbot bundles for dev branches reference commits that live in
        odoo-dev. Without these remotes those commits can never resolve."""
        protocol = self.protocol or 'ssh'
        for repo in cfg.REPOS:
            path = os.path.join(self.checkout, repo)
            if not os.path.exists(os.path.join(path, '.git')):
                continue
            if git.remote_url(path, 'dev'):
                ui.ok(f"{repo} already has a 'dev' remote")
                continue
            url = cfg.repo_url(protocol, cfg.DEV_ORG, repo)
            # Never auto-fetch: commits are fetched individually on demand.
            if git.git(path, 'remote', 'add', 'dev', url, timeout=30).ok:
                ui.ok(f"{repo}: added 'dev' remote ({url})")
            else:
                ui.warn(f"{repo}: could not add the 'dev' remote — dev-branch "
                        'bundles may not resolve')
        return True

    # ─── 5. Odoo system dependencies ─────────────────────────
    def odoo_dependencies(self):
        ui.step(5, TOTAL_STEPS, 'Odoo System Dependencies')
        script = os.path.join(self.checkout, 'odoo', 'setup', 'debinstall.sh')

        if self.manager != 'apt-get':
            ui.warn('debinstall.sh is Debian-only; skipping on this distribution.')
            ui.info('Install Odoo\'s system libraries manually if features misbehave.')
            return 'skip'
        if not os.path.exists(script):
            ui.warn(f'debinstall.sh not found at {script} — skipping.')
            return 'skip'

        # Optional step: never fail the whole run over it. Non-interactively
        # sudo cannot prompt, so skip rather than fail on a password we can't give.
        if not ui.interactive() and not proc.sudo_available_noninteractive():
            ui.warn('Skipping debinstall.sh: non-interactive and sudo needs a password.')
            ui.info('Run it yourself later if Odoo reports missing libraries:')
            ui.detail(f'sudo {script} -q')
            return 'skip'
        if not ui.confirm(f"Run 'sudo {script} -q' to install Odoo's system libraries?"):
            ui.warn('Skipped — some Odoo features may not work.')
            return 'skip'

        if not proc.stream(['sudo', script, '-q'], timeout=3600).ok:
            ui.err('debinstall.sh failed.')
            return False
        ui.ok('Odoo system dependencies installed')
        return True

    # ─── 6. Virtualenv ───────────────────────────────────────
    def virtualenv(self):
        ui.step(6, TOTAL_STEPS, 'Python Virtual Environment')
        python = os.path.join(cfg.VENV_DIR, 'bin', 'python')
        pip = os.path.join(cfg.VENV_DIR, 'bin', 'pip')

        # A directory is not proof of a working venv: a system Python upgrade
        # leaves the interpreter symlink dangling and every pip call then fails.
        if os.path.isdir(cfg.VENV_DIR) and not proc.run([python, '-c', 'import sys'],
                                                        timeout=30).ok:
            ui.warn(f'Virtualenv at {cfg.VENV_DIR} is broken — recreating.')
            shutil.rmtree(cfg.VENV_DIR, ignore_errors=True)

        if not os.path.isdir(cfg.VENV_DIR):
            import sys
            if not proc.stream([sys.executable, '-m', 'venv', cfg.VENV_DIR],
                               timeout=300).ok:
                ui.err('Could not create the virtualenv.')
                return False
            ui.ok('Virtual environment created')
        else:
            ui.ok('Virtual environment already usable')

        requirements = os.path.join(cfg.REPO_ROOT, 'requirements.txt')
        odoo_requirements = os.path.join(self.checkout, 'odoo', 'requirements.txt')

        ui.info('Installing Python packages...')
        for target in (['--upgrade', 'pip'], ['-r', requirements]):
            if not proc.stream([pip, 'install', *target], timeout=1800).ok:
                ui.err(f'pip install {" ".join(target)} failed.')
                return False

        if not os.path.exists(odoo_requirements):
            ui.err(f'{odoo_requirements} not found — is the odoo repo complete?')
            return False
        if not proc.stream([pip, 'install', '-r', odoo_requirements], timeout=3600).ok:
            ui.err("Odoo's Python requirements failed to install — Odoo will not start.")
            return False

        ui.ok('Python packages installed')
        return True

    # ─── 7. Configuration ────────────────────────────────────
    def configuration(self):
        ui.step(7, TOTAL_STEPS, 'Configuration')
        conf = cfg.defaults()
        conf.update({
            'mode': self.mode or 'clone',
            'checkout_path': self.checkout,
            'python': os.path.join(cfg.VENV_DIR, 'bin', 'python'),
            'repo_path': cfg.REPO_ROOT,
            'git_protocol': self.protocol or 'ssh',
            'server_port': self.server_port,
            'odoo_port': self.odoo_port,
            'db_user': self.db_user,
        })
        cfg.save(conf)
        ui.ok(f'Config written to {cfg.CONFIG_FILE}')
        return True

    # ─── 8. Service ──────────────────────────────────────────
    def service(self):
        ui.step(8, TOTAL_STEPS, 'Systemd Service')
        os.makedirs(cfg.LOG_DIR, exist_ok=True)

        # Installs made before the rename run as "runbot-local" and would keep
        # holding the server port, so retire that unit first.
        if systemd.exists('runbot-local'):
            ui.warn("Found a legacy 'runbot-local' service — disabling it.")
            systemd.disable('runbot-local', now=True)
            legacy = os.path.join(cfg.HOME, '.config', 'systemd', 'user',
                                  'runbot-local.service')
            if os.path.exists(legacy):
                os.remove(legacy)
            ui.info(f'The old directory {cfg.HOME}/.runbot-local can be deleted '
                    'once this works.')

        systemd.install_unit(
            os.path.join(cfg.REPO_ROOT, f'{cfg.APP_NAME}.service'),
            cfg.UNIT_FILE,
            {'%REPO%': cfg.REPO_ROOT, '%h': cfg.HOME},
        )
        systemd.daemon_reload()
        systemd.enable(cfg.APP_NAME)
        if not systemd.restart(cfg.APP_NAME).ok:
            ui.err('Could not start the service.')
            return False

        if not systemd.enable_lingering(self.db_user).ok:
            ui.warn('Could not enable lingering — the service stops when you log out.')

        if not self._wait_for_health():
            ui.err(f'Server did not come up on port {self.server_port}.')
            ui.info(f'Check: systemctl --user status {cfg.APP_NAME}')
            log = cfg.server_log()
            if os.path.exists(log):
                with open(log) as fh:
                    ui.detail(''.join(fh.readlines()[-20:]))
            return False

        ui.ok(f'Server responding on http://127.0.0.1:{self.server_port}')
        return True

    def _wait_for_health(self, attempts=8):
        import time
        import urllib.request
        url = f'http://127.0.0.1:{self.server_port}/health'
        for _ in range(attempts):
            time.sleep(1)
            try:
                with urllib.request.urlopen(url, timeout=3):
                    return True
            except Exception:
                continue
        return False

    # ─── 9. Desktop shortcut ─────────────────────────────────
    def desktop(self):
        ui.step(9, TOTAL_STEPS, 'Desktop Shortcut')
        source = os.path.join(cfg.REPO_ROOT, f'{cfg.APP_NAME}.desktop')
        if not os.path.exists(source):
            ui.warn('Desktop file not found — skipping.')
            return 'skip'
        os.makedirs(os.path.dirname(cfg.DESKTOP_FILE), exist_ok=True)
        shutil.copy2(source, cfg.DESKTOP_FILE)
        os.chmod(cfg.DESKTOP_FILE, 0o755)
        ui.ok('Desktop shortcut installed')
        return True

    # ─── 10. Extension + CLI ─────────────────────────────────
    def browser_extension(self):
        ui.step(10, TOTAL_STEPS, 'Browser Extension and CLI')
        try:
            extension.build(cfg.EXTENSION_DIR)
        except (FileNotFoundError, OSError) as exc:
            ui.err(f'Extension build failed: {exc}')
            return False
        ui.ok(f'Extension built at {cfg.EXTENSION_DIR}/{{chrome,firefox}}/')

        if self._link_cli():
            ui.ok(f'CLI available as: {cfg.APP_NAME} (short alias: orl)')
        return True

    def _link_cli(self):
        bin_dir = os.path.join(cfg.HOME, '.local', 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        source = os.path.join(cfg.REPO_ROOT, 'bin', cfg.APP_NAME)
        linked = False
        for name in (cfg.APP_NAME, 'orl'):
            target = os.path.join(bin_dir, name)
            try:
                if os.path.islink(target) or os.path.exists(target):
                    os.remove(target)
                os.symlink(source, target)
                linked = True
            except OSError as exc:
                ui.warn(f'Could not link {target}: {exc}')

        if linked:
            self._ensure_on_path(bin_dir)
        return linked

    @staticmethod
    def _login_shell():
        shell = os.environ.get('SHELL', '')
        if not shell:
            try:
                import pwd
                shell = pwd.getpwuid(os.getuid()).pw_shell
            except Exception:
                shell = ''
        return os.path.basename(shell)

    @classmethod
    def _shell_rc(cls):
        """The rc file the user's login shell actually reads.

        Pointing a zsh user at ~/.profile is useless: zsh never sources it,
        and on a desktop it only appears to work because the graphical session
        happens to read it at login.
        """
        shell = cls._login_shell()
        return {
            'zsh': os.path.join(cfg.HOME, '.zshrc'),
            'bash': os.path.join(cfg.HOME, '.bashrc'),
            'fish': os.path.join(cfg.HOME, '.config', 'fish', 'config.fish'),
        }.get(shell, os.path.join(cfg.HOME, '.profile'))

    @classmethod
    def _ensure_on_path(cls, bin_dir):
        if bin_dir in os.environ.get('PATH', '').split(os.pathsep):
            return True

        rc = cls._shell_rc()
        if cls._login_shell() == 'fish':
            line = f'fish_add_path {bin_dir}'
        else:
            line = f'export PATH="{bin_dir}:$PATH"'

        # An existing, uncommented entry means PATH is already configured and
        # the shell simply has not been reloaded yet.
        if os.path.exists(rc):
            with open(rc) as fh:
                for raw in fh:
                    stripped = raw.strip()
                    if stripped.startswith('#'):
                        continue
                    if bin_dir in stripped or '.local/bin' in stripped:
                        ui.warn(f'{bin_dir} is configured in {rc} but not yet in this '
                                'shell.')
                        ui.info(f'Start a new shell, or run: source {rc}')
                        return True

        ui.warn(f'{bin_dir} is not on your PATH, so "orl" will not be found.')
        if not ui.confirm(f'Add it to {rc}?'):
            ui.info(f'Add it yourself later: echo \'{line}\' >> {rc}')
            return False

        try:
            with open(rc, 'a') as fh:
                fh.write(f'\n# Added by {cfg.APP_NAME} setup\n{line}\n')
        except OSError as exc:
            ui.err(f'Could not write to {rc}: {exc}')
            ui.info(f'Add it yourself: echo \'{line}\' >> {rc}')
            return False

        ui.ok(f'Added {bin_dir} to {rc}')
        ui.info(f'Start a new shell, or run: source {rc}')
        return True


def run(args):
    setup = Setup(args)
    steps = ui.Steps()

    print()
    print(f'{ui.CYAN}=========={ui.NC} {ui.GREEN}{cfg.APP_NAME} — Setup{ui.NC} '
          f'{ui.CYAN}=========={ui.NC}')
    print()
    print('  Sets up everything needed to run Odoo branches locally.')
    print()
    print(f'  {ui.CYAN}*{ui.NC} Some steps need sudo (system packages, PostgreSQL role).')
    print(f'  {ui.CYAN}*{ui.NC} You need GitHub access to {ui.BOLD}odoo/odoo{ui.NC}, '
          f'{ui.BOLD}odoo/enterprise{ui.NC} and {ui.BOLD}odoo-dev{ui.NC}.')
    print()
    if ui.interactive():
        input('Press Enter to begin, or Ctrl+C to abort. ')

    steps.run('System packages', setup.system_packages)
    steps.run('PostgreSQL', setup.postgresql)
    steps.run('GitHub access', setup.github_access)

    # The rest is meaningless without these, so stop rather than produce a
    # cascade of confusing failures.
    if steps.failed():
        steps.summary()
        ui.err('Setup stopped early — the steps above are prerequisites for the rest.')
        ui.info(f'Fix them and re-run: {cfg.APP_NAME} setup')
        return 1

    steps.run('Repositories', setup.repositories)
    if steps.failed():
        steps.summary()
        ui.err('Setup stopped: without both repositories nothing downstream can work.')
        return 1

    steps.run('Odoo system deps', setup.odoo_dependencies)
    steps.run('Python virtualenv', setup.virtualenv)
    steps.run('Configuration', setup.configuration)
    steps.run('Systemd service', setup.service)
    steps.run('Desktop shortcut', setup.desktop)
    steps.run('Browser extension', setup.browser_extension)

    steps.summary()

    if steps.failed():
        ui.err('Setup finished with failures — the tool will not work until they '
               'are resolved.')
        ui.info(f'Run "{cfg.APP_NAME} doctor" for a full diagnosis.')
        return 1

    print(f'{ui.GREEN}Setup complete.{ui.NC}')
    print()
    print(f'  {ui.CYAN}Next steps:{ui.NC}')
    print('   1. Chrome:  chrome://extensions → Developer mode → Load unpacked →')
    print(f'               {cfg.EXTENSION_DIR}/chrome')
    print('      Firefox: open the signed .xpi in releases/')
    print('   2. Open https://runbot.odoo.com and click "Run locally"')
    print(f'      or run a branch directly: {ui.BOLD}orl run <branch>{ui.NC}')
    print()
    print(f'  Diagnose problems any time with: {ui.BOLD}orl doctor{ui.NC}')
    print()
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser('setup', help='install and configure everything')
    parser.add_argument('--checkout', help='where odoo/ and enterprise/ should live')
    parser.add_argument('--odoo-port', type=int, default=cfg.defaults()['odoo_port'],
                        help='port for the Odoo instance')
    parser.add_argument('--server-port', type=int, default=cfg.defaults()['server_port'],
                        help='port for this server (the extension expects 8765)')
    parser.set_defaults(func=run)
    return parser
