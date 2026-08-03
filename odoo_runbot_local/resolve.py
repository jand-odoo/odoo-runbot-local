"""Turn a branch name into a matching pair of odoo/enterprise commits.

This is what lets the tool work when runbot is unavailable. Runbot's batch
pairing is authoritative because it records what was actually built together;
what follows is a good-faith reconstruction, not an equivalent.
"""
from . import config as cfg
from .platform import git


class ResolutionError(Exception):
    def __init__(self, message, searched=()):
        super().__init__(message)
        self.message = message
        self.searched = list(searched)


class Resolution:
    """A commit pair, plus how it was arrived at."""

    def __init__(self, branch, base, commits, strategy, notes=()):
        self.branch = branch
        self.base = base
        self.commits = commits            # {'odoo': sha, 'enterprise': sha}
        self.strategy = strategy          # both | paired | version
        self.notes = list(notes)

    @property
    def exact(self):
        """True when both repos had the branch, so nothing was inferred."""
        return self.strategy == 'both'

    def __repr__(self):
        return (f'<Resolution {self.branch} {self.strategy} '
                f'odoo={self.commits["odoo"][:12]} '
                f'enterprise={self.commits["enterprise"][:12]}>')


def _base_ref(repo, base):
    """A usable ref for the base branch, refreshed from the remote."""
    for remote in ('origin', *[name for name in git.remotes(repo) if name != 'origin']):
        git.git(repo, 'fetch', '--no-tags', remote, base, timeout=600)
        for candidate in (f'{remote}/{base}', 'FETCH_HEAD'):
            if git.git(repo, 'rev-parse', '--verify', '--quiet', candidate,
                       timeout=30).ok:
                return candidate
    return None


def _pair_by_fork_point(conf, source_repo, source_sha, target_repo, base):
    """Pick the target commit contemporaneous with where the branch diverged.

    Using the fork point rather than the branch tip is what keeps a long-lived
    branch matched to the core it was actually developed against, and it stays
    correct across rebases because the merge base moves with the branch.
    """
    notes = []
    source_path = cfg.repo_path(conf, source_repo)
    target_path = cfg.repo_path(conf, target_repo)

    found, detail = git.fetch_commit(source_path, source_sha)
    if not found:
        raise ResolutionError(f'Could not fetch {source_sha[:12]} in {source_repo}: {detail}')

    source_base = _base_ref(source_path, base)
    timestamp = None
    if source_base:
        timestamp = git.fork_point_date(source_path, source_sha, source_base)

    if timestamp is None:
        timestamp = git.commit_date(source_path, source_sha)
        notes.append(f'Could not find where the branch diverged from {base}; '
                     'paired on the branch tip date instead.')
    if timestamp is None:
        raise ResolutionError(f'Could not date {source_sha[:12]} in {source_repo}')

    target_base = _base_ref(target_path, base)
    if not target_base:
        raise ResolutionError(
            f'{target_repo} has no branch "{base}" to pair against. '
            f'Pass --{target_repo}-commit to choose one yourself.')

    target_sha = git.commit_at_date(target_path, target_base, timestamp)
    if not target_sha:
        raise ResolutionError(
            f'No commit on {target_repo} {base} at or before the fork point.')

    return target_sha, notes


def resolve(conf, branch, odoo_commit=None, enterprise_commit=None):
    """Resolve a branch to a commit pair. Raises ResolutionError when it cannot."""
    if odoo_commit and enterprise_commit:
        return Resolution(branch, None,
                          {'odoo': odoo_commit, 'enterprise': enterprise_commit},
                          'both', ['Both commits supplied explicitly.'])

    base = git.base_version(branch)
    # For a version branch the upstream is authoritative; odoo-dev's copies of
    # master and friends are long-abandoned stubs.
    prefer = 'origin' if base == branch else 'dev'

    found = {}
    for repo in cfg.REPOS:
        remote, sha = git.find_branch(cfg.repo_path(conf, repo), branch, prefer=prefer)
        if sha:
            found[repo] = (remote, sha)

    if not found:
        raise ResolutionError(
            f'Branch "{branch}" was not found in either repository.',
            searched=[f'{repo}: {" ".join(git.remotes(cfg.repo_path(conf, repo)))}'
                      for repo in cfg.REPOS])

    # Both repos carry the branch: nothing has to be inferred.
    if len(found) == len(cfg.REPOS):
        return Resolution(branch, base,
                          {repo: sha for repo, (_, sha) in found.items()}, 'both')

    # The branch is itself a version, so take the other repo's tip of it.
    if base == branch:
        commits = {}
        for repo in cfg.REPOS:
            if repo in found:
                commits[repo] = found[repo][1]
                continue
            path = cfg.repo_path(conf, repo)
            ref = _base_ref(path, branch)
            if not ref:
                raise ResolutionError(f'{repo} has no branch "{branch}"')
            commits[repo] = git.git(path, 'rev-parse', ref, timeout=30).stdout
        return Resolution(branch, base, commits, 'version',
                          [f'{branch} is a version branch; used each repository\'s tip.'])

    # Present in one repo only: infer the other from the fork point.
    if not base:
        raise ResolutionError(
            f'Cannot tell which version "{branch}" is based on, so the other '
            f'repository cannot be paired. Branch names normally start with the '
            f'version, e.g. "master-feature-abc". Pass --odoo-commit and '
            f'--enterprise-commit to choose explicitly.')

    source_repo = next(iter(found))
    target_repo = next(repo for repo in cfg.REPOS if repo != source_repo)
    source_sha = found[source_repo][1]

    override = {'odoo': odoo_commit, 'enterprise': enterprise_commit}.get(target_repo)
    if override:
        target_sha, notes = override, ['Target commit supplied explicitly.']
    else:
        target_sha, notes = _pair_by_fork_point(
            conf, source_repo, source_sha, target_repo, base)
        notes.insert(0, f'{target_repo} has no branch "{branch}"; paired to '
                        f'{base} at the fork point.')

    commits = {source_repo: source_sha, target_repo: target_sha}
    return Resolution(branch, base, commits, 'paired', notes)


def search(conf, fragment, limit=25):
    """Remote branches matching a fragment, as (name, repos-containing-it)."""
    matches = {}
    for repo in cfg.REPOS:
        for name, (remote, _sha) in git.search_branches(
                cfg.repo_path(conf, repo), fragment, limit):
            matches.setdefault(name, {})[repo] = remote
    return sorted(matches.items())[:limit]
