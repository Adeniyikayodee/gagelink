"""The MCP surface: the protocol, the schemas, and how a failure travels."""

import io
import json
from pathlib import Path

import pytest

from gagelink import Service, Session
from gagelink.nldi import Network
from gagelink.nwps import Forecasts
from gagelink.results import ErrorCode
from gagelink.schema import validate
from gagelink.server import TOOLS, Server, serve_stdio
from gagelink.protocol import LEGACY_PROTOCOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
LOCATION = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())
LATEST = json.loads((FIXTURES / "latest_continuous_07374000.json").read_text())
GAUGE = json.loads((FIXTURES / "nwps_gauge_01646500.json").read_text())


def fetch(url, headers):
    page = LOCATION if "monitoring-locations" in url else LATEST
    return 200, {"X-RateLimit-Remaining": "995"}, json.dumps(page)


def gauge_fetch(url, headers):
    return 200, {}, json.dumps(GAUGE)


def offline():
    return Session(
        service=Service(fetch=fetch),
        forecasts=Forecasts(fetch=gauge_fetch),
        network=Network(fetch=gauge_fetch),
    )


@pytest.fixture
def server():
    return Server(session_factory=offline)


def text(response):
    return json.loads(response["content"][0]["text"])


# Schemas ----------------------------------------------------------------------------------


#: The ceiling the surface is designed to. A model degrades as its tool list grows, so the
#: surface is organised by verb and the choice of which service answers is made by the
#: server. Passing this is a design decision to take deliberately, not a number to raise.
TOOL_BUDGET = 14


def test_the_tool_list_stays_inside_its_budget():
    """Organised by verb rather than by agency: five services behind twelve tools, where
    mirroring their APIs would have cost several times that."""
    assert len(TOOLS) <= TOOL_BUDGET


def test_every_advertised_tool_can_actually_be_called(server):
    """A schema for a method that does not exist is worse than a missing tool, because the
    model will spend a turn discovering it."""
    for tool in TOOLS:
        name = tool["name"]
        assert name == "export_manifest" or hasattr(server.toolkit, name)


def test_every_tool_declares_a_schema_and_a_description():
    for tool in TOOLS:
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"


def test_the_descriptions_state_the_hazards_rather_than_only_the_arguments():
    """Declaring physical metadata in the schema without enforcing it still recovered a
    third of the failing runs in the quantity-guard evaluation, so what these say does
    work before any validation runs."""
    described = {t["name"]: t["description"] for t in TOOLS}
    assert "datum" in described["describe_location"]
    assert "not the same as current" in described["get_latest"]
    assert "surveyed elevation" in described["get_forecast"]
    assert "computed" in described["get_basin"]


# Protocol ----------------------------------------------------------------------------------


def test_initialize_announces_the_server_and_the_protocol(server):
    from gagelink.server import dispatch

    result = dispatch(server, "initialize", {})
    assert result["protocolVersion"] == LEGACY_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "gagelink"
    assert "tools" in result["capabilities"]


def test_an_unknown_method_is_a_method_not_found_rather_than_a_server_fault():
    """A client is meant to be able to probe for a capability, and -32603 reads as the
    server having broken."""
    exchange = serve(['{"jsonrpc":"2.0","id":1,"method":"resources/list"}'])
    assert exchange[0]["error"]["code"] == -32601


def test_a_notification_is_not_answered():
    """A message with no id expects no reply, and answering it corrupts the stream."""
    assert serve(['{"jsonrpc":"2.0","method":"notifications/initialized"}']) == []


def test_a_malformed_line_does_not_end_the_server():
    exchange = serve(["not json", '{"jsonrpc":"2.0","id":2,"method":"ping"}'])
    assert len(exchange) == 1
    assert exchange[0]["result"] == {}


