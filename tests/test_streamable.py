"""The Streamable HTTP transport: sessions, origins, and the methods it answers."""

import base64
import json
import threading
import urllib.error
import urllib.request

import pytest

from gagelink.protocol import CONVERSATION_KEY, MODERN_VERSION
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


# The 2026-07-28 revision ----------------------------------------------------------------------
#
# This transport changed as well as the messages on it: no session to mint, no GET stream to
# open, and headers mirroring fields out of the body so an intermediary can route without
# parsing it. Those headers are checked against the body here, which is what stops a request
# being routed on one value and answered on another.


def modern(method, name=None, version=MODERN_VERSION, conversation=None):
    """The body and headers of one modern request, which carry the same facts twice."""
    fields = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    if conversation is not None:
        fields[CONVERSATION_KEY] = conversation
    params = {"_meta": fields}
    if method == "tools/call":
        params |= {"name": name, "arguments": {"identifier": "USGS-07374000"}}
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers


def test_a_modern_request_is_answered_without_a_session_being_minted(endpoint):
    """There is nothing left to initialise. A client that has never spoken to this endpoint
    calls a tool on its first request."""
    body, headers = modern("tools/call", "describe_location")
    status, sent, answer = ask(endpoint, body, headers)
    assert status == 200
    assert "Mcp-Session-Id" not in sent
    assert answer["result"]["resultType"] == "complete"
    assert json.loads(answer["result"]["content"][0]["text"])["data"]["gage_datum"]


def test_discover_is_answered_on_a_cold_endpoint(endpoint):
    body, headers = modern("server/discover")
    status, _, answer = ask(endpoint, body, headers)
    assert status == 200
    assert MODERN_VERSION in answer["result"]["supportedVersions"]


@pytest.mark.parametrize("dropped", ["Mcp-Method", "MCP-Protocol-Version", "Mcp-Name"])
def test_a_required_mirrored_header_that_is_missing_is_a_mismatch(endpoint, dropped):
    """Absence is a mismatch as much as disagreement is: a request without these is one an
    intermediary could not have routed, which makes it indistinguishable from one routed on
    something else."""
    body, headers = modern("tools/call", "describe_location")
    del headers[dropped]
    status, _, answer = ask(endpoint, body, headers)
    assert status == 400
    assert answer["error"]["code"] == -32020


def test_a_header_that_disagrees_with_the_body_is_refused_rather_than_resolved(endpoint):
    """Picking either value is a guess about whether the thing that routed the request or
    the thing about to answer it was right."""
    body, headers = modern("tools/call", "describe_location")
    headers["Mcp-Name"] = "get_latest"
    status, _, answer = ask(endpoint, body, headers)
    assert status == 400
    assert answer["error"]["code"] == -32020
    assert "Mcp-Name" in answer["error"]["message"]


def test_a_version_header_that_disagrees_with_the_body_is_refused(endpoint):
    body, headers = modern("tools/list")
    headers["MCP-Protocol-Version"] = "2025-06-18"
    status, _, answer = ask(endpoint, body, headers)
    assert status == 400
    assert answer["error"]["code"] == -32020


def test_a_name_the_client_had_to_encode_is_decoded_before_it_is_compared(endpoint):
    """A tool name is only advised to stay inside the characters a header may carry, so the
    transport defines an encoding for the ones that do not. Comparing without decoding would
    make every encoded name look like a mismatch."""
    body, headers = modern("tools/call", "describe_location")
    encoded = base64.b64encode(b"describe_location").decode()
    headers["Mcp-Name"] = f"=?base64?{encoded}?="
    status, _, answer = ask(endpoint, body, headers)
    assert status == 200
    assert json.loads(answer["result"]["content"][0]["text"])["ok"] is True


def test_a_revision_this_server_does_not_speak_is_a_400_naming_the_ones_it_does(endpoint):
    """A dual-era client reads the body of exactly this response to decide whether to retry
    at another version or fall back to the handshake."""
    body, headers = modern("tools/list", version="2030-01-01")
    status, _, answer = ask(endpoint, body, headers)
    assert status == 400
    assert answer["error"]["code"] == -32022
    assert MODERN_VERSION in answer["error"]["data"]["supported"]


def test_an_unimplemented_method_is_a_404_carrying_a_json_rpc_error(endpoint):
    """The body is what separates this from the 404 of a server that does not host a modern
    endpoint at all."""
    body, headers = modern("resources/subscribe")
    status, _, answer = ask(endpoint, body, headers)
    assert status == 404
    assert answer["error"]["code"] == -32601


def test_a_session_header_from_an_older_client_is_ignored_rather_than_refused(endpoint):
    """The revision says to ignore it and to mint none in return, which is what lets a
    client that is half-migrated keep working."""
    body, headers = modern("tools/list")
    status, sent, answer = ask(endpoint, body, headers | {"Mcp-Session-Id": "a" * 32})
    assert status == 200
    assert "Mcp-Session-Id" not in sent
    assert answer["result"]["tools"]


def test_named_conversations_do_not_meet_over_one_endpoint(endpoint):
    """One process serving two people without either one's manifest picking up the other's
    retrievals, which is now the client's name for the conversation rather than a session
    the server handed out."""
    body, headers = modern("tools/call", "get_latest", conversation="mine")
    ask(endpoint, body, headers)

    body, headers = modern("tools/call", "export_manifest", conversation="mine")
    body["params"]["arguments"] = {}
    _, _, mine = ask(endpoint, body, headers)

    body, headers = modern("tools/call", "export_manifest", conversation="theirs")
    body["params"]["arguments"] = {}
    _, _, theirs = ask(endpoint, body, headers)

    assert json.loads(mine["result"]["content"][0]["text"])["data"]["retrievals"]
    assert json.loads(theirs["result"]["content"][0]["text"])["data"]["retrievals"] == []


def test_the_handshake_still_works_alongside_it(endpoint):
    """The dual-era row of the compatibility matrix, and the one that keeps every client
    already configured against this server answering."""
    session_id, result = initialise(endpoint)
    assert result["protocolVersion"] == "2025-06-18"

    status, _, answer = ask(
        endpoint,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Mcp-Session-Id": session_id},
    )
    assert status == 200
    assert "resultType" not in answer["result"]


def test_an_oversized_body_gets_the_refusal_and_not_a_broken_pipe(endpoint):
    """Found by a black-box client in a container. The server answers 413 without reading
    the body, which is the point of checking the length first, but a keep-alive connection
    with an unread body on it makes the client see a transport failure instead of the
    explanation. The connection is closed so the 413 arrives."""
    oversized = b"{" + b"x" * (2 << 20)
    request = urllib.request.Request(
        endpoint, oversized, {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, dict(exc.headers)

    assert status == 413
    assert headers.get("Connection", "").lower() == "close"
