"""SketchupConnection: the JSON-over-TCP protocol to the SketchUp extension."""

import json

import pytest

from sketchup_mcp import server
from tests.conftest import json_reply


def sent_request(sock):
    """Decode the single request the server wrote to the socket."""
    assert sock.sent.endswith(b"\n"), "requests must be newline-terminated"
    return json.loads(sock.sent.decode("utf-8"))


def test_direct_command_is_wrapped_as_jsonrpc_tools_call(make_connection):
    conn = make_connection([json_reply({"result": {"ok": True}})])

    conn.send_command("create_component", {"type": "cube"}, request_id=7)

    req = sent_request(conn.sockets[-1])
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "tools/call"
    assert req["params"] == {"name": "create_component", "arguments": {"type": "cube"}}
    assert req["id"] == 7


def test_preformed_tools_call_passes_through_unchanged(make_connection):
    conn = make_connection([json_reply({"result": {}})])
    params = {"name": "eval_ruby", "arguments": {"code": "1 + 1"}}

    conn.send_command("tools/call", params, request_id=3)

    req = sent_request(conn.sockets[-1])
    assert req["method"] == "tools/call"
    assert req["params"] == params


def test_result_field_is_returned(make_connection):
    conn = make_connection([json_reply({"result": {"id": "abc", "value": 2}})])

    assert conn.send_command("eval_ruby", {"code": "1+1"}) == {"id": "abc", "value": 2}


def test_error_response_raises_with_message(make_connection):
    conn = make_connection([json_reply({"error": {"code": -1, "message": "undefined method foo"}})])

    with pytest.raises(Exception, match="undefined method foo"):
        conn.send_command("eval_ruby", {"code": "foo"})


def test_response_split_across_chunks_is_reassembled(make_connection):
    full = json_reply({"result": {"entities": list(range(50))}})
    conn = make_connection([full[:20], full[20:45], full[45:]])

    assert conn.send_command("get_selection") == {"entities": list(range(50))}


def test_connection_closed_before_reply_raises(make_connection):
    conn = make_connection([])  # recv returns b"" immediately

    with pytest.raises(Exception, match="Connection closed|No data received|Connection to Sketchup lost"):
        conn.send_command("get_selection")


def test_non_json_reply_raises(make_connection):
    conn = make_connection([b"this is not json"])

    with pytest.raises(Exception, match="Incomplete JSON|Invalid response|Communication error"):
        conn.send_command("get_selection")


# --- One request per connection (the extension's protocol) -----------------


def test_each_command_opens_its_own_connection_and_closes_it(make_connection):
    conn = make_connection([json_reply({"result": {"n": 1}})], [json_reply({"result": {"n": 2}})])

    assert conn.send_command("get_selection") == {"n": 1}
    assert conn.send_command("get_selection") == {"n": 2}

    assert len(conn.sockets) == 2, "a fresh connection per command"
    assert all(s.closed for s in conn.sockets), "never left open after the reply"
    assert all(s.address == ("test-host", 1) for s in conn.sockets)


def test_connection_is_closed_even_when_the_reply_is_an_error(make_connection):
    conn = make_connection([json_reply({"error": {"code": -1, "message": "boom"}})])

    with pytest.raises(Exception, match="boom"):
        conn.send_command("eval_ruby", {"code": "x"})
    assert conn.sockets[0].closed


def test_connection_is_closed_when_nothing_comes_back(make_connection):
    conn = make_connection([])

    with pytest.raises(Exception, match="Connection closed|No data received"):
        conn.send_command("get_selection")
    assert conn.sockets[0].closed


def test_refused_connect_is_retried_with_a_fresh_socket(make_connection):
    conn = make_connection([json_reply({"result": {"ok": True}})], refuse_first=2)

    assert conn.send_command("get_selection") == {"ok": True}
    assert len(conn.sockets) == 3
    assert all(s.closed for s in conn.sockets)


def test_gives_up_after_three_refused_connects_with_a_helpful_message(make_connection):
    conn = make_connection(None, refuse_first=3)

    with pytest.raises(Exception, match="Start Server"):
        conn.send_command("get_selection")
    assert len(conn.sockets) == 3


def test_a_sent_request_is_never_retried(make_connection):
    # Once the request is on the wire, a retry could run an eval twice.
    conn = make_connection([])

    with pytest.raises(Exception, match="Connection closed|No data received"):
        conn.send_command("eval_ruby", {"code": "counter += 1"})
    assert len(conn.sockets) == 1


def test_getting_a_connection_opens_no_socket(monkeypatch):
    opened = []
    monkeypatch.setattr(server, "_new_socket", lambda: opened.append(1))

    conn = server.get_sketchup_connection()

    assert isinstance(conn, server.SketchupConnection)
    assert opened == []


def test_server_startup_does_not_connect_to_sketchup(monkeypatch):
    # An idle connection held from startup is what froze SketchUp: the
    # extension blocks waiting for a request that never comes.
    import asyncio

    opened = []
    monkeypatch.setattr(server, "_new_socket", lambda: opened.append(1))
    monkeypatch.setattr(server.SketchupConnection, "send_command", lambda *a, **k: opened.append(1))

    async def run():
        async with server.server_lifespan(server.mcp):
            pass

    asyncio.run(run())
    assert opened == []


def test_reply_is_read_up_to_its_newline_without_waiting_for_close(make_connection):
    # The server ends every reply with a newline; reading must stop there.
    conn = make_connection([json_reply({"result": {"ok": 1}}) + b"\n", b"never read"])

    assert conn.send_command("get_selection") == {"ok": 1}
    assert conn.sockets[0].replies == [b"never read"]


def test_a_real_socket_is_created_per_command_when_no_factory_is_given(monkeypatch):
    # Guards the regression the startup/no-socket tests exist for: the default
    # path must go through _new_socket at call time, so patching it is seen.
    from tests.conftest import FakeSocket

    opened = []

    def fake():
        sock = FakeSocket([json_reply({"result": {}}) + b"\n"])
        opened.append(sock)
        return sock

    monkeypatch.setattr(server, "_new_socket", fake)
    conn = server.get_sketchup_connection()
    assert opened == []
    conn.send_command("get_selection")
    conn.send_command("get_selection")
    assert len(opened) == 2 and all(s.closed for s in opened)
