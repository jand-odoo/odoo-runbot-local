"""Subprocess helpers shared by every platform module."""
import subprocess


class Result:
    __slots__ = ('returncode', 'stdout', 'stderr')

    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.returncode == 0

    def __bool__(self):
        return self.ok

    @property
    def last_error_line(self):
        lines = [line for line in self.stderr.splitlines() if line.strip()]
        return lines[-1] if lines else ''


def run(cmd, timeout=60, cwd=None, env=None, stdin=None):
    """Run a command, capturing output. Never raises on failure or timeout."""
    try:
        completed = subprocess.run(
            cmd, cwd=cwd, env=env, input=stdin,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(124, '', f'timed out after {timeout}s: {" ".join(cmd)}')
    except OSError as exc:
        return Result(127, '', str(exc))
    return Result(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def stream(cmd, cwd=None, env=None, timeout=None):
    """Run a command with output going straight to the terminal."""
    try:
        completed = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(124, '', f'timed out after {timeout}s')
    except OSError as exc:
        return Result(127, '', str(exc))
    return Result(completed.returncode)


def which(binary):
    import shutil
    return shutil.which(binary)


def sudo_available_noninteractive():
    """True when sudo can run without prompting for a password."""
    return run(['sudo', '-n', 'true'], timeout=10).ok
