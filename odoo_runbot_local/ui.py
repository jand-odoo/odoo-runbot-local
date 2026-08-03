"""Terminal output and prompts."""
import os
import sys

_COLOUR = sys.stdout.isatty() and os.environ.get('NO_COLOR') is None

GREEN = '\033[0;32m' if _COLOUR else ''
YELLOW = '\033[1;33m' if _COLOUR else ''
RED = '\033[0;31m' if _COLOUR else ''
CYAN = '\033[0;36m' if _COLOUR else ''
DIM = '\033[2m' if _COLOUR else ''
BOLD = '\033[1m' if _COLOUR else ''
NC = '\033[0m' if _COLOUR else ''

# Set by the --yes flag: take the default for every prompt without reading stdin.
ASSUME_YES = False


def info(message):
    print(f'{CYAN}[INFO]{NC} {message}')


def ok(message):
    print(f'{GREEN}[OK]{NC} {message}')


def warn(message):
    print(f'{YELLOW}[WARN]{NC} {message}', file=sys.stderr)


def err(message):
    print(f'{RED}[ERROR]{NC} {message}', file=sys.stderr)


def detail(message, indent='    '):
    """Secondary output: command excerpts, log tails, hints."""
    for line in str(message).rstrip().splitlines():
        print(f'{DIM}{indent}{line}{NC}')


def heading(message):
    print()
    print(f'{BOLD}{message}{NC}')


def step(number, total, title):
    print()
    print(f'{CYAN}========== Step {number}/{total} — {title} =========={NC}')


def interactive():
    return not ASSUME_YES and sys.stdin.isatty()


def confirm(message, default=True):
    if not interactive():
        return default
    suffix = '[Y/n]' if default else '[y/N]'
    try:
        answer = input(f'{message} {suffix} ').strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer.startswith('y')


def ask(message, default=''):
    if not interactive():
        return default
    try:
        answer = input(f'{message} [{default}] ' if default else f'{message} ').strip()
    except EOFError:
        return default
    return answer or default


def choose(message, options, allow_cancel=True):
    """Numbered picker. Returns the chosen item, or None if cancelled."""
    if not options:
        return None
    if len(options) == 1:
        return options[0]
    if not interactive():
        return None

    print()
    print(message)
    for index, option in enumerate(options, 1):
        print(f'  {BOLD}{index}{NC}) {option}')
    if allow_cancel:
        print(f'  {BOLD}0{NC}) cancel')

    while True:
        try:
            raw = input('Choice: ').strip()
        except EOFError:
            return None
        if not raw:
            continue
        if raw == '0' and allow_cancel:
            return None
        try:
            index = int(raw)
        except ValueError:
            continue
        if 1 <= index <= len(options):
            return options[index - 1]


class Steps:
    """Records the real outcome of each step so a summary cannot claim success."""

    OK, FAIL, SKIP = 'ok', 'fail', 'skip'

    def __init__(self):
        self.results = []

    def record(self, name, state):
        self.results.append((name, state))

    def run(self, name, func, *args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except KeyboardInterrupt:
            raise
        except Exception as exc:                      # noqa: BLE001 - report, never abort
            err(f'{name} failed: {exc}')
            self.record(name, self.FAIL)
            return False
        state = self.SKIP if result == 'skip' else (self.OK if result else self.FAIL)
        self.record(name, state)
        if state == self.FAIL:
            warn(f"Step '{name}' failed. Fix the cause above and re-run — setup is idempotent.")
        return state != self.FAIL

    def failed(self):
        return any(state == self.FAIL for _, state in self.results)

    def summary(self):
        print()
        print(f'{BOLD}============== Summary =============={NC}')
        marks = {self.OK: f'{GREEN}✓{NC}', self.FAIL: f'{RED}✗{NC}', self.SKIP: f'{YELLOW}⊘{NC}'}
        for name, state in self.results:
            print(f'  {marks[state]} {name}')
        print()
