"""The Streamable HTTP transport: sessions, origins, and the methods it answers."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from gagelink.server import Server
from gagelink.streamable import Sessions, origin_is_allowed, serve_http
from test_server import offline


@pytest.fixture
def endpoint():
    httpd = serve_http(lambda: Server(session_factory=offline), port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()
    httpd.sessions.close_all()
    thread.join(timeout=5)


def ask(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data, {"Content-Type": "application/json", **(headers or {})}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, dict(exc.headers), json.loads(raw) if raw else None


def initialise(url, version="2025-06-18"):
    status, headers, body = ask(
        url,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}},
    )
    return headers["Mcp-Session-Id"], body["result"]


def test_a_session_is_issued_at_initialise_and_required_afterwards(endpoint):
    session_id, result = initialise(endpoint)
    assert len(session_id) == 32
    assert result["serverInfo"]["name"] == "gagelink"

    status, _, body = ask(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 404
    assert "initialize" in body["error"]["message"]


def test_a_tool_call_travels_over_http_carrying_its_frames(endpoint):
    session_id, _ = initialise(endpoint)
    status, _, body = ask(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "describe_location", "arguments": {"identifier": "USGS-07374000"}},
        },
        {"Mcp-Session-Id": session_id},
    )
    assert status == 200
    assert body["result"]["structuredContent"]["data"]["gage_datum"]


def test_two_conversations_do_not_share_a_manifest(endpoint):
    """The property stdio cannot offer, and the reason a session owns a thread."""
    first, _ = initialise(endpoint)
    second, _ = initialise(endpoint)

    ask(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_latest", "arguments": {"identifier": "USGS-07374000"}},
        },
        {"Mcp-Session-Id": first},
    )

    def retrievals(session_id):
        _, _, body = ask(
            endpoint,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "export_manifest", "arguments": {}}},
            {"Mcp-Session-Id": session_id},
        )
        return body["result"]["structuredContent"]["data"]["retrievals"]

    assert retrievals(first)
    assert retrievals(second) == []


def test_a_notification_is_accepted_without_an_answer(endpoint):
    session_id, _ = initialise(endpoint)
    status, _, body = ask(
        endpoint,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"Mcp-Session-Id": session_id},
    )
    assert (status, body) == (202, None)


def test_an_origin_off_the_list_is_refused_before_the_body_is_read(endpoint):
    """DNS rebinding: a page the user did not write must not drive a local server."""
    session_id, _ = initialise(endpoint)
    status, _, _ = ask(
        endpoint,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Mcp-Session-Id": session_id, "Origin": "https://not-the-user.example"},
    )
    assert status == 403


def test_a_browser_on_loopback_is_allowed(endpoint):
    session_id, _ = initialise(endpoint)
    status, _, _ = ask(
        endpoint,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Mcp-Session-Id": session_id, "Origin": "http://localhost:3000"},
    )
    assert status == 200


def test_a_client_without_an_origin_is_allowed():
    """A command-line client sends none, and refusing those refuses every non-browser."""
    assert origin_is_allowed(None, frozenset({"localhost"}))
    assert not origin_is_allowed("https://elsewhere.example", frozenset({"localhost"}))
    assert origin_is_allowed("https://elsewhere.example", frozenset({"*"}))


def test_an_unsupported_protocol_version_is_refused(endpoint):
    session_id, _ = initialise(endpoint)
    status, _, _ = ask(
        endpoint,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Mcp-Session-Id": session_id, "MCP-Protocol-Version": "1999-01-01"},
    )
    assert status == 400


def test_no_event_stream_is_offered(endpoint):
    session_id, _ = initialise(endpoint)
    status, headers, _ = ask(endpoint, None, {"Mcp-Session-Id": session_id}, method="GET")
    assert status == 405
    assert "POST" in headers["Allow"]


def test_a_session_is_closed_on_delete_and_then_unknown(endpoint):
    session_id, _ = initialise(endpoint)
    assert ask(endpoint, None, {"Mcp-Session-Id": session_id}, method="DELETE")[0] == 204
    assert ask(endpoint, None, {"Mcp-Session-Id": session_id}, method="DELETE")[0] == 404


def test_another_path_serves_nothing(endpoint):
    status, _, _ = ask(endpoint.replace("/mcp", "/"), {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 404


def test_a_malformed_body_is_a_parse_error_not_a_crash(endpoint):
    request = urllib.request.Request(
        endpoint, b"{not json", {"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert json.loads(caught.value.read())["error"]["code"] == -32700


def test_batching_is_refused_since_the_protocol_withdrew_it(endpoint):
    status, _, body = ask(endpoint, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert status == 400
    assert "batching" in body["error"]["message"]


def test_sessions_are_bounded_so_a_long_lived_server_does_not_grow():
    sessions = Sessions(lambda: Server(session_factory=offline), limit=2)
    first, _ = sessions.open()
    sessions.open()
    sessions.open()
    assert len(sessions) == 2
    assert sessions.get(first) is None
    sessions.close_all()
    assert len(sessions) == 0
