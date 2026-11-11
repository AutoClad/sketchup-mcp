"""Where tool calls go: a SketchUp leased from the pool (Windows), or, without
the pool, the one SketchUp at SKETCHUP_HOST:SKETCHUP_PORT (a Mac, or a host
driving SketchUp in a VM)."""

import json

import pytest

from sketchup_mcp import server


class RecordingConnection:
    """Replaces SketchupConnection to capture constructor args without connecting."""

    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        RecordingConnection.instances.append(self)


class FakeLauncher:
    """The pool, with one instance leased to this session on port 9878."""

    HOST = "127.0.0.1"

    def holder(self):
        return (1, 1.0)

    def held(self, _who):
        return {"name": "sketchup-2", "port": 9878, "alive": True, "env": {}, "lease": {}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    RecordingConnection.instances.clear()
    monkeypatch.setattr(server, "SketchupConnection", RecordingConnection)
    monkeypatch.setattr(server, "_connections", {})
    monkeypatch.setattr(server, "_holder", None)
    monkeypatch.delenv("SKETCHUP_HOST", raising=False)
    monkeypatch.delenv("SKETCHUP_PORT", raising=False)


def test_without_the_pool_defaults_to_localhost_9876(monkeypatch):
    monkeypatch.setattr(server, "launcher", None)

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("localhost", 9876)


def test_env_vars_override_host_and_port(monkeypatch):
    monkeypatch.setattr(server, "launcher", None)
    monkeypatch.setenv("SKETCHUP_HOST", "192.168.5.20")
    monkeypatch.setenv("SKETCHUP_PORT", "9900")

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("192.168.5.20", 9900)


def test_with_the_pool_calls_go_to_the_leased_instance(monkeypatch):
    monkeypatch.setattr(server, "launcher", FakeLauncher())

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("127.0.0.1", 9878)


def test_sketchup_port_opts_out_of_the_pool(monkeypatch):
    monkeypatch.setattr(server, "launcher", FakeLauncher())
    monkeypatch.setenv("SKETCHUP_PORT", "9876")

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("localhost", 9876)


@pytest.mark.parametrize("tool", ["lease_sketchup", "release_sketchup"])
def test_lease_tools_say_there_is_no_pool_without_one(monkeypatch, tool):
    import asyncio

    monkeypatch.setattr(server, "launcher", None)
    args = {"src": "/tmp"} if tool == "lease_sketchup" else {}

    out = json.loads(asyncio.run(getattr(server, tool)(None, **args)))

    assert out["success"] is False
    assert "no pool here" in out["error"]
    assert "localhost:9876" in out["error"]
