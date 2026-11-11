"""Shared fixtures. Nothing here touches a real socket or a real SketchUp."""

import json
import socket

import pytest

from sketchup_mcp import server


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
        self.timeout_at_connect = self.timeout
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