def serve(lines):
    out = io.StringIO()
    serve_stdio(Server(session_factory=offline), io.StringIO("\n".join(lines)), out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


# Calling ------------------------------------------------------------------------------------


def test_a_tool_result_arrives_as_text_content(server):
    body = text(server.call_tool("describe_location", {"identifier": "USGS-07374000"}))
    assert body["ok"] is True
    assert body["data"]["gage_datum"] == "GAGE:07374000"


def test_a_failure_is_content_marked_in_error_rather_than_a_protocol_fault(server):
    """Which is what keeps the repair in front of the model instead of ending the turn."""
    response = server.call_tool("find_locations", {})
    assert response["isError"] is True
    assert "Supply at least one of" in text(response)["repair"]


def test_wrong_arguments_come_back_to_the_model_rather_than_raising(server):
    response = server.call_tool("describe_location", {"nonsense": 1})
    assert response["isError"] is True
    assert "describe_location" in response["content"][0]["text"]


def test_an_unknown_tool_is_refused_by_name(server):
    response = server.call_tool("drop_database", {})
    assert response["isError"] is True


def test_a_private_attribute_is_not_reachable_as_a_tool(server):
    """The dispatch is by name against the advertised list, not by attribute lookup."""
    response = server.call_tool("_summarise", {})
    assert response["isError"] is True


def test_the_manifest_is_a_tool_so_a_model_can_be_told_to_end_with_it(server):
    server.call_tool("get_latest", {"identifier": "USGS-07374000"})
    manifest = text(server.call_tool("export_manifest", {}))["data"]

    assert [r["collection"] for r in manifest["retrievals"]] == [
        "monitoring-locations",
        "latest-continuous",
    ]
    assert manifest["quantities"]


def test_initialising_again_clears_the_previous_conversation(server):
    """A long-lived process must not carry one conversation's quantities into the next
    one's manifest."""
    from gagelink.server import dispatch

    server.call_tool("get_latest", {"identifier": "USGS-07374000"})
    assert text(server.call_tool("export_manifest", {}))["data"]["retrievals"]

    dispatch(server, "initialize", {})
    assert text(server.call_tool("export_manifest", {}))["data"]["retrievals"] == []


def test_the_result_is_held_to_the_budget():
    """Set low, the trimming shows up in the payload the client receives."""
    server = Server(session_factory=offline, budget_tokens=60)
    body = text(server.call_tool("get_latest", {"identifier": "USGS-07374000"}))
    assert any("token budget" in note for note in body["notes"])


# Discoverability -----------------------------------------------------------------------------


def test_the_handshake_tells_the_model_what_this_is_for():
    """The one piece of text that reaches every conversation, which clients put in front of
    the model before it calls anything."""
    from gagelink.server import INSTRUCTIONS, dispatch

    result = dispatch(Server(session_factory=offline), "initialize", {})
    assert result["instructions"] == INSTRUCTIONS
    assert result["serverInfo"]["title"]


def test_the_instructions_carry_the_errors_rather_than_a_description_of_the_software():
    """Each of these was measured or observed, and each decides whether an answer is
    right: the datum comparison seven of eleven models got wrong, a payload holding a
    discharge from this morning beside a turbidity from 2019, and four spellings of one
    unit across two agencies."""
    from gagelink.server import INSTRUCTIONS

    assert "own datum" in INSTRUCTIONS and "describe_location" in INSTRUCTIONS
    assert "Latest is not current" in INSTRUCTIONS
    assert "Modelled is not measured" in INSTRUCTIONS
    assert "USGS-01646500" in INSTRUCTIONS
    assert "unavailable" in INSTRUCTIONS


def test_the_instructions_stay_small_enough_to_send_every_time():
    """They are prepended to every conversation, so their length is a cost paid on every
    question rather than once."""
    from gagelink.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) < 3000


# Failure, and what a client can rely on -------------------------------------------------------


def test_a_fault_inside_a_tool_is_a_failure_and_not_a_protocol_error(server, monkeypatch):
    """A field renamed upstream is the likeliest failure this package will ever see.

    Ending the turn on it loses the repair, the quota count, and any chance the model has
    of saying what went wrong, so it comes back as a result like any other failure.
    """
    monkeypatch.delenv("GAGELINK_RAISE", raising=False)

    def renamed(*args, **kwargs):
        raise KeyError("properties")

    monkeypatch.setattr(server.session, "items", renamed)
    response = server.call_tool("get_latest", {"identifier": "USGS-07374000"})

    assert response["isError"] is True
    body = text(response)
    assert body["error"] == "INTERNAL_ERROR"
    assert "KeyError" in body["message"]
    assert "from memory" in body["repair"]


