"""SketchupConnection: the JSON-over-TCP protocol to the SketchUp extension."""

import json

import pytest

from tests.conftest import json_reply


def sent_request(sock):
    """Decode the single request the server wrote to the socket."""
    assert sock.sent.endswith(b"\n"), "requests must be newline-terminated"
    return json.loads(sock.sent.decode("utf-8"))


def test_direct_command_is_wrapped_as_jsonrpc_tools_call(make_connection):
    conn, sock = make_connection([json_reply({"result": {"ok": True}})])

    conn.send_command("create_component", {"type": "cube"}, request_id=7)

    req = sent_request(sock)
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "tools/call"
    assert req["params"] == {"name": "create_component", "arguments": {"type": "cube"}}
    assert req["id"] == 7


def test_preformed_tools_call_passes_through_unchanged(make_connection):
    conn, sock = make_connection([json_reply({"result": {}})])
    params = {"name": "eval_ruby", "arguments": {"code": "1 + 1"}}

    conn.send_command("tools/call", params, request_id=3)

    req = sent_request(sock)
    assert req["method"] == "tools/call"
    assert req["params"] == params


def test_result_field_is_returned(make_connection):
    conn, _ = make_connection([json_reply({"result": {"id": "abc", "value": 2}})])

    assert conn.send_command("eval_ruby", {"code": "1+1"}) == {"id": "abc", "value": 2}


def test_error_response_raises_with_message(make_connection):
    conn, _ = make_connection([json_reply({"error": {"code": -1, "message": "undefined method foo"}})])

    with pytest.raises(Exception, match="undefined method foo"):
        conn.send_command("eval_ruby", {"code": "foo"})


def test_response_split_across_chunks_is_reassembled(make_connection):
    full = json_reply({"result": {"entities": list(range(50))}})
    conn, _ = make_connection([full[:20], full[20:45], full[45:]])

    assert conn.send_command("get_selection") == {"entities": list(range(50))}


def test_connection_closed_before_reply_raises(make_connection):
    conn, _ = make_connection([])  # recv returns b"" immediately

    with pytest.raises(Exception, match="Connection closed|No data received|Connection to Sketchup lost"):
        conn.send_command("get_selection")


def test_non_json_reply_raises(make_connection):
    conn, _ = make_connection([b"this is not json"])

    with pytest.raises(Exception, match="Incomplete JSON|Invalid response|Communication error"):
        conn.send_command("get_selection")
