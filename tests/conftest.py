"""Shared fixtures. Nothing here touches a real socket or a real SketchUp."""

import json

import pytest

from sketchup_mcp import server


class FakeSocket:
    """Stands in for socket.socket. Records what was sent, replays scripted replies."""

    def __init__(self, replies=None):
        # Each reply is bytes delivered by one recv() call; [] means "closed".
        self.replies = list(replies or [])
        self.sent = b""
        self.closed = False
        self.timeout = None

    # -- methods the server calls --
    def connect(self, _addr):
        pass

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
    """Build a SketchupConnection whose socket is a FakeSocket with the given replies."""

    def _make(replies=None):
        sock = FakeSocket(replies)
        conn = server.SketchupConnection(host="test-host", port=1, sock=sock)
        return conn, sock

    return _make


@pytest.fixture(autouse=True)
def reset_global_connection():
    """The module caches one connection globally; isolate tests from each other."""
    server._sketchup_connection = None
    yield
    server._sketchup_connection = None
