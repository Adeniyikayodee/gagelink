"""UK stations, through the Environment Agency flood-monitoring service.

Recorded live on 2026-08-23 against Maidenhead on the Thames, which publishes no datum
offset, and Hemingford on the Great Ouse, which publishes one. The pair is deliberate: the
offset is what decides whether a level can be put on Ordnance Datum, most stations do not
publish it, and a suite holding only the station that does would test the easy half.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from gagelink import Session
from gagelink.ea import EnvironmentAgency, identifier_of, is_ea, reference_of
from gagelink.results import ErrorCode
from gagelink.tools import Toolkit

FIXTURES = Path(__file__).parent / "fixtures"

#: Maidenhead publishes no datumOffset; Hemingford publishes 6.3 m.
NO_OFFSET = "2604TH"
WITH_OFFSET = "E21136"


def fetch(url: str) -> tuple[int, str]:
    """The recorded service, keyed the way the pack builds its URLs."""
    for reference in (NO_OFFSET, WITH_OFFSET):
        if url.endswith(f"/stations/{reference}"):
            return 200, (FIXTURES / f"ea_station_{reference}.json").read_text()
        if url.endswith(f"/stations/{reference}/measures"):
            return 200, (FIXTURES / f"ea_measures_{reference}.json").read_text()
    raise AssertionError(f"no fixture for {url}")


@pytest.fixture
def session():
    with Session(agency=EnvironmentAgency(fetch=fetch)) as work:
        yield work


@pytest.fixture
def tools(session):
    return Toolkit(session)


# Identifiers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identifier, expected",
    [("EA-2604TH", True), ("ea-E2043", True), ("USGS-01646500", False), ("2604TH", False)],
)
def test_an_identifier_names_its_agency(identifier, expected):
    assert is_ea(identifier) is expected


def test_a_reference_survives_the_underscores_the_service_uses():
    """Live references include entries such as 055003_TG_316."""
    assert reference_of("EA-055003_TG_316") == "055003_TG_316"
    assert identifier_of("055003_TG_316") == "EA-055003_TG_316"


# Description ----------------------------------------------------------------------------------


def test_a_uk_station_is_described_in_the_same_shape_as_an_american_one(tools):
    body = tools.describe_location(identifier_of(NO_OFFSET)).to_dict()

    assert body["ok"] is True
    assert body["data"]["id"] == "EA-2604TH"
    assert body["data"]["name"] == "Maidenhead"
    assert body["data"]["gage_datum"] == "GAUGE:2604TH"


def test_a_station_without_an_offset_says_a_level_cannot_reach_ordnance_datum(tools):
    """Most Environment Agency stations publish no offset, so this is the common case."""
    result = tools.describe_location(identifier_of(NO_OFFSET))
    body = result.to_dict()

    assert body["data"].get("altitude_of_gage_datum") is None
    assert any("cannot be converted onto Ordnance Datum" in n for n in body["notes"])


def test_a_station_with_an_offset_states_it_and_says_the_two_are_not_interchangeable(tools):
    body = tools.describe_location(identifier_of(WITH_OFFSET)).to_dict()
    altitude = body["data"]["altitude_of_gage_datum"]

    assert altitude["value"] == 6.3
    assert altitude["unit"] == "m"
    assert altitude["datum"] == "ODN"
    assert any("not interchangeable" in n for n in body["notes"])


def test_an_unknown_reference_is_a_missing_station_not_a_crash(tools, monkeypatch):
    def missing(url):
        return 200, json.dumps({"items": []})

    monkeypatch.setattr(tools.session.agency, "_fetch", missing)
    body = tools.describe_location("EA-NOSUCH").to_dict()

    assert body["error"] == ErrorCode.LOCATION_UNKNOWN
    assert "EA-2604TH" in body["repair"]


# Readings -------------------------------------------------------------------------------------


def test_a_level_carries_the_station_datum_and_a_flow_does_not(tools):
    """mASD is metres above the station's own zero; a flow is not measured from anything."""
    body = tools.get_latest(identifier_of(NO_OFFSET)).to_dict()
    by_name = {r["parameter_code"]: r for r in body["data"]["readings"]}

    assert by_name["Water Level"]["value"]["datum"] == "GAUGE:2604TH"
    assert by_name["Water Level"]["value"]["unit"] == "m"
    assert "datum" not in by_name["Flow"]["value"]
    assert by_name["Flow"]["value"]["unit"] == "m^3/s"


