"""Host and port come from the environment so the Python side can target a VM."""

from sketchup_mcp import server


class RecordingConnection:
    """Replaces SketchupConnection to capture constructor args without connecting."""

    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        RecordingConnection.instances.append(self)

    def connect(self):
        return True


def _patch(monkeypatch):
    RecordingConnection.instances.clear()
    monkeypatch.setattr(server, "SketchupConnection", RecordingConnection)


def test_defaults_to_localhost_9876(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.delenv("SKETCHUP_HOST", raising=False)
    monkeypatch.delenv("SKETCHUP_PORT", raising=False)

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("localhost", 9876)


def test_env_vars_override_host_and_port(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setenv("SKETCHUP_HOST", "192.168.5.20")
    monkeypatch.setenv("SKETCHUP_PORT", "9900")

    server.get_sketchup_connection()

    (conn,) = RecordingConnection.instances
    assert (conn.host, conn.port) == ("192.168.5.20", 9900)
