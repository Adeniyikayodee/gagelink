"""The MCP surface: the protocol, the schemas, and how a failure travels."""

import io
import json
from pathlib import Path

import pytest

from gagelink import Service, Session
from gagelink.nldi import Network
from gagelink.nwps import Forecasts
from gagelink.server import PROTOCOL_VERSION, TOOLS, Server, serve_stdio

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
    assert result["protocolVersion"] == PROTOCOL_VERSION
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
    manifest = text(server.call_tool("export_manifest", {}))

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
    assert text(server.call_tool("export_manifest", {}))["retrievals"]

    dispatch(server, "initialize", {})
    assert text(server.call_tool("export_manifest", {}))["retrievals"] == []


def test_the_result_is_held_to_the_budget():
    """Set low, the trimming shows up in the payload the client receives."""
    server = Server(session_factory=offline, budget_tokens=60)
    body = text(server.call_tool("get_latest", {"identifier": "USGS-07374000"}))
    assert any("token budget" in note for note in body["notes"])
