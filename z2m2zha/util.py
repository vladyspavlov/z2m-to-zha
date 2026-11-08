"""Small shared helpers: logging, subprocesses, privileged file access, detaching."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = time.time()
# Anything that looks like a 128-bit key (32+ hex chars) is masked in streamed output.
KEYLIKE = re.compile(r"[0-9a-fA-F]{32,}")


def log(*args):
    print(f"[{time.strftime('%H:%M:%S')} +{time.time() - T0:5.0f}s]", *args, flush=True)


def run(cmd, check=True, input=None, timeout=None):
    """Run a command (argument list) and return its stdout as text."""
    r = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        shown = " ".join(str(c) for c in cmd)[:200]
        raise RuntimeError(f"{shown} -> exit {r.returncode}: {r.stderr[-800:]}")
    return r.stdout


def ok(cmd):
    """True when the command exits 0 (output discarded)."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def have_sudo():
    return os.geteuid() != 0 and shutil.which("sudo") is not None


def read_bytes(path):
    """Read a file. Falls back to passwordless sudo (HA config dirs are often root-only)."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except PermissionError:
        if not have_sudo():
            raise
        return subprocess.run(["sudo", "-n", "cat", path], check=True, capture_output=True).stdout


def read_text(path):
    return read_bytes(path).decode()


def read_json(path):
    return json.loads(read_bytes(path))


def exists(path):
    if os.path.exists(path):
        return True
    return have_sudo() and ok(["sudo", "-n", "test", "-e", path])


def write_bytes(path, data, mode=0o644):
    """Write a file with the given mode, using sudo when the directory is not writable."""
    if isinstance(data, str):
        data = data.encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, mode)
        return
    except PermissionError:
        if not have_sudo():
            raise
    subprocess.run(["sudo", "-n", "sh", "-c", 'umask 077 && cat > "$1" && chmod "$2" "$1"', "sh", path, f"{mode:o}"],
                   input=data, check=True)


def fs(*args):
    """Run a file utility (mv, cp, rm, mkdir, chmod), retrying with sudo on failure."""
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode and have_sudo():
        r = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{' '.join(args)} -> {r.stderr.strip()[-400:]}")


def mask(line):
    return KEYLIKE.sub("<hidden>", line)


def detach(name, state_dir):
    """Re-run the current command in its own session with output in a log file.

    Steps that stop Zigbee2MQTT must survive a dropped SSH session, otherwise the process that
    was going to start Z2M again dies with the shell. Returns only in the detached child (or
    right away with --foreground).
    """
    if os.environ.get("Z2M2ZHA_DETACHED") or "--foreground" in sys.argv:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # run finally blocks (restart Z2M)
        return
    logf = os.path.join(state_dir, f"{name}.log")
    env = dict(os.environ, Z2M2ZHA_DETACHED="1")
    with open(logf, "w") as out:
        p = subprocess.Popen([sys.executable, "-m", "z2m2zha", *sys.argv[1:]], env=env, cwd=REPO_ROOT,
                             stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"started in the background (pid {p.pid}), so an SSH drop cannot interrupt it.\n"
          f"Follow it with:  tail -f {logf}\nIt ends with a line starting with RESULT.")
    sys.exit(0)


def confirm(flag="--yes"):
    if flag not in sys.argv:
        sys.exit(f"refusing: this step writes. Re-run with {flag}")
