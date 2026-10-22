"""A pool of SketchUp instances, leased out to MCP servers and CI.

Several Claude sessions on one machine can each drive a SketchUp of their
own. The pool has a fixed set of instances, each on its own MCP port
(SKETCHUP_MCP_PORT, which the extension reads) with its own copy of a
template model and its own source folder (SHELLIFY_SRC, which Shellify's dev
loader reads). A session leases one, its tool calls go to that port, and when
it releases it the instance keeps running for the next one.

There is no daemon. The state is a JSON file, guarded by a lock file, that
every MCP server and the sketchup-launcher CLI read and write:

    %LOCALAPPDATA%\\sketchup-mcp\\pool.json

A lease belongs to a holder process (an MCP server, or the shell that ran the
CLI), recorded by pid and start time so a reused pid isn't mistaken for it,
and a holder has one SketchUp at most. A lease whose holder has gone is freed
by the next lease, release or status. The extension in a pool instance reads
pool.json too, and shows a badge in the viewport while it's leased.

Nothing is stopped for being idle. An instance is started, or restarted, only
when a lease needs it: its SketchUp has died, never ran, or was started with
another SHELLIFY_SRC. The user's own SketchUp, on 9876, is never touched.

The Claude desktop app is a packaged (MSIX) app, so the processes it starts,
MCP servers included, see %LOCALAPPDATA% virtualized: a folder they create
there goes to the package's private copy (in Packages, under LocalCache),
which nothing outside the package sees, though writes into a folder that
already exists outside go through (checked 2026-10-22). So the pool's folder
is created from outside the package the first time (see _ensure_pool_dir),
and SketchUp is started, its model copied and its autosave deleted by a
scheduled task, outside the package too (see _launch and spawn).

Windows only, like SketchUp 2026.
"""

import argparse
import ctypes
import json
import msvcrt
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

POOL_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "sketchup-mcp"
POOL_FILE = POOL_DIR / "pool.json"
LOCK_FILE = POOL_DIR / "pool.lock"
MODELS_DIR = POOL_DIR / "models"

# The pool's instances. 9876 is the default SketchUp's, started by the
# "Start SketchUp" scheduled task for the user; it isn't in the pool, and MCP
# sessions don't use it.
PORTS = [9877, 9878, 9879, 9880]
HOST = "127.0.0.1"

SKETCHUP_EXE = Path(os.environ.get(
    "SKETCHUP_EXE", r"C:\Program Files\SketchUp\SketchUp 2026\SketchUp\SketchUp.exe"))

# Copied for each instance as it starts. An empty model, so a fresh instance
# has nothing in it but what its session builds.
TEMPLATE = Path(os.environ.get(
    "SKETCHUP_POOL_TEMPLATE", str(Path.home() / "Desktop" / "shellify scratch.skp")))

# SketchUp 2026 autosaves to this folder, keyed by the model's file name
# only, and after a crash asks whether to open the "recovered version"
# before its extensions load, so nothing answers on the port. The model
# copy's file there is deleted before every start.
AUTOSAVE_DIR = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "SketchUp" / "SketchUp 2026" / "SketchUp" / "working")

# How long a start may take before the lease gives up. SketchUp listens about
# 10-15 s after launch when it's alone; four starting at once take longer.
START_TIMEOUT = float(os.environ.get("SKETCHUP_POOL_START_TIMEOUT", "180"))
LOCK_TIMEOUT = 30.0


class PoolError(Exception):
    """A lease or release that can't be done, with why."""


# --- Processes ---------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def process_started(pid):
    """When a running process started, in seconds since the epoch, or None
    if it isn't running. A pid with the start time recorded for it is the
    same process; Windows reuses pids."""
    if not pid:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        # FILETIME counts 100 ns ticks from 1601.
        return round(created / 1e7 - 11644473600, 3)
    finally:
        _kernel32.CloseHandle(handle)


def is_running(pid, started):
    """Whether the process recorded as pid, started at started, still runs."""
    now = process_started(pid)
    return now is not None and started is not None and abs(now - started) < 0.01