def test_a_measure_name_stands_as_its_own_parameter(tools):
    """This agency publishes no numeric vocabulary, so the name is the code."""
    body = tools.get_latest(identifier_of(NO_OFFSET)).to_dict()
    reading = body["data"]["readings"][0]
    assert reading["parameter"] == reading["parameter_code"]


def test_readings_are_returned_ungraded_and_say_why(tools):
    """The live service publishes no record grade at all, which is a fact about it."""
    body = tools.get_latest(identifier_of(NO_OFFSET)).to_dict()

    assert all("quality" not in r["value"] for r in body["data"]["readings"])
    assert any("neither provisional nor approved" in n for n in body["notes"])


def test_a_measure_is_matched_by_name_since_there_are_no_codes(tools):
    body = tools.get_latest(identifier_of(NO_OFFSET), parameters=["level"]).to_dict()
    assert [r["parameter_code"] for r in body["data"]["readings"]] == ["Water Level"]


def test_a_measure_the_station_does_not_take_lists_the_ones_it_does(tools):
    body = tools.get_latest(identifier_of(NO_OFFSET), parameters=["turbidity"]).to_dict()

    assert body["error"] == ErrorCode.PARAMETER_NOT_MEASURED
    assert "Water Level" in body["repair"]
    assert "no numeric parameter codes" in body["repair"]


def test_a_stale_reading_is_dropped_and_named(tools):
    """Age is the only staleness signal here: a failed sensor serves its last good value
    indefinitely and nothing in the response marks it as old."""
    body = tools.get_latest(identifier_of(NO_OFFSET), max_age_hours=0).to_dict()
    assert any("dropped as older than" in n for n in body["notes"])


# What this service does not publish ------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.get_series("EA-2604TH", "level", "2026-01-01", "2026-01-02"),
        lambda t: t.get_peaks("EA-2604TH"),
        lambda t: t.get_forecast("EA-2604TH"),
        lambda t: t.get_model_forecast("EA-2604TH"),
        lambda t: t.navigate_network("EA-2604TH"),
        lambda t: t.get_basin("EA-2604TH"),
    ],
)
def test_a_tool_with_no_uk_counterpart_refuses_by_name(tools, call):
    """Refused as unavailable rather than as an unknown station. The station exists; the
    tool cannot answer about it, and those have different repairs."""
    body = call(tools).to_dict()

    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "Environment Agency" in body["message"]
    assert "describe_location and get_latest" in body["repair"]
    assert "from memory" in body["repair"]


# Provenance -----------------------------------------------------------------------------------


def test_a_uk_retrieval_reaches_the_manifest_like_any_other(session):
    """Provenance covering one agency would be provenance in name."""
    tools = Toolkit(session)
    tools.get_latest(identifier_of(NO_OFFSET))
    manifest = session.manifest()

    collections = [r["collection"] for r in manifest["retrievals"]]
    assert collections == ["ea-station", "ea-measures"]
    assert all(r["sha256"] for r in manifest["retrievals"])
    assert manifest["quantities"]


def test_a_station_is_fetched_once_per_session(session):
    tools = Toolkit(session)
    tools.describe_location(identifier_of(NO_OFFSET))
    tools.describe_location(identifier_of(NO_OFFSET))

    stations = [r for r in session.retrievals if r.collection == "ea-station"]
    assert len(stations) == 1


def test_a_level_converts_onto_ordnance_datum_where_the_offset_is_published(tools):
    """The American hazard in UK vocabulary, end to end.

    mASD against mAOD is gage datum against NAVD88: both metres, both plausible, measured
    from different surfaces. Describing the station registers the offset, after which the
    conversion is available and a level can be put on Ordnance Datum.
    """
    from quantity_guard import Q

    tools.describe_location(identifier_of(WITH_OFFSET))
    body = tools.get_latest(identifier_of(WITH_OFFSET)).to_dict()
    level = next(
        r for r in body["data"]["readings"] if r["parameter_code"] == "Water Level"
    )

    on_station = Q(level["value"]["value"], "meter", datum="GAUGE:E21136")
    on_ordnance = on_station.to_datum("ODN")

    assert level["value"]["datum"] == "GAUGE:E21136"
    assert on_ordnance.datum == "ODN"
    assert on_ordnance.magnitude == pytest.approx(level["value"]["value"] + 6.3)


