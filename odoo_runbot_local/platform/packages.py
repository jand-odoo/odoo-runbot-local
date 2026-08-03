"""Multi-distro package detection and installation."""
from . import proc

MANAGERS = ('apt-get', 'dnf', 'pacman', 'zypper')

# Generic dependency -> per-manager package name. An empty string means the
# dependency is already satisfied by another package on that distribution.
PACKAGES = {
    'postgresql': {
        'apt-get': 'postgresql', 'dnf': 'postgresql-server',
        'pacman': 'postgresql', 'zypper': 'postgresql-server',
    },
    'postgresql-client': {
        'apt-get': 'postgresql-client', 'dnf': 'postgresql',
        'pacman': 'postgresql-libs', 'zypper': 'postgresql',
    },
    'libpq-dev': {
        'apt-get': 'libpq-dev', 'dnf': 'libpq-devel',
        'pacman': 'postgresql-libs', 'zypper': 'postgresql-devel',
    },
    'python3': {
        'apt-get': 'python3', 'dnf': 'python3',
        'pacman': 'python', 'zypper': 'python3',
    },
    'python3-venv': {
        'apt-get': 'python3-venv', 'dnf': '',
        'pacman': '', 'zypper': '',
    },
    'python3-pip': {
        'apt-get': 'python3-pip', 'dnf': 'python3-pip',
        'pacman': 'python-pip', 'zypper': 'python3-pip',
    },
    'git': {
        'apt-get': 'git', 'dnf': 'git', 'pacman': 'git', 'zypper': 'git',
    },
    'curl': {
        'apt-get': 'curl', 'dnf': 'curl', 'pacman': 'curl', 'zypper': 'curl',
    },
    'iproute2': {
        'apt-get': 'iproute2', 'dnf': 'iproute',
        'pacman': 'iproute2', 'zypper': 'iproute2',
    },
}

REQUIRED = tuple(PACKAGES)

# The command each dependency puts on PATH, for distros we cannot query.
# Empty means it ships no binary (headers, or a stdlib module).
BINARIES = {
    'postgresql-client': 'psql',
    'python3': 'python3',
    'python3-pip': 'pip3',
    'git': 'git',
    'curl': 'curl',
    'iproute2': 'ss',
}


def detect_manager():
    for manager in MANAGERS:
        if proc.which(manager):
            return manager
    return None


def package_for(manager, dependency):
    return PACKAGES.get(dependency, {}).get(manager, '')


def is_installed(manager, package):
    if not package:
        return True
    if manager == 'apt-get':
        return proc.run(['dpkg', '-s', package], timeout=20).ok
    if manager in ('dnf', 'zypper'):
        return proc.run(['rpm', '-q', package], timeout=20).ok
    if manager == 'pacman':
        return proc.run(['pacman', '-Q', package], timeout=20).ok
    return False


def is_present(manager, dependency, postgres_ready=None):
    """A dependency counts as present if its binary is on PATH, or the package
    manager reports it installed.

    Probing only the binary would reinstall packages that ship no command
    (libpq-dev) or install outside PATH (the postgresql server on Debian).
    """
    binary = BINARIES.get(dependency)
    if binary and proc.which(binary):
        return True
    if dependency == 'postgresql' and postgres_ready:
        return True
    return is_installed(manager, package_for(manager, dependency))


def missing(manager, postgres_ready=None):
    """Package names to install, deduplicated and order-preserved."""
    names = []
    for dependency in REQUIRED:
        if is_present(manager, dependency, postgres_ready):
            continue
        package = package_for(manager, dependency)
        if package and package not in names:
            names.append(package)
    return names


def missing_binaries():
    """For an unknown package manager: which required commands are absent."""
    return [binary for binary in BINARIES.values() if not proc.which(binary)]


def install(manager, names):
    if not names:
        return proc.Result(0)
    commands = {
        'apt-get': [['sudo', 'apt-get', 'update'],
                    ['sudo', 'apt-get', 'install', '-y', '--no-install-recommends', *names]],
        'dnf': [['sudo', 'dnf', 'install', '-y', *names]],
        'pacman': [['sudo', 'pacman', '-S', '--needed', '--noconfirm', *names]],
        'zypper': [['sudo', 'zypper', '--non-interactive', 'install', *names]],
    }
    for command in commands.get(manager, []):
        result = proc.stream(command, timeout=1800)
        if not result.ok:
            return result
    return proc.Result(0)