def _processes():
    """{pid: (parent pid, exe name)} for every process."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {}
    found = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return found


def holder(pid=None):
    """The holder a lease is recorded for: (pid, start time)."""
    pid = pid or os.getpid()
    started = process_started(pid)
    if started is None:
        raise PoolError(f"Process {pid} isn't running")
    return (pid, started)


def calling_shell():
    """The process that ran the CLI, for its leases to belong to.

    A pip console script on Windows is an .exe that runs Python as its
    child, so Python's parent is that .exe, which exits with it. Go past it,
    and past a py.exe or python.exe that ran `python -m`.
    """
    table = _processes()
    pid = os.getpid()
    skip = {"sketchup-launcher.exe", "python.exe", "pythonw.exe", "py.exe"}
    while True:
        parent, _ = table.get(pid, (0, ""))
        if not parent or parent not in table:
            return pid
        pid = parent
        if table[pid][1].lower() not in skip:
            return pid


_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_userenv = ctypes.WinDLL("userenv", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
_userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002


def user_environment():
    """The user's own environment, as Explorer gives a program started from
    the Start menu: the system's and the user's variables, from the registry.
    Not this process's: an MCP client may start its server with only a few
    variables (the Python SDK's stdio client passes about a dozen), and
    SketchUp started with those hangs before showing its window."""
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE,
                                      ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    block = ctypes.c_void_p()
    try:
        if not _userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise ctypes.WinError(ctypes.get_last_error())
        env = {}
        address = block.value
        while True:
            entry = ctypes.wstring_at(address)
            if not entry:
                break
            address += (len(entry) + 1) * ctypes.sizeof(ctypes.c_wchar)
            name, sep, value = entry.partition("=")
            # Entries such as "=C:=C:\" hold a drive's current folder.
            if sep and name:
                env[name] = value
        return env
    finally:
        if block.value:
            _userenv.DestroyEnvironmentBlock(block)
        _kernel32.CloseHandle(token)


def _kill(pid, started):
    """Ends a pool instance's SketchUp, if it's still the one recorded, and
    waits for it to go. Its model is a throwaway copy."""
    if not is_running(pid, started):
        return
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, capture_output=True)
    deadline = time.time() + 15
    while is_running(pid, started):
        if time.time() > deadline:
            raise PoolError(f"SketchUp (pid {pid}) didn't exit after taskkill")
        time.sleep(0.2)


# --- The pool's state --------------------------------------------------------

@contextmanager
def _locked():
    """Holds the pool's lock file, so one process at a time reads and writes
    pool.json. Held for the bookkeeping only, never while SketchUp starts."""
    _ensure_pool_dir()
    with open(LOCK_FILE, "a+b") as f:
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() > deadline:
                    raise PoolError(f"Timed out waiting for {LOCK_FILE}")
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def _ensure_pool_dir():
    """Creates the pool's folder where every process sees it. Created from
    a packaged app's process (a Claude session's MCP server), it would go to
    the package's private copy of %LOCALAPPDATA% (see the module's notes),
    so it's made by a process WMI starts, which is outside any package."""
    if POOL_DIR.is_dir():
        return
    path = str(POOL_DIR).replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
         f"@{{ CommandLine = 'cmd /c mkdir \"{path}\"' }} | Out-Null"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    deadline = time.time() + 10
    while not POOL_DIR.is_dir() and time.time() < deadline:
        time.sleep(0.2)
    # Without WMI, at least work for this process and its package.
    POOL_DIR.mkdir(parents=True, exist_ok=True)


def _name(port):
    return f"sketchup-{PORTS.index(port) + 1}"


def _blank(port):
    name = _name(port)
    return {
        "name": name,
        "port": port,
        "pid": None,
        "started": None,
        # Its own file name: SketchUp's autosave is keyed by file name.
        "model": str(MODELS_DIR / f"pool-{name}.skp"),
        "env": None,
        # Set once it answered eval_ruby with its pid.
        "ready": False,
        "lease": None,
    }


def _load():
    try:
        state = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    records = {r["port"]: r for r in state.get("instances", []) if r.get("port") in PORTS}
    return [records.get(port) or _blank(port) for port in PORTS]


def _save(records):
    tmp = POOL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"instances": records}, indent=2), encoding="utf-8")
    os.replace(tmp, POOL_FILE)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reap(records):
    """Frees every lease whose holder process has gone."""
    for r in records:
        lease = r["lease"]
        if lease and not is_running(lease["holder_pid"], lease["holder_started"]):
            r["lease"] = None


def _held_by(lease, who):
    return bool(lease) and lease["holder_pid"] == who[0] and abs(lease["holder_started"] - who[1]) < 0.01


def _alive(r):
    return is_running(r["pid"], r["started"])


def normalize_src(src):
    """A source folder as the dev loader wants it: absolute, forward slashes."""
    if not src:
        return None
    return os.path.abspath(os.path.expanduser(src)).replace("\\", "/").rstrip("/")


