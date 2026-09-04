"""The surface besides the tools: prompts, resources, templates, and completion.

A tool list says what can be called. These say what the server is for, which is the part a
client shows before anything is called and the part a model reaches for when its client
did not surface the server instructions at all. They are checked here for the same reason
the tool schemas are: what they claim has to be true of what the code does, and both of
them are read by something that cannot ask a follow-up question.
"""

import json
from pathlib import Path

import pytest

from gagelink import Service, Session
from gagelink.catalogue import (
    CONVERTIBLE_DATUMS,
    COVERAGE,
    PROMPTS,
    RENDERERS,
    RESOURCE_TEMPLATES,
    RESOURCES,
    SCHEME,
    UnknownPrompt,
    UnknownResource,
    complete,
    prompt,
)
from gagelink.protocol import (
    CLIENT_CAPABILITIES_KEY,
    MODERN_VERSION,
    PROTOCOL_VERSION_KEY,
    MalformedRequest,
)
from gagelink.server import CAPABILITIES, INSTRUCTIONS, TOOLS, Server, dispatch
from gagelink.vdatum import DESCRIPTIONS


FIXTURES = Path(__file__).parent / "fixtures"
LOCATION = (FIXTURES / "monitoring_location_01646500.json").read_text()


def offline():
    return Session(service=Service(fetch=lambda url, headers: (200, {}, LOCATION)))


@pytest.fixture
def server():
    return Server(session_factory=offline)


def modern(params=None):
    """A request in the revision that carries its own version and capabilities."""
    return {
        **(params or {}),
        "_meta": {PROTOCOL_VERSION_KEY: MODERN_VERSION, CLIENT_CAPABILITIES_KEY: {}},
    }


# Capabilities -----------------------------------------------------------------------------


def test_both_revisions_declare_the_same_capabilities():
    """The modern revision has no handshake to have declared them at, so `server/discover`
    has to carry what `initialize` carries or a client learns less by upgrading."""
    server = Server(session_factory=offline)
    handshake = dispatch(server, "initialize", {})["capabilities"]
    discovered = dispatch(server, "server/discover", modern())["capabilities"]
    assert handshake == discovered == CAPABILITIES


def test_nothing_is_declared_that_is_not_implemented(server):
    """A capability is a promise a client will hold the server to. Each of these is
    answered below; the ones deliberately not implemented are declared false."""
    assert set(CAPABILITIES) == {"tools", "prompts", "resources", "completions"}
    assert CAPABILITIES["resources"]["subscribe"] is False
    for surface in ("tools", "prompts", "resources"):
        assert CAPABILITIES[surface]["listChanged"] is False


# Prompts ----------------------------------------------------------------------------------


def test_every_declared_prompt_can_be_rendered():
    """A prompt a client can list and cannot get is worse than one that was never
    offered, because the failure lands after the person has chosen it."""
    for declared in PROMPTS:
        assert declared["name"] in RENDERERS
    assert set(RENDERERS) == {p["name"] for p in PROMPTS}


def test_a_prompt_renders_with_only_its_required_arguments():
    """The optional ones have defaults in the renderer rather than in the client, so a
    client that omits them still sends a complete instruction."""
    rendered = prompt(
        "freeboard_check",
        {"identifier": "USGS-01646500", "crest_elevation": "41 ft"},
    )
    text = rendered["messages"][0]["content"]["text"]
    assert "USGS-01646500" in text and "41 ft" in text
    assert "NAVD88" in text


def test_the_freeboard_prompt_orders_the_offset_before_the_subtraction():
    """This is the whole content of the prompt. A sequence that fetches the offset after
    differencing is the error the package exists to prevent, written out as a procedure."""
    text = prompt(
        "freeboard_check",
        {"identifier": "USGS-01646500", "crest_elevation": "41 ft"},
    )["messages"][0]["content"]["text"]
    assert text.index("describe_location") < text.index("Subtract")
    assert "offset_uncertainty" in text


