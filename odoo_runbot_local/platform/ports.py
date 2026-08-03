"""Port inspection without a hard dependency on lsof."""
import os
import re
import socket

from . import proc


def is_free(port, host='127.0.0.1'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def listeners(port):
    """PIDs listening on a port. Best effort — ss, then lsof, else empty."""
    if proc.which('ss'):
        result = proc.run(['ss', '-ltnpH', f'sport = :{port}'], timeout=10)
        if result.ok:
            return {int(pid) for pid in re.findall(r'pid=(\d+)', result.stdout)}
    if proc.which('lsof'):
        result = proc.run(['lsof', f'-tiTCP:{port}', '-sTCP:LISTEN'], timeout=10)
        if result.ok:
            return {int(pid) for pid in result.stdout.split() if pid.isdigit()}
    return set()


def cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as fh:
            return fh.read().decode('utf-8', 'replace').split('\0')
    except OSError:
        return []


def start_time(pid):
    """Kernel start time, used to detect pid reuse."""
    try:
        with open(f'/proc/{pid}/stat') as fh:
            data = fh.read()
        # comm may contain spaces and parentheses, so parse after the last ')'.
        return int(data[data.rindex(')') + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
