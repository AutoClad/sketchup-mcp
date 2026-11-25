"""Lets the suite load sketchup_mcp off Windows.

launcher.py uses Windows' APIs as it loads (msvcrt, and kernel32, advapi32
and userenv through ctypes.WinDLL), and server.py imports it, so on Linux
(CI) or macOS neither can be imported as they are. Before any test module
is, this puts stand-ins in their place: a lock that locks nothing, and DLLs
whose every function fails by returning 0, as a Windows call does. So off
Windows process_started finds no process and _processes finds none, and a
test that needs a real answer from Windows must replace it or be skipped.

On Windows nothing is replaced.
"""

import sys

if sys.platform != "win32":
    import ctypes
    import types

    class _Function:
        """A DLL function that fails; restype and argtypes can be set on it."""

        def __call__(self, *_args):
            return 0

    class _Dll:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, name):
            function = _Function()
            setattr(self, name, function)
            return function

    ctypes.WinDLL = _Dll

    msvcrt = types.ModuleType("msvcrt")
    msvcrt.LK_NBLCK = 2
    msvcrt.LK_UNLCK = 0
    msvcrt.locking = lambda _fd, _mode, _nbytes: None
    sys.modules.setdefault("msvcrt", msvcrt)
