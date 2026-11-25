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

import json  # noqa: E402
import socket  # noqa: E402

import pytest  # noqa: E402

from sketchup_mcp import server  # noqa: E402


class FakeSocket:
    """Stands in for socket.socket. Records what was sent, replays scripted replies.

    Each reply is bytes (or an exception to raise) delivered by one recv()
    call; once the script runs out, recv() returns b"" (the peer closed). A
    peek (MSG_PEEK) between requests raises BlockingIOError, as a quiet open
    socket does, or returns b"" once closed_before_send is set.
    """

    def __init__(self, replies=None, refuse=False, closed_before_send=False):
        self.replies = list(replies or [])
        self.sent = b""
        self.closed = False
        self.timeout = None
        self.address = None
        self.refuse = refuse
        # The peer hung up before anything was sent (SketchUp restarted).
        self.closed_before_send = closed_before_send

    def connect(self, addr):
        if self.refuse:
            raise ConnectionRefusedError("refused")
        self.address = addr

    def settimeout(self, t):
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def setblocking(self, flag):
        self.timeout = None if flag else 0.0

    def sendall(self, data):
        self.sent += data

    def recv(self, _size, flags=0):
        if flags & socket.MSG_PEEK:
            # Replies arrive only in answer to a request, so a quiet socket
            # between requests has nothing waiting, unless the peer hung up.
            if self.closed_before_send:
                return b""
            raise BlockingIOError()
        if not self.replies:
            return b""  # connection closed
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        self.closed = True


def json_reply(obj):
    """Encode one JSON reply as the SketchUp extension would send it."""
    return json.dumps(obj).encode("utf-8") + b"\n"


@pytest.fixture
def make_connection(monkeypatch):
    """Build a SketchupConnection whose sockets are FakeSockets.

    Each positional argument is a dict of FakeSocket arguments for one
    socket, in the order they're opened; the last repeats.
    conn.sockets lists every socket the connection opened.
    """

    def _make(*scripts):
        scripts = scripts or ({},)
        opened = []

        def factory(*_args, **_kwargs):
            sock = FakeSocket(**scripts[min(len(opened), len(scripts) - 1)])
            opened.append(sock)
            return sock

        monkeypatch.setattr(server.socket, "socket", factory)
        conn = server.SketchupConnection(host="test-host", port=1)
        conn.sockets = opened
        return conn

    return _make


@pytest.fixture
def pool(monkeypatch):
    """The pool as server.py sees it, with nothing of Windows behind it: this
    server isn't shared, its holder is (1, 1.0), and it holds no lease until
    a test sets pool.record. pool.leases and pool.releases list the calls."""

    class Pool:
        record = None  # what launcher.held answers: this session's lease
        new_record = None  # what launcher.lease answers, or an exception it raises

    state = Pool()
    state.leases, state.releases = [], []

    def held(_who, touch=True):
        return state.record

    def lease(src, who=None, label=None, session=None, link=None, instance=None):
        state.leases.append({"src": src, "who": who, "label": label, "session": session, "link": link})
        if isinstance(state.new_record, Exception):
            raise state.new_record
        return state.new_record

    def release(who=None):
        state.releases.append(who)
        return None

    monkeypatch.setattr(server, "_shared", "")
    monkeypatch.setattr(server, "_holder", (1, 1.0))
    monkeypatch.setattr(server, "_connections", {})
    monkeypatch.setattr(server.launcher, "shared_server", lambda *a, **k: None)
    monkeypatch.setattr(server.launcher, "held", held)
    monkeypatch.setattr(server.launcher, "lease", lease)
    monkeypatch.setattr(server.launcher, "release", release)
    return state
