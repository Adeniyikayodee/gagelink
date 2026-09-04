"""The two revisions of MCP this server speaks, and how it tells them apart.

The 2026-07-28 revision removed the handshake. A server that answers only the older shape
stops answering as its clients update, and one that answers only the newer shape stops
answering the clients that have not, so both are served and the tests below check that
serving one has not changed the other.
"""

import io
import json

import pytest

from gagelink.protocol import (
    CONVERSATION_KEY,
    LEGACY_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSIONS,
    MODERN_VERSION,
    SERVER_INFO_KEY,
    SUPPORTED_VERSIONS,
    MethodNotFound,
    ProtocolError,
    UnsupportedProtocolVersion,
    named_by,
    read_envelope,
)
from gagelink.server import Server, dispatch, serve_stdio
from test_server import offline


@pytest.fixture
def server():
    return Server(session_factory=offline)


def meta(version=MODERN_VERSION, conversation=None, capabilities=None):
    """The per-request metadata a modern client sends, which is all the handshake there is."""
    fields = {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {} if capabilities is None else capabilities,
    }
    if conversation is not None:
        fields[CONVERSATION_KEY] = conversation
    return {"_meta": fields}


def text(response):
    return json.loads(response["content"][0]["text"])


# Which revision -----------------------------------------------------------------------------


def test_a_request_carrying_no_metadata_is_served_by_the_handshake_revisions():
    """The two eras cannot be told apart by method name, since `tools/call` is in both. A
    legacy client's request is a modern one with the metadata missing, so absence decides
    it, and absence has to mean legacy or every handshake client breaks."""
    envelope = read_envelope("tools/list", {})
    assert envelope.modern is False
    assert envelope.version == LEGACY_PROTOCOL_VERSION


def test_a_request_declaring_the_modern_revision_is_served_statelessly():
    envelope = read_envelope("tools/list", meta())
    assert envelope.modern is True
    assert envelope.version == MODERN_VERSION


def test_a_handshake_revision_named_in_per_request_metadata_is_refused():
    """The eras disagree about what a request means. A client mixing them has not said
    which it wants, and guessing is worse than an error naming what is on offer."""
    with pytest.raises(UnsupportedProtocolVersion) as raised:
        read_envelope("tools/list", meta(version="2025-06-18"))
    assert raised.value.data["requested"] == "2025-06-18"


def test_an_unknown_revision_is_refused_with_the_list_that_lets_a_client_recover():
    """There is no handshake left to negotiate in, so the first request's failure has to
    carry enough for the second one to succeed."""
    with pytest.raises(UnsupportedProtocolVersion) as raised:
        read_envelope("tools/list", meta(version="1999-01-01"))
    assert raised.value.code == -32022
    assert raised.value.status == 400
    assert raised.value.data["supported"] == list(SUPPORTED_VERSIONS)
    assert MODERN_VERSION in raised.value.data["supported"]