def test_a_fault_can_be_made_to_raise_so_the_suite_still_sees_it(server, monkeypatch):
    """The catch-all must not hide a bug from the tests that exist to find it."""
    monkeypatch.setenv("GAGELINK_RAISE", "1")

    def renamed(*args, **kwargs):
        raise KeyError("properties")

    monkeypatch.setattr(server.session, "items", renamed)
    with pytest.raises(KeyError):
        server.call_tool("get_latest", {"identifier": "USGS-07374000"})


def test_an_argument_the_schema_does_not_name_is_reported_against_the_schema(server):
    body = text(server.call_tool("describe_location", {"identifier": "USGS-07374000", "depth": 3}))
    assert body["error"] == "INVALID_ARGUMENTS"
    assert "depth is not an argument" in body["message"]


def test_a_missing_required_argument_says_which(server):
    body = text(server.call_tool("describe_location", {}))
    assert body["error"] == "INVALID_ARGUMENTS"
    assert "identifier is required" in body["message"]


def test_an_argument_of_the_wrong_type_says_what_was_wanted(server):
    body = text(server.call_tool("get_peaks", {"identifier": "USGS-07374000", "limit": "ten"}))
    assert body["error"] == "INVALID_ARGUMENTS"
    assert "should be integer" in body["message"]


def test_an_unknown_tool_names_the_ones_that_exist(server):
    body = text(server.call_tool("drop_database", {}))
    assert body["error"] == "INVALID_ARGUMENTS"
    assert "describe_location" in body["repair"]


# The declared contract ------------------------------------------------------------------------


def test_every_tool_declares_a_title_an_output_schema_and_annotations():
    for tool in TOOLS:
        assert tool["title"], tool["name"]
        assert tool["outputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False


def test_nothing_here_writes_so_a_client_has_one_thing_to_ask_about():
    """Thirteen read-only tools should cost a user one consent, not thirteen prompts."""
    assert all(t["annotations"]["readOnlyHint"] for t in TOOLS)
    manifest = next(t for t in TOOLS if t["name"] == "export_manifest")
    assert manifest["annotations"]["openWorldHint"] is False


def test_a_result_is_returned_as_data_as_well_as_text(server):
    """The package's claim is that a value carries its frame. Structured output is what
    lets a client read the frame as a field instead of parsing it out of a string."""
    response = server.call_tool("describe_location", {"identifier": "USGS-07374000"})
    assert response["structuredContent"] == json.loads(response["content"][0]["text"])
    assert response["structuredContent"]["data"]["gage_datum"]


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("describe_location", {"identifier": "USGS-07374000"}),
        ("get_latest", {"identifier": "USGS-07374000"}),
        ("export_manifest", {}),
        ("describe_location", {}),  # a failure has to conform to the schema too
    ],
)
def test_what_a_tool_returns_matches_what_it_declared(server, name, arguments):
    declared = next(t for t in TOOLS if t["name"] == name)
    response = server.call_tool(name, arguments)
    assert validate(response["structuredContent"], declared["outputSchema"]) == []


def test_every_error_code_is_declared_in_the_output_schema():
    """A code the schema does not list is one a client cannot be written against."""
    declared = next(t for t in TOOLS if t["name"] == "get_latest")
    assert set(declared["outputSchema"]["properties"]["error"]["enum"]) == set(ErrorCode.all())


# Protocol -------------------------------------------------------------------------------------


@pytest.mark.parametrize("asked", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_a_client_is_answered_in_the_revision_it_asked_for(server, asked):
    from gagelink.server import dispatch

    result = dispatch(server, "initialize", {"protocolVersion": asked})
    assert result["protocolVersion"] == asked


def test_a_revision_this_server_does_not_speak_is_answered_in_the_newest(server):
    from gagelink.server import dispatch

    result = dispatch(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert result["protocolVersion"] == LEGACY_PROTOCOL_VERSION


def test_every_tool_conforms_to_its_own_schema_when_it_fails(server):
    """Called with nothing, most of these fail. A failure is the path a client is most
    likely to meet and the one least likely to be checked, so it is checked here."""
    for tool in TOOLS:
        response = server.call_tool(tool["name"], {})
        assert validate(response["structuredContent"], tool["outputSchema"]) == [], tool["name"]
