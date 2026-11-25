"""The pool on macOS: see the package's notes for what each name is.

Nothing here is particular to macOS but the paths and how SketchUp is
started, so Linux uses it too: there is no SketchUp for Linux to start, but
the package loads there and most of the tests run there.

SketchUp is started with `open`, as the Finder would start it, on its model.
Run directly (the program inside the bundle, with the model as its
argument) it opens the model but loads no extensions, so nothing ever
listens on its port. What was measured, with SketchUp 2026 (26.2.242) on
macOS 27.2, 2026-11-25:

* `open -n -F -a <bundle> --env NAME=value <model>` works: the extension
  listens 3 to 4 s later, and two such instances run side by side.
* `-n` starts a new SketchUp though one is running.
* `-F` is needed: without it, a SketchUp started after another was killed
  brings back that one's window too, and its active model is the dead
  one's.
* `-g` (start in the background) can't be used: the extension never
  listens. So each start comes to the front.
* A SketchUp with an unsaved change doesn't quit when asked (it stops at
  its save prompt), and one stopped at a dialog ignores the request too:
  hence kill's wait and SIGKILL.

`open` doesn't say which process it started. It is found by what only it
has: it runs SketchUp's program, with this instance's port in its
environment (see _instances), which `ps` shows for the user's own
processes. It belongs to launchd, not to the MCP server: it outlives the
session that started it, and leaves no zombie behind when it ends.
"""

import calendar
import fcntl
import os
import re
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

# How long SketchUp gets to quit when asked, before it's killed. It ignores
# the request when it has an unsaved change, or is stopped at a dialog.
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

# What a pool instance's SketchUp is told through its environment. `open`
# passes its own environment on, so any of these set for this process is
# taken out of it first.
_OURS = ("SKETCHUP_MCP_PORT", "SKETCHUP_MCP_BIND", "SHELLIFY_SRC", "SKETCHUP_MCP_POOL_FILE")

# How long the SketchUp that `open` started may take to show among the
# processes. It is there by the time `open` returns, as a rule.
FIND_WAIT = 20.0


def _bundle(exe):
    """The app bundle that the program exe is in."""
    for folder in Path(exe).parents:
        if folder.suffix == ".app":
            return folder
    raise PoolError(
        f"{exe} isn't inside an app bundle. SKETCHUP_EXE names the program in it, "
        "as in /Applications/SketchUp 2026/SketchUp.app/Contents/MacOS/SketchUp"
    )


def _instances(exe, port):
    """The pids of the SketchUps (the program exe) started for port: the
    processes whose command line starts with exe (or with an interpreter
    and then exe, for a script) and that have SKETCHUP_MCP_PORT=<port> in
    their environment. One, as a rule; a process SketchUp itself started
    inherits its environment, so of a parent and its child only the parent
    is given.

    ps gives a process's environment (its `e`) only on the end of its
    command line, so the command line is read without it too, to tell the
    two apart. It gives the environment of the user's own processes only.
    """
    paths = (str(exe), os.path.realpath(exe))

    def runs_exe(command):
        return command.startswith(paths) or command.partition(" ")[2].startswith(paths)

    ours = re.compile(rf"(^|\s)SKETCHUP_MCP_PORT={int(port)}(\s|$)")
    commands = {}
    for line in _ps("-axww", "-o", "pid=,ppid=,args=").splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and runs_exe(parts[2]):
            commands[int(parts[0])] = (int(parts[1]), parts[2].rstrip())
    found = []
    for pid, (_, command) in commands.items():
        # One process at a time: a value in its environment may have a line
        # break in it.
        text = _ps("eww", "-o", "command=", "-p", str(pid)).strip()
        if text.startswith(command) and ours.search(text[len(command) :]):
            found.append(pid)
    return [pid for pid in found if commands[pid][0] not in found]


def start(r, src, exe, template, pool_dir, pool_file):
    """Starts SketchUp (the program exe, in its app bundle) for instance r
    on a new copy of template, so that it outlives the process that started
    it. Returns (pid, start time). pool_file is where the extension reads
    its lease."""
    if sys.platform != "darwin":
        raise PoolError("There is no SketchUp to start on this system: the pool starts it on Windows and macOS")
    bundle = _bundle(exe)
    # A SketchUp started for this port that the pool has no record of, as
    # when the process that started it ended before writing it down.
    for pid in _instances(exe, r["port"]):
        kill(pid)
    # Any still there (one just killed may take a moment to go) isn't the
    # one about to be started.
    before = set(_instances(exe, r["port"]))

    model = Path(r["model"])
    model.parent.mkdir(parents=True, exist_ok=True)
    # What an earlier SketchUp may have left beside the model, so this one
    # doesn't stop to offer it back. None was seen after a SketchUp was
    # killed within a minute of a change; one killed after its autosave
    # interval wasn't tried, and these are the names SketchUp has used.
    for leftover in (model.with_name(f"AutoSave_{model.name}"), model.with_suffix(".skb")):
        leftover.unlink(missing_ok=True)
    shutil.copyfile(template, model)

    tell = {"SKETCHUP_MCP_PORT": str(r["port"]), "SKETCHUP_MCP_POOL_FILE": str(pool_file)}
    if src:
        tell["SHELLIFY_SRC"] = src
    command = ["open", "-n", "-F", "-a", str(bundle)]
    for name, value in tell.items():
        command += ["--env", f"{name}={value}"]
    try:
        done = subprocess.run(
            [*command, str(model)],
            env={name: value for name, value in os.environ.items() if name not in _OURS},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PoolError(f"Starting SketchUp failed: {type(e).__name__}: {e}")
    if done.returncode != 0:
        raise PoolError(f"Starting SketchUp failed: open said: {(done.stderr or done.stdout).strip()}")

    deadline = time.time() + FIND_WAIT
    while True:
        for pid in _instances(exe, r["port"]):
            started = process_started(pid)
            if pid not in before and started is not None:
                return pid, started
        if time.time() > deadline:
            raise PoolError(
                f"SketchUp was opened for port {r['port']}, but no process of {exe} with that port in its "
                f"environment showed within {FIND_WAIT:g} s: it exited straight away, or ps doesn't show it. "
                f"{START_LOG_HINT}"
            )
        time.sleep(0.2)
