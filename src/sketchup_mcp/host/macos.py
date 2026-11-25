"""The pool on macOS: see the package's notes for what each name is.

Nothing here is particular to macOS but the paths, so Linux uses it too:
there is no SketchUp for Linux to start, but the package loads there and
the tests run there.

SketchUp is started by running the program inside its app bundle, not with
`open`: the environment set here reaches it as it is, and its pid is known
at once, which `open` doesn't give. (`open -n --env NAME=value` passes the
environment through too; tried with a stand-in app on macOS 26.) It is
started through a shell that puts it in the background and exits, so it
belongs to no MCP server: it outlives the session that started it, and when
it ends it leaves no zombie behind for the pool to mistake for a running
SketchUp.

Not yet tried against a real SketchUp on a Mac.
"""

import calendar
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import PoolError

if sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support"
else:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")

# The program inside the app bundle. SketchUp loads no extensions at its
# welcome screen, so it's always started on a model.
SKETCHUP_EXE = "/Applications/SketchUp 2026/SketchUp.app/Contents/MacOS/SketchUp"

START_LOG_HINT = "If it crashed, macOS keeps a report in ~/Library/Logs/DiagnosticReports."

# How long SketchUp gets to quit when asked, before it's killed. It
# sometimes ignores the request.
QUIT_WAIT = 5.0
KILLED_WITH = f"being asked to quit and then, {QUIT_WAIT:g} s later, SIGKILL"


# --- Processes ---------------------------------------------------------------


def _ps(*args):
    """ps's output, in the C locale and in UTC, so that a start time reads
    the same whatever the user's language and time zone are, or become."""
    done = subprocess.run(
        ["ps", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    )
    return done.stdout


def process_started(pid):
    """When a running process started, in seconds since the epoch, or None
    if it isn't running. A pid with the start time recorded for it is the
    same process; pids are reused. A process that has ended but whose parent
    hasn't collected it (a zombie) isn't running."""
    if not pid:
        return None
    if os.path.isdir("/proc/self"):
        return _proc_started(int(pid))
    state, _, started = _ps("-o", "stat=,lstart=", "-p", str(int(pid))).strip().partition(" ")
    if not started or state.startswith("Z"):
        return None
    try:
        # As in "Wed Nov 25 07:42:21 2026".
        return float(calendar.timegm(time.strptime(started.strip(), "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return None


def _proc_started(pid):
    """process_started on Linux, from /proc. ps works its start times out
    from the uptime there, and they can come out a second apart from one
    call to the next; these don't."""
    try:
        # After the program's name, which is in brackets and may have spaces:
        # the state, then 18 more fields, then the start in clock ticks
        # since boot.
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        if fields[0] == "Z":
            return None
        ticks = int(fields[19])
        boot = next(
            int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")
        )
        return round(boot + ticks / os.sysconf("SC_CLK_TCK"), 3)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def processes():
    """{pid: (parent pid, program name)} for every process."""
    found = {}
    for line in _ps("-axo", "pid=,ppid=,comm=").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            # macOS gives the program's whole path.
            found[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
    return found


def image_path(pid):
    """The full path of a running process's program, or None."""
    try:
        return os.readlink(f"/proc/{int(pid)}/exe")
    except OSError:
        pass
    path = _ps("-o", "comm=", "-p", str(int(pid))).strip()
    return path or None


# Programs that start Python for something else: the process that started
# the MCP server is the first ancestor that isn't one of them.
PYTHON_LAUNCHERS = frozenset({"python", "python3", "uv", "uvx"} | {f"python3.{minor}" for minor in range(8, 40)})

# The same for the sketchup-launcher CLI, which is a Python script here.
CLI_WRAPPERS = PYTHON_LAUNCHERS | {"sketchup-launcher"}


def is_desktop_app(path):
    """Whether the program at path is the Claude desktop app itself (or a
    helper inside its bundle), and not the Claude Code it runs for a
    session, which is a claude.app of its own in a claude-code folder.

    From one Mac (Claude Code 2.1.330 in the desktop app, 2026-11-25): a
    session's MCP server is started by
    ~/Library/Application Support/Claude/claude-code/<version>/<id>/claude.app/Contents/MacOS/claude,
    itself started by /Applications/Claude.app/Contents/Helpers/disclaimer.
    How the app starts a server from its own config wasn't looked at.
    """
    path = (path or "").lower()
    return "/claude.app/contents/" in path and "/claude-code/" not in path


def kill(pid):
    """Asks a SketchUp to quit, and kills it if it's still there QUIT_WAIT
    seconds later. Its model is a throwaway copy."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + QUIT_WAIT
    while time.time() < deadline:
        if process_started(pid) is None:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# --- The pool's lock and folder ----------------------------------------------


def lock(f):
    """Takes the pool's lock on its open lock file, or raises OSError if
    another holds it. flock, which two handles in one process contend for
    too, as two of the server's threads do."""
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(f):
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def make_shared_dir(folder):
    folder.mkdir(parents=True, exist_ok=True)


# --- Starting SketchUp -------------------------------------------------------

# What a pool instance's SketchUp is told through its environment. Any of
# these set for this process is not passed on.
_OURS = ("SKETCHUP_MCP_PORT", "SKETCHUP_MCP_BIND", "SHELLIFY_SRC", "SKETCHUP_MCP_POOL_FILE")

# Runs "$0" "$@" in the background, cut off from this process, and prints
# its pid.
_DETACH = '"$0" "$@" </dev/null >/dev/null 2>&1 & echo $!'


def start(r, src, exe, template, pool_dir, pool_file):
    """Starts SketchUp (exe) for instance r on a new copy of template, so
    that it outlives the process that started it. Returns (pid, start time).
    pool_file is where the extension reads its lease."""
    model = Path(r["model"])
    model.parent.mkdir(parents=True, exist_ok=True)
    # What an earlier SketchUp left beside the model, so this one doesn't
    # stop to offer it back. Where SketchUp 2026 keeps these on macOS hasn't
    # been checked; these are the names it has used beside a model.
    for leftover in (model.with_name(f"AutoSave_{model.name}"), model.with_suffix(".skb")):
        leftover.unlink(missing_ok=True)
    shutil.copyfile(template, model)

    env = {name: value for name, value in os.environ.items() if name not in _OURS}
    env["SKETCHUP_MCP_PORT"] = str(r["port"])
    # Where the extension reads its lease, for its badge in the viewport.
    env["SKETCHUP_MCP_POOL_FILE"] = str(pool_file)
    if src:
        env["SHELLIFY_SRC"] = src
    try:
        done = subprocess.run(
            ["/bin/sh", "-c", _DETACH, str(exe), str(model)],
            env=env,
            cwd=str(Path(exe).parent),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=30,
        )
        pid = int(done.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise PoolError(f"Starting SketchUp failed: {type(e).__name__}: {e}")
    # Long enough for a program that can't be run at all to have gone.
    time.sleep(0.3)
    started = process_started(pid)
    if started is None:
        raise PoolError("SketchUp exited straight away")
    return pid, started
