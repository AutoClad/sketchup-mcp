"""SketchupConnection: the JSON-over-TCP protocol to the SketchUp extension.

One socket per SketchUp, kept open across requests: each request is one
newline-terminated JSON object, and each reply one JSON object. A socket the
extension has closed is noticed before sending (with a peek), so a call
after SketchUp restarts doesn't fail; a request that was sent is never sent
again when retries=0 (eval_ruby), since it may have run.
"""

import asyncio
import json

import pytest

from sketchup_mcp import server
from tests.conftest import json_reply


def requests_sent(sock):
    """Decode every request the server wrote to the socket."""
    assert sock.sent.endswith(b"\n"), "requests must be newline-terminated"
    return [json.loads(line) for line in sock.sent.decode("utf-8").splitlines()]


def test_direct_command_is_wrapped_as_jsonrpc_tools_call(make_connection):
    conn = make_connection({"replies": [json_reply({"result": {"ok": True}})]})

    conn.send_command("get_selection", {"a": 1}, request_id=7)

    (req,) = requests_sent(conn.sockets[0])
    assert req == {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_selection", "arguments": {"a": 1}},
        "id": 7,
    }


def test_preformed_tools_call_passes_through_unchanged(make_connection):
    conn = make_connection({"replies": [json_reply({"result": {}})]})
    params = {"name": "eval_ruby", "arguments": {"code": "1"}}

    conn.send_command("tools/call", params, request_id=3)

    (req,) = requests_sent(conn.sockets[0])
    assert req["params"] == params and req["id"] == 3


def test_result_field_is_returned(make_connection):
    conn = make_connection({"replies": [json_reply({"result": {"content": [{"text": "2"}]}})]})

    assert conn.send_command("get_selection") == {"content": [{"text": "2"}]}


def test_error_response_raises_and_keeps_the_connection(make_connection):
    conn = make_connection({"replies": [json_reply({"error": {"message": "boom"}})]})

    with pytest.raises(server.SketchupRubyError, match="boom"):
        conn.send_command("get_selection")
    assert conn.sock is conn.sockets[0] and not conn.sockets[0].closed


def test_response_split_across_chunks_is_reassembled(make_connection):
    body = json_reply({"result": {"text": "a\nb {not a brace}"}})
    conn = make_connection({"replies": [body[:10], body[10:25], body[25:]]})

    assert conn.send_command("get_selection") == {"text": "a\nb {not a brace}"}


def test_the_socket_is_kept_for_the_next_request(make_connection):
    conn = make_connection({"replies": [json_reply({"result": {"n": 1}}), json_reply({"result": {"n": 2}})]})

    assert conn.send_command("get_selection") == {"n": 1}
    assert conn.send_command("get_selection") == {"n": 2}
    assert len(conn.sockets) == 1
    assert len(requests_sent(conn.sockets[0])) == 2


def test_a_socket_the_extension_closed_is_replaced_before_sending(make_connection):
    # SketchUp restarted between calls: the old socket peeks as closed.
    conn = make_connection(
        {"replies": [json_reply({"result": {"n": 1}})]},
        {"replies": [json_reply({"result": {"n": 2}})]},
    )
    conn.send_command("get_selection")
    conn.sockets[0].closed_before_send = True

    assert conn.send_command("get_selection") == {"n": 2}
    assert conn.sockets[0].closed
    assert len(requests_sent(conn.sockets[0])) == 1
    assert len(requests_sent(conn.sockets[1])) == 1


def test_connection_closed_before_reply_raises(make_connection):
    conn = make_connection({"replies": []})

    with pytest.raises(Exception, match="Connection closed before receiving any data"):
        conn.send_command("get_selection", retries=0)


def test_a_sent_request_is_never_retried_when_retries_is_zero(make_connection):
    conn = make_connection({"replies": [TimeoutError("timed out")]})

    with pytest.raises(Exception, match="No response from Sketchup"):
        conn.send_command("tools/call", {"name": "eval_ruby", "arguments": {"code": "x += 1"}}, retries=0)
    sent = [s for s in conn.sockets if s.sent]
    assert len(sent) == 1 and len(requests_sent(sent[0])) == 1


def test_a_refused_connect_says_where_it_tried(make_connection):
    conn = make_connection({"refuse": True})

    with pytest.raises(ConnectionError, match="test-host:1"):
        conn.send_command("get_selection")


def test_getting_a_connection_opens_no_socket(monkeypatch):
    opened = []
    monkeypatch.setattr(server.socket, "socket", lambda *a, **k: opened.append(1))
    monkeypatch.setattr(server, "launcher", None)
    monkeypatch.setattr(server, "_connections", {})

    conn = server.get_sketchup_connection()

    assert isinstance(conn, server.SketchupConnection)
    assert opened == []


def test_server_startup_does_not_connect_to_sketchup(monkeypatch):
    # Patched on the class, not socket.socket, which asyncio needs too.
    opened = []
    monkeypatch.setattr(server.SketchupConnection, "connect", lambda *a, **k: opened.append(1))
    monkeypatch.setattr(server.SketchupConnection, "send_command", lambda *a, **k: opened.append(1))
    monkeypatch.setattr(server, "launcher", None)

    async def run():
        async with server.server_lifespan(server.mcp):
            pass

    asyncio.run(run())
    assert opened == []
