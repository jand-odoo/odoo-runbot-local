"""Git operations, including branch resolution and commit pairing."""
import os
import re

from . import proc
from .. import config as cfg

# Odoo branches are named <base>-<feature>-<initials>, e.g.
# "master-l10n_cn-tax_report-jand" or "saas-18.4-fix-abc".
BASE_VERSION_RE = re.compile(r'^(master|saas-\d+\.\d+|\d+\.\d+)(?:-|$)')

SSH_PROBE_OPTS = [
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=10',
]


def env():
    """Git environment that fails fast instead of hanging on a credential prompt.

    Uses this tool's own deploy key when one has been set up, so git
    operations never depend on the user's personal ssh-agent being reachable
    (it commonly isn't, from a lingering systemd user service).
    """
    environment = dict(os.environ)
    environment['GIT_TERMINAL_PROMPT'] = '0'
    if os.path.exists(cfg.SSH_KEY):
        environment['GIT_SSH_COMMAND'] = (
            f'ssh -i {cfg.SSH_KEY} -o IdentitiesOnly=yes '
            '-o BatchMode=yes -o StrictHostKeyChecking=accept-new'
        )
    else:
        environment.setdefault(
            'GIT_SSH_COMMAND',
            'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new',
        )
    return environment


def git(repo, *args, timeout=600):
    return proc.run(['git', *args], cwd=repo, env=env(), timeout=timeout)


def git_stream(repo, *args, timeout=3600):
    return proc.stream(['git', *args], cwd=repo, env=env(), timeout=timeout)


# ─── Connectivity ────────────────────────────────────────────

def ssh_works(key=None):
    """`ssh -T git@github.com` exits 1 even on success, so match the banner.

    The output must be captured before matching — piping into grep would make a
    shell's pipefail report failure for a perfectly good key.
    """
    opts = list(SSH_PROBE_OPTS)
    if key:
        opts += ['-i', key, '-o', 'IdentitiesOnly=yes']
    result = proc.run(['ssh', *opts, '-T', 'git@github.com'], timeout=30)
    return 'successfully authenticated' in (result.stdout + result.stderr)


def https_works():
    if proc.which('gh') and proc.run(['gh', 'auth', 'status'], timeout=20).ok:
        return True
    result = proc.run(['git', 'credential', 'fill'], timeout=20,
                      stdin='protocol=https\nhost=github.com\n\n')
    return 'password=' in result.stdout


def reachable(url, timeout=60):
    return proc.run(['git', 'ls-remote', '--exit-code', url, 'HEAD'],
                    env=env(), timeout=timeout).ok


# ─── Repository state ────────────────────────────────────────

def is_repo(path):
    return git(path, 'rev-parse', '--git-dir', timeout=30).ok


def is_worktree(path):
    # A worktree stores .git as a file pointing at the parent repository.
    return os.path.isfile(os.path.join(path, '.git'))


def remotes(repo):
    result = git(repo, 'remote', timeout=30)
    if not result.ok or not result.stdout:
        return []
    names = [name.strip() for name in result.stdout.splitlines() if name.strip()]
    # origin first, then dev: feature branches live in odoo-dev.
    names.sort(key=lambda name: (name != 'origin', name != 'dev', name))
    return names


def remote_url(repo, remote):
    result = git(repo, 'remote', 'get-url', remote, timeout=30)
    return result.stdout if result.ok else ''


def has_commit(repo, commit):
    return git(repo, 'cat-file', '-e', f'{commit}^{{commit}}', timeout=30).ok


def fetch_commit(repo, commit):
    """Make a commit available locally. Returns (ok, detail)."""
    if has_commit(repo, commit):
        return True, ''

    errors = []
    for remote in remotes(repo):
        # Fetching a bare sha needs no refspec: objects stay reachable through
        # FETCH_HEAD, so nothing is written into the shared ref namespace.
        # No fallback to a refspec-less `fetch <remote>` here — GitHub reliably
        # serves a reachable sha directly, and that fallback means "every
        # branch on this remote", which for odoo-dev is thousands of branches
        # and turns a missing commit into a multi-minute download for nothing.
        result = git(repo, 'fetch', '--no-tags', remote, commit)
        if result.ok and has_commit(repo, commit):
            return True, ''
        if result.stderr:
            errors.append(f'{remote}: {result.error_summary}')

    if git(repo, 'rev-parse', '--is-shallow-repository', timeout=30).stdout == 'true':
        git(repo, 'fetch', '--unshallow')
        if has_commit(repo, commit):
            return True, ''

    return False, '; '.join(errors[:3]) or 'commit not found on any remote'


def checkout_commit(repo, commit):
    result = git(repo, 'checkout', '--force', '--detach', commit)
    return result.ok, result.stderr


# ─── Branch resolution ───────────────────────────────────────

def base_version(branch):
    """The version a feature branch forked from, parsed from its name."""
    match = BASE_VERSION_RE.match(branch)
    return match.group(1) if match else None


def find_branch(repo, branch, prefer='dev'):
    """Locate a branch on the remotes. Returns (remote, sha) or (None, None).

    Which remote wins matters. Feature branches live in odoo-dev, so `dev` is
    searched first by default. Version branches are the opposite: odoo-dev's
    `master` is an abandoned 2014 tombstone commit ("Please use the right
    repo"), so callers must pass prefer='origin' for those or the checkout
    silently lands a decade in the past.
    """
    ordered = sorted(remotes(repo), key=lambda name: name != prefer)
    for remote in ordered:
        result = git(repo, 'ls-remote', '--heads', remote, f'refs/heads/{branch}',
                     timeout=60)
        if result.ok and result.stdout:
            sha = result.stdout.split()[0]
            return remote, sha
    return None, None


def search_branches(repo, fragment, limit=25):
    """Remote branches whose name contains a fragment, newest-looking first."""
    found = {}
    for remote in remotes(repo):
        result = git(repo, 'ls-remote', '--heads', remote, f'*{fragment}*', timeout=60)
        if not result.ok:
            continue
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            name = parts[1].removeprefix('refs/heads/')
            found.setdefault(name, (remote, parts[0]))
    return sorted(found.items())[:limit]


def commit_date(repo, commit):
    """Committer date as a unix timestamp, or None."""
    result = git(repo, 'show', '-s', '--format=%ct', commit, timeout=60)
    if not result.ok or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.split()[0])
    except ValueError:
        return None


def fork_point_date(repo, branch_sha, base_ref):
    """When the branch diverged from its base.

    Pairing on this rather than the branch tip is what keeps a long-lived branch
    matched to the core it was actually developed against, and it stays correct
    across rebases because the merge base moves with the branch.
    """
    result = git(repo, 'merge-base', branch_sha, base_ref, timeout=120)
    if not result.ok or not result.stdout:
        return None
    return commit_date(repo, result.stdout.split()[0])


def commit_at_date(repo, ref, timestamp):
    """The newest commit on ref at or before a timestamp."""
    result = git(repo, 'rev-list', '-1', f'--before={timestamp}', ref, timeout=120)
    if not result.ok or not result.stdout:
        return None
    return result.stdout.split()[0]