def test_the_station_prompt_names_the_filter_each_agency_matches_on():
    """The three networks disagree about what a searchable name is, and the wrong filter
    returns an empty list rather than an error, which reads as absence of coverage."""
    uk = prompt("find_a_station", {"place": "Oxford", "country": "GB"})
    assert "River Thames" in uk["messages"][0]["content"]["text"]
    france = prompt("find_a_station", {"place": "Paris", "country": "FR"})
    assert "La Seine" in france["messages"][0]["content"]["text"]


def test_an_unknown_prompt_is_refused_by_name():
    with pytest.raises(UnknownPrompt):
        prompt("no_such_prompt", {})


def test_the_server_answers_a_prompt_and_names_the_rest_when_it_cannot(server):
    listed = dispatch(server, "prompts/list", modern())["prompts"]
    assert [p["name"] for p in listed] == [p["name"] for p in PROMPTS]

    with pytest.raises(MalformedRequest) as raised:
        dispatch(server, "prompts/get", modern({"name": "nope"}))
    assert "freeboard_check" in raised.value.message


# Resources --------------------------------------------------------------------------------


def read(uri, server):
    return read_for(uri, server, None)


def read_for(uri, server, conversation):
    body = server.read_resource(uri, conversation)["contents"][0]
    return json.loads(body["text"]) if body["mimeType"] == "application/json" else body["text"]


def test_every_listed_resource_can_be_read(server):
    """The list is what a client caches. A URI in it that does not resolve is a broken
    link the client has already shown to somebody."""
    for declared in RESOURCES:
        assert server.read_resource(declared["uri"])["contents"][0]["uri"] == declared["uri"]


def test_the_instructions_resource_is_the_text_the_handshake_sends(server):
    """A client is not obliged to surface what a server sends at discovery, and the four
    rules in it are what decide whether an answer is right. This is the one form a model
    can ask for by name."""
    assert read(f"{SCHEME}instructions", server) == INSTRUCTIONS


def test_the_datum_resource_agrees_with_what_the_converter_will_accept():
    """A datum described here as reachable that the converter would refuse is worse than
    one left out, because the refusal arrives after the request is spent."""
    server = Server(session_factory=offline)
    datums = read(f"{SCHEME}datums", server)
    assert datums["convertible_onto"] == CONVERTIBLE_DATUMS
    for name, entry in datums["datums"].items():
        assert entry["convertible_onto"] == (name in CONVERTIBLE_DATUMS)
    assert set(datums["datums"]) == set(DESCRIPTIONS)


def test_the_coverage_resource_names_only_tools_that_exist():
    """The per-country tool lists are read to choose a call. A name in one that is not a
    tool sends the model at a method that does not exist."""
    declared = {t["name"] for t in TOOLS}
    for network in COVERAGE.values():
        for name in network["tools"]:
            assert name in declared, name


def test_the_coverage_resource_states_what_each_service_leaves_out():
    """The gap is the part a model cannot infer. A UK station answering three tools and
    not the other ten is a fact about the agency, not a failure of the request."""
    assert set(COVERAGE["GB"]["tools"]) < set(COVERAGE["US"]["tools"])
    assert set(COVERAGE["FR"]["tools"]) < set(COVERAGE["US"]["tools"])
    for network in COVERAGE.values():
        assert network["notes"]


def test_the_manifest_resource_is_the_ledger_the_tool_would_have_returned(server):
    """Readable without spending a tool call, and scoped to the conversation that asked."""
    manifest = read(f"{SCHEME}manifest", server)
    assert "retrievals" in manifest and "versions" in manifest


def test_a_named_conversation_reads_its_own_manifest(server):
    """The modern revision forbids reading a conversation out of the connection, so a
    resource read has to be scoped by the name on the request the way a tool call is. One
    conversation's retrievals appearing in another's manifest is the failure this
    prevents."""
    server.call_tool("describe_location", {"identifier": "USGS-01646500"}, conversation="one")
    one = read_for(f"{SCHEME}manifest", server, "one")
    two = read_for(f"{SCHEME}manifest", server, "two")
    assert one["retrievals"] and not two["retrievals"]
    assert one["locations"] and not two["locations"]