def test_a_level_without_an_offset_refuses_rather_than_guessing(tools):
    """The common case, and the one worth getting right: no offset, no conversion."""
    from quantity_guard import Q
    from quantity_guard.errors import DatumConversionUnavailable

    tools.describe_location(identifier_of(NO_OFFSET))
    with pytest.raises(DatumConversionUnavailable, match="no registered offset"):
        Q(2.187, "meter", datum="GAUGE:2604TH").to_datum("ODN")


# The station search ---------------------------------------------------------------------------
#
# Left out of the first two UK releases on the argument that the pack offers no search and
# writing one here would be writing a second client. The argument did not survive the
# coverage: without a search every UK tool needs a station reference, and a reference is not
# something a question arrives holding.

THAMES = (FIXTURES / "ea_stations_thames.json").read_text()


def searching(body=THAMES, status=200):
    seen = []

    def fetch(url: str) -> tuple[int, str]:
        seen.append(url)
        return status, body

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def kit_searching(**kwargs):
    fetch = searching(**kwargs)
    return Session(agency=EnvironmentAgency(fetch=fetch)), fetch


def test_a_river_search_returns_stations_a_question_can_use():
    """Recorded live for River Thames. The identifiers come back in the EA- form every
    other tool takes, which is the whole point of the search existing."""
    with kit_searching()[0] as work:
        result = Toolkit(work).find_locations(country="GB", river="River Thames").to_dict()

    found = result["data"]["locations"]
    assert result["data"]["count"] == len(found) == 3
    assert {f["id"] for f in found} == {"EA-3400TH", "EA-2607TH", "EA-2902TH"}
    assert all(f["river"] == "River Thames" for f in found)


def test_the_search_says_what_the_listing_does_not_carry():
    """A listing has no altitude, datum or timezone, and a UK level is metres above either
    the station's own datum or Ordnance Datum. A search that let that pass unstated would
    hand back identifiers and leave the hazard to be met later."""
    with kit_searching()[0] as work:
        result = Toolkit(work).find_locations(country="GB", river="River Thames").to_dict()
    assert "Ordnance Datum" in " ".join(result["notes"])


def test_a_search_with_no_filter_is_refused_rather_than_answered():
    """The list is the whole network, and the first ten of it would read as a result."""
    session, fetch = kit_searching()
    with session as work:
        result = Toolkit(work).find_locations(country="GB").to_dict()
    assert fetch.seen == []
    assert result["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "whole Environment Agency network" in result["message"]


def test_the_repair_for_no_match_names_the_filter_that_matches_loosely():
    """The agency matches a river name in full: `Thames` finds nothing and `River Thames`
    finds three. A question almost never arrives in the agency's spelling, so the failure
    has to point at the filter that does not need it."""
    session, _ = kit_searching(body=json.dumps({"items": []}))
    with session as work:
        result = Toolkit(work).find_locations(country="GB", river="Thames").to_dict()
    assert result["error"] == ErrorCode.NO_DATA
    assert "River Thames rather than Thames" in result["repair"]


def test_one_match_arrives_as_a_station_and_not_as_a_list_of_its_fields():
    """The service answers a single match with an object rather than a list of one. Read
    without that, a station's fields are taken for a list of stations."""
    single = json.loads(THAMES)
    single["items"] = single["items"][0]
    session, _ = kit_searching(body=json.dumps(single))
    with session as work:
        result = Toolkit(work).find_locations(country="GB", river="River Thames").to_dict()
    assert result["data"]["count"] == 1


def test_a_search_is_recorded_in_the_manifest_like_any_other_retrieval():
    with kit_searching()[0] as work:
        Toolkit(work).find_locations(country="GB", river="River Thames")
        assert [r["collection"] for r in work.manifest()["retrievals"]] == ["ea-stations"]


def test_the_free_text_filter_reaches_the_service_as_search():
    session, fetch = kit_searching()
    with session as work:
        Toolkit(work).find_locations(country="GB", state="Thames")
    assert "search=Thames" in fetch.seen[0]


def test_a_result_count_is_capped_so_a_wide_search_is_not_the_network():
    from gagelink.ea import MAX_RESULTS

    session, fetch = kit_searching()
    with session as work:
        # The town reaches the agency through county, which is the argument the tool has.
        Toolkit(work).find_locations(country="GB", county="Oxford", limit=5000)
    assert f"_limit={MAX_RESULTS}" in fetch.seen[0]


def test_a_country_this_package_does_not_search_names_the_three_it_does():
    with kit_searching()[0] as work:
        result = Toolkit(work).find_locations(country="DE", river="Rhein").to_dict()
    assert result["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "GB" in result["repair"]
