"""MCP tool functions, with the SketchUp connection replaced by a stub."""

import json
from dataclasses import dataclass

from sketchup_mcp import server


@dataclass
class FakeContext:
    request_id: int = 42


class StubConnection:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def send_command(self, method, params=None, request_id=None):
        self.calls.append({"method": method, "params": params, "request_id": request_id})
        if self.error:
            raise Exception(self.error)
        return self.result


def test_eval_ruby_sends_code_and_returns_result(monkeypatch):
    # The extension replies in MCP tool-result shape: content is a list of text parts.
    stub = StubConnection(result={"content": [{"type": "text", "text": "2"}]})
    monkeypatch.setattr(server, "get_sketchup_connection", lambda: stub)

    out = json.loads(server.eval_ruby(FakeContext(), "1 + 1"))

    (call,) = stub.calls
    assert call["method"] == "tools/call"
    assert call["params"] == {"name": "eval_ruby", "arguments": {"code": "1 + 1"}}
    assert call["request_id"] == 42
    assert out["success"] is True
    assert out["result"] == "2"


def test_eval_ruby_without_content_reports_success(monkeypatch):
    stub = StubConnection(result={})
    monkeypatch.setattr(server, "get_sketchup_connection", lambda: stub)

    out = json.loads(server.eval_ruby(FakeContext(), "nil"))

    assert out == {"success": True, "result": "Success"}


def test_eval_ruby_reports_errors_instead_of_raising(monkeypatch):
    stub = StubConnection(error="NameError: undefined local variable")
    monkeypatch.setattr(server, "get_sketchup_connection", lambda: stub)

    out = json.loads(server.eval_ruby(FakeContext(), "nope"))

    assert out["success"] is False
    assert "NameError" in out["error"]