def test_a_template_resolves_one_member_of_its_vocabulary(server):
    assert read(f"{SCHEME}parameter/00065", server)["name"].startswith("gage height")
    assert read(f"{SCHEME}datum/navd88", server)["datum"] == "NAVD88"


def test_a_resource_that_is_not_there_is_refused_with_the_ones_that_are(server):
    with pytest.raises(UnknownResource):
        server.read_resource(f"{SCHEME}nothing")
    with pytest.raises(MalformedRequest) as raised:
        dispatch(server, "resources/read", modern({"uri": f"{SCHEME}nothing"}))
    assert f"{SCHEME}parameters" in raised.value.message


def test_every_template_variable_is_one_the_reader_resolves(server):
    """A template is an invitation to construct a URI. One whose variable the reader does
    not understand invites a request that cannot succeed."""
    for template in RESOURCE_TEMPLATES:
        assert template["uriTemplate"].startswith(SCHEME)
        assert template["uriTemplate"].count("{") == 1


# Completion -------------------------------------------------------------------------------


def test_a_datum_completes_from_a_lower_case_prefix():
    """Every vocabulary here is upper case or numeric, and a person types navd88."""
    answer = complete(
        {"type": "ref/prompt", "name": "freeboard_check"},
        {"name": "crest_datum", "value": "navd"},
    )["completion"]
    assert answer["values"] == ["NAVD88"]
    assert answer["hasMore"] is False


def test_a_template_variable_completes_against_the_same_table_the_reader_uses(server):
    """Offering a value the reader would then refuse is worse than offering none."""
    codes = complete(
        {"type": "ref/resource", "uri": f"{SCHEME}parameter/{{code}}"},
        {"name": "code", "value": "000"},
    )["completion"]["values"]
    assert codes
    for code in codes:
        assert server.read_resource(f"{SCHEME}parameter/{code}")


def test_a_free_text_argument_completes_to_nothing():
    """A station identifier is not a closed set, and offering a prefix of one would
    suggest the rest of it exists."""
    answer = complete(
        {"type": "ref/prompt", "name": "freeboard_check"},
        {"name": "identifier", "value": "USGS-"},
    )["completion"]
    assert answer["values"] == [] and answer["total"] == 0


def test_an_unknown_reference_completes_to_nothing_rather_than_failing():
    """A client calls this speculatively. A server that raised would make offering
    completion a risk rather than a convenience."""
    assert complete({"type": "ref/prompt", "name": "gone"}, {"name": "x", "value": ""})[
        "completion"
    ]["values"] == []
    assert complete({}, {})["completion"]["values"] == []


def test_the_server_answers_completion_over_the_protocol(server):
    answer = dispatch(
        server,
        "completion/complete",
        modern(
            {
                "ref": {"type": "ref/resource", "uri": f"{SCHEME}datum/{{name}}"},
                "argument": {"name": "name", "value": "M"},
            }
        ),
    )
    assert "MLLW" in answer["completion"]["values"]


# The manifest link ------------------------------------------------------------------------


def test_export_manifest_points_a_modern_client_at_the_resource(server):
    """The record can be large. A client that would rather fetch it than carry it needs to
    be told it is fetchable."""
    answered = server.call_tool("export_manifest", {}, modern=True)
    links = [c for c in answered["content"] if c["type"] == "resource_link"]
    assert [link["uri"] for link in links] == [f"{SCHEME}manifest"]


def test_a_handshake_client_is_not_sent_a_content_type_its_revision_predates(server):
    """`resource_link` postdates 2024-11-05, which this server still answers, and a client
    of that revision has no reading for a content block it was never told about."""
    answered = server.call_tool("export_manifest", {})
    assert {c["type"] for c in answered["content"]} == {"text"}