def _same_src(a, b):
    if not a or not b:
        return a == b
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --- Starting SketchUp -------------------------------------------------------

def _delete_autosave(model):
    """Deletes the model copy's autosave, so SketchUp doesn't stop at the
    "recovered version" dialog. -LiteralPath, so the name isn't a pattern.
    Run by spawn, outside the package, so it's SketchUp's real file."""
    path = str(AUTOSAVE_DIR / Path(model).name).replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"Remove-Item -LiteralPath '{path}' -Force -ErrorAction SilentlyContinue"],
        stdin=subprocess.DEVNULL, capture_output=True,
    )


def _port_open(port):
    try:
        with socket.create_connection((HOST, port), timeout=0.5):
            return True
    except OSError:
        return False


def _launch(r, src):
    """Starts SketchUp on a new copy of the template, so that it outlives the
    process that started it. Returns (pid, start time)."""
    if not SKETCHUP_EXE.exists():
        raise PoolError(f"SketchUp isn't at {SKETCHUP_EXE} (set SKETCHUP_EXE)")
    if not TEMPLATE.exists():
        raise PoolError(f"The template model isn't at {TEMPLATE} (set SKETCHUP_POOL_TEMPLATE)")
    if _port_open(r["port"]):
        raise PoolError(f"Something not in the pool already listens on port {r['port']}")

    # Started by a scheduled task of the instance's own, not as a child of
    # this process. An MCP client may run its server in a job object that
    # kills the server's children when it closes and allows no breaking away
    # (the Python SDK's stdio client does), which would take SketchUp down
    # with the session that started it. Task Scheduler starts the task on
    # the user's desktop, outside that job, as the "Start SketchUp" task
    # does for the default SketchUp. (WMI's Win32_Process.Create escapes the
    # job too, but SketchUp started by it never showed a window or listened,
    # 2026-10-22: it was outside the package too, and couldn't see the model
    # copy, which was inside.) The task runs `sketchup-launcher spawn`, which
    # copies the model, sets the environment, starts SketchUp and writes its
    # pid back.
    request = POOL_DIR / f"start-{r['name']}.json"
    result = POOL_DIR / f"started-{r['name']}.json"
    result.unlink(missing_ok=True)
    request.write_text(json.dumps({
        "exe": str(SKETCHUP_EXE), "template": str(TEMPLATE), "model": r["model"],
        "port": r["port"], "src": src, "result": str(result),
    }), encoding="utf-8")
    task = f"SketchUp pool {r['name']}"
    _create_task(task, request)
    done = subprocess.run(["schtasks", "/run", "/tn", task],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode != 0:
        raise PoolError(f"Running the task '{task}' failed: {done.stdout.strip()} {done.stderr.strip()}")
    deadline = time.time() + 60
    while not result.exists():
        if time.time() > deadline:
            raise PoolError(f"The task '{task}' didn't start SketchUp within 60 s")
        time.sleep(0.2)
    time.sleep(0.1)
    started = json.loads(result.read_text(encoding="utf-8"))
    result.unlink(missing_ok=True)
    if "error" in started:
        raise PoolError(f"Starting SketchUp failed: {started['error']}")
    return started["pid"], started["started"]


def _create_task(task, request):
    """Creates (or replaces) the scheduled task that starts an instance:
    run on demand only, as the logged-on user, on their desktop, with no
    time limit (the default is 72 hours) and not stopped on battery."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = Path(sys.executable)
    args = f'-m sketchup_mcp.launcher spawn "{request}"'
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts a SketchUp of the sketchup-mcp pool; see sketchup_mcp/launcher.py.</Description></RegistrationInfo>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_xml(str(pythonw))}</Command><Arguments>{_xml(args)}</Arguments></Exec></Actions>
</Task>"""
    # One file per task: two leases may be creating tasks at once.
    path = POOL_DIR / f"{task}.xml"
    path.write_text(xml, encoding="utf-16")
    try:
        done = subprocess.run(["schtasks", "/create", "/tn", task, "/xml", str(path), "/f"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)
    finally:
        path.unlink(missing_ok=True)
    if done.returncode != 0:
        raise PoolError(f"Creating the task '{task}' failed: {done.stdout.strip()} {done.stderr.strip()}")


def _xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def spawn(request):
    """Run by an instance's scheduled task: starts SketchUp on the request's
    model, and writes {"pid", "started"} or {"error"} to its result file."""
    req = json.loads(Path(request).read_text(encoding="utf-8"))
    out = Path(req["result"])
    try:
        model = Path(req["model"])
        model.parent.mkdir(parents=True, exist_ok=True)
        _delete_autosave(model)
        shutil.copyfile(req["template"], model)
        model.with_suffix(".skb").unlink(missing_ok=True)
        # The user's own environment, with this instance's port and source.
        # Names are case-insensitive on Windows; drop any spelling of these.
        env = {k: v for k, v in user_environment().items()
               if k.upper() not in ("SKETCHUP_MCP_PORT", "SKETCHUP_MCP_BIND", "SHELLIFY_SRC",
                                    "SKETCHUP_MCP_POOL_FILE")}
        env["SKETCHUP_MCP_PORT"] = str(req["port"])
        # Where the extension reads its lease, for its badge in the viewport.
        env["SKETCHUP_MCP_POOL_FILE"] = str(POOL_FILE)
        if req["src"]:
            env["SHELLIFY_SRC"] = req["src"]
        # Shown without taking the focus from whatever the user is doing.
        # Never minimized: SketchUp draws and picks only in a window that's
        # shown.
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
        args = dict(env=env, cwd=str(Path(req["exe"]).parent), startupinfo=startup, close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Out of the task's job too, where it allows that.
            proc = subprocess.Popen([req["exe"], req["model"]],
                                    creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **args)
        except OSError:
            proc = subprocess.Popen([req["exe"], req["model"]], creationflags=flags, **args)
        started = process_started(proc.pid)
        if started is None:
            raise PoolError(f"SketchUp exited straight away (exit code {proc.poll()})")
        reply = {"pid": proc.pid, "started": started}
    except Exception as e:
        reply = {"error": f"{type(e).__name__}: {e}"}
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(reply), encoding="utf-8")
    os.replace(tmp, out)


def ask_pid(port, timeout=5.0):
    """Asks the extension on port for SketchUp's pid through eval_ruby, the
    way the MCP server sends a request: one line of JSON each way."""
    request = {"jsonrpc": "2.0", "method": "tools/call", "id": 1,
               "params": {"name": "eval_ruby", "arguments": {"code": "Process.pid"}}}
    with socket.create_connection((HOST, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data.decode("utf-8"))
    return int(reply["result"]["content"][0]["text"])


def _wait_ready(r):
    """Waits until the instance's eval_ruby answers with its own pid."""
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if not _alive(r):
            raise PoolError(
                f"{r['name']}'s SketchUp (pid {r['pid']}) exited while starting. Its log is "
                "entlog-sketchup.txt in %LOCALAPPDATA%\\SketchUp\\SketchUp 2026.")
        try:
            pid = ask_pid(r["port"])
            if pid == r["pid"]:
                return
            raise PoolError(f"Port {r['port']} answered from SketchUp pid {pid}, not {r['pid']}")
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            time.sleep(1)
    raise PoolError(
        f"{r['name']}'s SketchUp (pid {r['pid']}) didn't answer on port {r['port']} within "
        f"{START_TIMEOUT:g} s. It may be stopped at a dialog.")


# --- Leasing -----------------------------------------------------------------

def _pick(records, src):
    """The free instance a lease should take, and whether it needs starting:
    one already running on src; else one that isn't running; else a running
    one, restarted on src."""
    free = [r for r in records if not r["lease"]]
    for r in free:
        if _alive(r) and r["ready"] and _same_src((r["env"] or {}).get("SHELLIFY_SRC"), src):
            return r, False
    for r in free:
        if not _alive(r):
            return r, True
    if free:
        return free[0], True
    return None, False


def _busy_message(records):
    held = ", ".join(
        f"{r['name']} ({r['lease'].get('label') or 'holder pid ' + str(r['lease']['holder_pid'])}, "
        f"last used {r['lease']['last_used']})"
        for r in records)
    return f"All {len(records)} pool instances are leased: {held}. Wait for one to be released."


def lease(src=None, who=None, label=None, session=None, link=None):
    """Leases a SketchUp to who (default: this process), and returns its
    record with what happened:

    - fresh: whether SketchUp was just started on a new copy of the template
      (anything built or loaded in an earlier lease is gone);
    - message: the same, in words.

    A holder has one SketchUp at most. Leasing again returns the one it
    holds, unless its SketchUp has died or runs another src, when it's
    replaced.

    src is the Shellify source folder (a session's worktree); default
    SHELLIFY_SRC. label (what the holder is doing, such as a session's
    title), session (the Claude desktop app's id for it) and link (a URL that
    opens it) are recorded with the lease, for the badge the extension shows
    in a leased SketchUp's viewport.
    """
    who = who or holder()
    src = normalize_src(src or os.environ.get("SHELLIFY_SRC"))
    now = _now()
    with _locked():
        records = _load()
        _reap(records)
        for r in records:
            if _held_by(r["lease"], who):
                if _alive(r) and r["ready"] and _same_src((r["env"] or {}).get("SHELLIFY_SRC"), src):
                    r["lease"].update(last_used=now, label=label or r["lease"].get("label"))
                    _save(records)
                    return dict(r, fresh=False, message=f"Already leased {r['name']} on port {r['port']}.")
                r["lease"] = None
        r, start = _pick(records, src)
        if r is None:
            _save(records)
            raise PoolError(_busy_message(records))
        r["lease"] = {"holder_pid": who[0], "holder_started": who[1], "label": label,
                      "session": session, "link": link, "leased_at": now, "last_used": now}
        if start:
            # Reserved under the lock; started outside it.
            r["ready"] = False
            old = (r["pid"], r["started"])
        _save(records)

    if not start:
        return dict(r, fresh=False, message=(
            f"Leased {r['name']} on port {r['port']}, already running on this source. Its model "
            "is as the last holder left it, and code it loaded is still loaded; call Shellify.sync."))

    try:
        _kill(*old)
        pid, started = _launch(r, src)
        with _locked():
            records = _load()
            rec = next(x for x in records if x["port"] == r["port"])
            rec.update(pid=pid, started=started, env={"SHELLIFY_SRC": src}, ready=False)
            _save(records)
        r = rec
        _wait_ready(r)
    except Exception:
        # Free the lease, so the next one doesn't wait on a broken instance
        # (it isn't ready, so that one restarts it).
        with _locked():
            records = _load()
            rec = next(x for x in records if x["port"] == r["port"])
            if _held_by(rec["lease"], who):
                rec["lease"] = None
            _save(records)
        raise

    with _locked():
        records = _load()
        rec = next(x for x in records if x["port"] == r["port"])
        rec["ready"] = True
        _save(records)
    return dict(rec, fresh=True, message=(
        f"Leased {rec['name']} on port {rec['port']}: SketchUp started fresh (pid {rec['pid']}) on a "
        f"new copy of the template model, loading Shellify from {src}."))


def release(who=None):
    """Frees who's lease. SketchUp keeps running for the next lease. Returns
    the instance's record, or None if who held none."""
    who = who or holder()
    with _locked():
        records = _load()
        _reap(records)
        found = None
        for r in records:
            if _held_by(r["lease"], who):
                r["lease"] = None
                found = r
        _save(records)
    return found


def held(who=None, touch=True):
    """who's lease, as its instance's record, or None. Marks the lease used
    now."""
    who = who or holder()
    with _locked():
        records = _load()
        for r in records:
            if _held_by(r["lease"], who):
                if touch:
                    r["lease"]["last_used"] = _now()
                    _save(records)
                return dict(r, alive=_alive(r))
    return None


def status():
    """Every instance's record, with whether its SketchUp is running."""
    with _locked():
        records = _load()
        _reap(records)
        _save(records)
    return [dict(r, alive=_alive(r)) for r in records]


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="sketchup-launcher",
        description="Lease a SketchUp from the pool. A lease belongs to the shell that ran "
                    "this, unless --holder names another process, and each holder has one.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the pool")
    p = sub.add_parser("lease", help="lease an instance, starting it if need be")
    p.add_argument("--src", help="Shellify source folder (default: SHELLIFY_SRC)")
    p.add_argument("--label", help="what it's for, shown in SketchUp's viewport")
    p.add_argument("--holder", type=int, help="pid of the process the lease belongs to")
    p = sub.add_parser("release", help="release the lease; SketchUp keeps running")
    p.add_argument("--holder", type=int)
    p = sub.add_parser("spawn")  # run by an instance's scheduled task
    p.add_argument("request")
    args = parser.parse_args(argv)

    if args.command == "spawn":
        spawn(args.request)
        return 0

    try:
        if args.command == "status":
            out = status()
        else:
            who = holder(args.holder or calling_shell())
            if args.command == "lease":
                out = lease(args.src, who, label=args.label)
            else:
                out = release(who)
                if out is None:
                    raise PoolError(f"Process {who[0]} holds no lease")
    except PoolError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