def test_a_modern_request_that_declares_no_capabilities_is_malformed():
    """A server may not rely on a capability the client did not declare, so a request
    declaring none has not said enough to be answered."""
    params = meta()
    del params["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    with pytest.raises(ProtocolError) as raised:
        read_envelope("tools/list", params)
    assert raised.value.code == -32602


def test_the_newest_revision_is_the_modern_one_and_the_handshake_ones_follow():
    assert SUPPORTED_VERSIONS[0] == MODERN_VERSION
    assert SUPPORTED_VERSIONS[1:] == LEGACY_PROTOCOL_VERSIONS


@pytest.mark.parametrize(
    "method, params, expected",
    [
        ("tools/call", {"name": "get_latest"}, "get_latest"),
        ("resources/read", {"uri": "file:///x"}, "file:///x"),
        ("prompts/get", {"name": "p"}, "p"),
        ("tools/list", {}, None),
    ],
)
def test_the_mirrored_name_is_read_from_where_each_method_keeps_it(method, params, expected):
    assert named_by(method, params) == expected


# Discovery ------------------------------------------------------------------------------------


def test_discover_answers_before_any_version_has_been_agreed(server):
    """The probe a dual-era client sends on stdio to find out which era it reached, and the
    only way a modern client learns what this server speaks without failing first."""
    result = dispatch(server, "server/discover", meta())
    assert result["supportedVersions"] == list(SUPPORTED_VERSIONS)
    assert "tools" in result["capabilities"]
    assert result["_meta"][SERVER_INFO_KEY]["name"] == "gagelink"


def test_discover_carries_the_instructions_the_handshake_used_to(server):
    """The one piece of text that reaches every conversation. Losing it in the new revision
    would cost every answer the datum rule and the four spellings of discharge."""
    from gagelink.server import INSTRUCTIONS

    assert dispatch(server, "server/discover", meta())["instructions"] == INSTRUCTIONS


def test_discover_says_how_long_it_may_be_held(server):
    """Nothing in the answer changes while the process runs, and the revision replaced
    long-lived discovery streams with a cache lifetime."""
    result = dispatch(server, "server/discover", meta())
    assert result["ttlMs"] > 0
    assert result["cacheScope"] == "public"


# The shape of a modern result -------------------------------------------------------------------


def test_a_modern_result_says_it_is_finished_and_who_answered_it(server):
    result = dispatch(server, "tools/list", meta())
    assert result["resultType"] == "complete"
    assert result["_meta"][SERVER_INFO_KEY]["version"]


def test_a_handshake_result_is_left_exactly_as_it_was(server):
    """A client written against the older revision does not know these fields. Adding them
    is harmless by the specification and pointless in fact, and leaving them off keeps the
    older answer byte for byte what it was."""
    result = dispatch(server, "tools/list", {})
    assert "resultType" not in result
    assert "_meta" not in result


def test_a_tool_call_answers_in_either_revision_with_the_same_payload(server):
    modern = dispatch(
        server,
        "tools/call",
        {"name": "describe_location", "arguments": {"identifier": "USGS-07374000"}, **meta()},
    )
    legacy = dispatch(
        server,
        "tools/call",
        {"name": "describe_location", "arguments": {"identifier": "USGS-07374000"}},
    )
    assert modern["structuredContent"] == legacy["structuredContent"]
    assert modern["resultType"] == "complete"


def test_initialize_is_never_answered_in_a_revision_that_has_no_handshake(server):
    """A client reaching `initialize` is legacy by the act of sending it, and answering it
    with a version that removed the method would leave it holding a contradiction."""
    for asked in (MODERN_VERSION, "1999-01-01", None):
        result = dispatch(server, "initialize", {"protocolVersion": asked} if asked else {})
        assert result["protocolVersion"] in LEGACY_PROTOCOL_VERSIONS


def test_an_unknown_method_is_a_404_rather_than_a_fault(server):
    """404 with -32601 in the body is what separates an unimplemented method from an
    endpoint that is not there, which is the difference a dual-era client is reading for.

    `resources/subscribe` because the capability declaration says subscribe is false, so
    this is a method a client can be expected to probe for and find absent."""
    with pytest.raises(MethodNotFound) as raised:
        dispatch(server, "resources/subscribe", meta())
    assert raised.value.code == -32601
    assert raised.value.status == 404


# Conversations ----------------------------------------------------------------------------------


def test_two_named_conversations_keep_their_ledgers_apart(server):
    """The revision is explicit that a connection is not a conversation and that a client
    may interleave unrelated work on one process. A manifest that spans a conversation
    therefore cannot be scoped to the process."""
    dispatch(
        server,
        "tools/call",
        {"name": "get_latest", "arguments": {"identifier": "USGS-07374000"}, **meta(conversation="a")},
    )
    mine = dispatch(server, "tools/call", {"name": "export_manifest", "arguments": {}, **meta(conversation="a")})
    theirs = dispatch(server, "tools/call", {"name": "export_manifest", "arguments": {}, **meta(conversation="b")})

    assert text(mine)["data"]["retrievals"]
    assert text(theirs)["data"]["retrievals"] == []


def test_a_conversation_that_names_itself_the_same_twice_is_the_same_one(server):
    dispatch(
        server,
        "tools/call",
        {"name": "get_latest", "arguments": {"identifier": "USGS-07374000"}, **meta(conversation="a")},
    )
    again = dispatch(server, "tools/call", {"name": "export_manifest", "arguments": {}, **meta(conversation="a")})
    assert text(again)["data"]["retrievals"]


def test_a_client_that_names_no_conversation_shares_the_default(server):
    """The same arrangement a single-conversation client had before, kept as a stated
    fallback rather than as an assumption about how the process is used."""
    dispatch(
        server,
        "tools/call",
        {"name": "get_latest", "arguments": {"identifier": "USGS-07374000"}, **meta()},
    )
    assert text(server.call_tool("export_manifest", {}))["data"]["retrievals"]


def test_the_number_of_conversations_a_process_holds_is_bounded(server):
    """Nothing in the modern revision ends a conversation: no handshake to reset on and no
    close to wait for. Unbounded, the ledgers would grow until the process died."""
    from gagelink.server import MAX_CONVERSATIONS

    for n in range(MAX_CONVERSATIONS + 5):
        server.conversation(f"conversation-{n}")
    assert len(server._named) == MAX_CONVERSATIONS


def test_closing_the_server_closes_every_ledger_it_opened(server):
    server.conversation("a")
    server.conversation("b")
    server.close()
    assert server._named == {}
    assert server.session is None


# Over stdio ---------------------------------------------------------------------------------------


def serve(lines):
    out = io.StringIO()
    serve_stdio(Server(session_factory=offline), io.StringIO("\n".join(lines)), out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_modern_client_probes_with_discover_and_gets_an_answer():
    """The stdio fallback rule: a `DiscoverResult` means modern, a recognised modern error
    means modern at another version, and anything else means fall back to the handshake."""
    exchange = serve(
        [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": meta()})]
    )
    assert exchange[0]["result"]["supportedVersions"][0] == MODERN_VERSION


def test_a_probe_at_a_version_this_server_does_not_speak_names_the_ones_it_does():
    exchange = serve(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "server/discover",
                    "params": meta(version="2030-01-01"),
                }
            )
        ]
    )
    error = exchange[0]["error"]
    assert error["code"] == -32022
    assert MODERN_VERSION in error["data"]["supported"]


