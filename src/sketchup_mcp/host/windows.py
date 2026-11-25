"""The pool on Windows: see the package's notes for what each name is.

This is launcher.py's Windows code, moved here as it was. launcher.py's own
notes say why SketchUp is started by a scheduled task and why the pool's
folder is made through WMI.
"""

import ctypes
import json
import msvcrt
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from . import PoolError

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))

SKETCHUP_EXE = r"C:\Program Files\SketchUp\SketchUp 2026\SketchUp\SketchUp.exe"

START_LOG_HINT = "Its log is entlog-sketchup.txt in %LOCALAPPDATA%\\SketchUp\\SketchUp 2026."

# SketchUp 2026 autosaves to this folder, keyed by the model's file name
# only, and after a crash asks whether to open the "recovered version"
# before its extensions load, so nothing answers on the port. The model
# copy's file there is deleted before every start.
AUTOSAVE_DIR = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                / "SketchUp" / "SketchUp 2026" / "SketchUp" / "working")


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


def processes():
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


_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def image_path(pid):
    """The full path of a running process's program, or None."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        _kernel32.CloseHandle(handle)


# Programs that start Python for something else (a venv's python.exe runs
# the real interpreter as its child): the process that started the MCP
# server is the first ancestor that isn't one of them.
PYTHON_LAUNCHERS = {"python.exe", "pythonw.exe", "py.exe", "uv.exe", "uvx.exe"}

# The same for the sketchup-launcher CLI: a pip console script on Windows is
# an .exe that runs Python as its child.
CLI_WRAPPERS = {"sketchup-launcher.exe", "python.exe", "pythonw.exe", "py.exe"}


def is_desktop_app(path):
    """Whether the program at path is the Claude desktop app itself, which
    is a packaged app, and not the Claude Code it runs for a session."""
    path = (path or "").replace("/", "\\").lower()
    return "\\windowsapps\\claude_" in path


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


KILLED_WITH = "taskkill"


def kill(pid):
    """Ends a SketchUp at once. Its model is a throwaway copy."""
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL, capture_output=True)


# --- The pool's lock and folder ----------------------------------------------

def lock(f):
    """Takes the pool's lock on its open lock file, or raises OSError if
    another process holds it."""
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)


def unlock(f):
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def make_shared_dir(folder):
    """Creates the pool's folder where every process sees it. Created from
    a packaged app's process (a Claude session's MCP server), it would go to
    the package's private copy of %LOCALAPPDATA% (see launcher's notes),
    so it's made by a process WMI starts, which is outside any package."""
    path = str(folder).replace("'", "''")
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
         f"@{{ CommandLine = 'cmd /c mkdir \"{path}\"' }} | Out-Null"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    deadline = time.time() + 10
    while not folder.is_dir() and time.time() < deadline:
        time.sleep(0.2)


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


def start(r, src, exe, template, pool_dir, pool_file):
    """Starts SketchUp (exe) for instance r on a new copy of template, so
    that it outlives the process that started it. Returns (pid, start time).
    Its request and result files go in pool_dir. pool_file isn't used
    here: spawn is given it by the process that runs it."""
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
    request = pool_dir / f"start-{r['name']}.json"
    result = pool_dir / f"started-{r['name']}.json"
    result.unlink(missing_ok=True)
    request.write_text(json.dumps({
        "exe": str(exe), "template": str(template), "model": r["model"],
        "port": r["port"], "src": src, "result": str(result),
    }), encoding="utf-8")
    task = f"SketchUp pool {r['name']}"
    _create_task(task, request, pool_dir)
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


def _create_task(task, request, pool_dir):
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
    path = pool_dir / f"{task}.xml"
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


def spawn(request, pool_file):
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
        env["SKETCHUP_MCP_POOL_FILE"] = str(pool_file)
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
