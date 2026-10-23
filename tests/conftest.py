"""Shared fixtures. Nothing here touches a real socket or a real SketchUp."""

import json

import pytest

from sketchup_mcp import server


class FakeSocket:
    """Stands in for socket.socket. Records what was sent, replays scripted replies."""

    def __init__(self, replies=None, refuse=False):
        # Each reply is bytes delivered by one recv() call; [] means "closed".
        self.replies = list(replies or [])
        self.sent = b""
        self.closed = False
        self.timeout = None
        self.address = None
        self.refuse = refuse

    # -- methods the server calls --
    def connect(self, addr):
        if self.refuse:
            raise ConnectionRefusedError("refused")
        self.address = addr

    def settimeout(self, t):
        self.timeout = t

    def send(self, data):
        return len(data)

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        if not self.replies:
            return b""  # connection closed
        return self.replies.pop(0)

    def close(self):
        self.closed = True


def json_reply(obj):
    """Encode one JSON reply as the SketchUp extension would send it."""
    return json.dumps(obj).encode("utf-8")


@pytest.fixture
def make_connection():
    """Build a SketchupConnection whose sockets are FakeSockets.

    Each positional argument is the reply script for one connection, in
    order; the last repeats. refuse_first makes that many connects fail
    first. conn.sockets lists every socket the connection opened.
    """

    def _make(replies=None, *more_replies, refuse_first=0):
        scripts = [replies, *more_replies]
        opened = []

        def factory():
            index = len(opened)
            refuse = index < refuse_first
            script = None if refuse else scripts[min(index - refuse_first, len(scripts) - 1)]
            sock = FakeSocket(script, refuse=refuse)
            opened.append(sock)
            return sock

        conn = server.SketchupConnection(host="test-host", port=1, socket_factory=factory, retry_delay=0)
        conn.sockets = opened
        return conn

    return _make