def test_a_modern_tool_call_works_over_stdio_without_any_handshake():
    exchange = serve(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "describe_location",
                        "arguments": {"identifier": "USGS-07374000"},
                        **meta(),
                    },
                }
            )
        ]
    )
    body = json.loads(exchange[0]["result"]["content"][0]["text"])
    assert body["data"]["gage_datum"] == "GAGE:07374000"


def test_the_handshake_still_opens_a_conversation_over_stdio():
    """The compatibility matrix calls this the dual-era row, and it is the one that keeps
    every client already configured against this server working."""
    exchange = serve(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
    )
    assert exchange[0]["result"]["protocolVersion"] == LEGACY_PROTOCOL_VERSION
    assert exchange[1]["result"]["tools"]
    assert "resultType" not in exchange[1]["result"]


def test_valid_json_that_is_not_a_message_does_not_end_the_process():
    """Found by a black-box client in a container, not by this suite. A bare list, a bare
    null, a bare number: each is valid JSON, each raises on the first field read, and a
    raise in the read loop ends the process. Batching was part of 2024-11-05, which this
    server still answers, so an old client sending one would take it down instead of being
    told the protocol withdrew it."""
    lines = ["[]", "null", "123", '"a string"']
    lines.append(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
    exchange = serve(lines)

    assert [m["error"]["code"] for m in exchange[:4]] == [-32600] * 4
    assert all(m["id"] is None for m in exchange[:4])
    assert exchange[4]["result"] == {}, "the server stopped answering after the garbage"
