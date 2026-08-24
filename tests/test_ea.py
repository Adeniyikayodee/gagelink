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
