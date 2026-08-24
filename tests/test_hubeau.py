"""French stations, through Hub'Eau.

Recorded live on 2026-08-23. The station pair is deliberate: the Seine at Austerlitz
publishes an altitude on NGF-IGN69, so a level there can be put on a national datum, and
the Loire at Cros-de-Georand publishes neither an altitude nor a vertical system, so it
cannot. The second is the case that has to refuse.
"""

import json
from pathlib import Path

import pytest

from gagelink import Session
from gagelink.hubeau import (
    ALTIMETRIC_SYSTEMS,
    Hydrometry,
    UnknownQuantity,
    datum_of,
    grade_of,
    identifier_of,
    is_french,
    reference_of,
    unit_of,
)
from gagelink.results import ErrorCode
from gagelink.tools import Toolkit

FIXTURES = Path(__file__).parent / "fixtures"

WITH_ALTITUDE = "F700000102"     # La Seine a Paris, Austerlitz
WITHOUT_ALTITUDE = "K001002010"  # La Loire a Cros-de-Georand
WITH_DAILY = "V100001001"        # Le Rhone a Pougny


def fetch(url: str) -> tuple[int, str]:
    """The recorded service, matched on the query the client builds."""
    if "referentiel/stations" in url:
        if f"code_station={WITH_ALTITUDE}" in url:
            return 200, (FIXTURES / f"hubeau_station_{WITH_ALTITUDE}.json").read_text()
        if f"code_station={WITHOUT_ALTITUDE}" in url:
            return 200, (FIXTURES / f"hubeau_station_{WITHOUT_ALTITUDE}.json").read_text()
        if f"code_station={WITH_DAILY}" in url:
            return 200, (FIXTURES / f"hubeau_station_{WITH_DAILY}.json").read_text()
        if "libelle_cours_eau" in url:
            return 206, (FIXTURES / "hubeau_search_seine.json").read_text()
        return 200, json.dumps({"count": 0, "data": []})
    if "observations_tr" in url:
        quantity = "H" if "grandeur_hydro=H" in url else "Q"
        if f"code_entite={WITH_ALTITUDE}" in url:
            return 206, (FIXTURES / f"hubeau_obs_{WITH_ALTITUDE}_{quantity}.json").read_text()
        return 200, json.dumps({"count": 0, "data": []})
    if "obs_elab" in url:
        if f"code_entite={WITH_DAILY}" in url:
            return 200, (FIXTURES / f"hubeau_daily_{WITH_DAILY}.json").read_text()
        return 200, json.dumps({"count": 0, "data": []})
    raise AssertionError(f"no fixture for {url}")


@pytest.fixture
def session():
    with Session(france=Hydrometry(fetch=fetch)) as work:
        yield work


@pytest.fixture
def tools(session):
    return Toolkit(session)


# The vocabularies the payload does not carry -----------------------------------------------


@pytest.mark.parametrize(
    "identifier, expected",
    [("FR-F700000102", True), ("fr-K001002010", True), ("EA-2604TH", False), ("USGS-01646500", False)],
)
def test_an_identifier_names_its_agency(identifier, expected):
    assert is_french(identifier) is expected
    if expected:
        assert identifier_of(reference_of(identifier)).upper() == identifier.upper()


def test_a_unit_is_supplied_because_no_payload_carries_one():
    """The whole hazard: 358000 for the Rhone is litres per second, not cubic metres."""
    assert unit_of("H") == "millimeter"
    assert unit_of("Q") == "liter/second"


def test_an_unmapped_quantity_is_refused_rather_than_guessed():
    with pytest.raises(UnknownQuantity, match="no recorded unit"):
        unit_of("TW")


def test_a_vertical_system_code_is_decoded_to_a_name():
    assert datum_of(3) == "NGF-IGN69"
    assert ALTIMETRIC_SYSTEMS["2"] == "NGF-Lallemand"


@pytest.mark.parametrize("code", [0, 31, None, 999])
def test_a_system_that_is_not_a_national_datum_decodes_to_nothing(code):
    """0 is unknown and 31 is the station's own zero. Naming either would let a level be
    differenced against an elevation."""
    assert datum_of(code) is None


@pytest.mark.parametrize(
    "qualification, status, expected",
    [
        (20, 16, "approved"),      # Bonne, validated
        (16, 4, "provisional"),     # not qualified, raw
        (20, 4, "provisional"),     # good but raw: the weaker of the two wins
        (12, 16, "unverified"),     # doubtful, however validated
        (99, 16, "unverified"),     # an unrecognised code grades down
    ],
)
def test_two_quality_vocabularies_combine_to_the_weaker(qualification, status, expected):
    assert grade_of(qualification, status) == expected


# Description -----------------------------------------------------------------------------


def test_a_french_station_carries_its_river_and_region(tools):
    body = tools.describe_location(identifier_of(WITH_ALTITUDE)).to_dict()

    assert body["ok"] is True
    assert body["data"]["id"] == "FR-F700000102"
    assert body["data"]["site_type"] == "La Seine"
    assert body["data"]["state"] == "ILE-DE-FRANCE"


def test_an_altitude_arrives_on_the_system_its_code_named(tools):
    body = tools.describe_location(identifier_of(WITH_ALTITUDE)).to_dict()
    altitude = body["data"]["altitude_of_gage_datum"]

    assert altitude == {"value": 25.92, "unit": "m", "datum": "NGF-IGN69"}
    assert any("Add that offset" in n for n in body["notes"])


def test_a_station_with_no_vertical_system_says_a_level_cannot_reach_one(tools):
    body = tools.describe_location(identifier_of(WITHOUT_ALTITUDE)).to_dict()

    # Absent rather than null: a key rendered as None reads as a station that published
    # nothing there, and this is an agency that published no vertical system at all.
    assert body["data"].get("altitude_of_gage_datum") is None
    assert any("cannot be converted onto a national datum" in n for n in body["notes"])


def test_an_unknown_code_is_a_missing_station(tools):
    body = tools.describe_location("FR-NOSUCH").to_dict()
    assert body["error"] == ErrorCode.LOCATION_UNKNOWN
    assert "country=FR" in body["repair"]


# Readings --------------------------------------------------------------------------------


def test_a_level_arrives_in_millimetres_on_the_station_zero(tools):
    """Neither the unit nor the frame is in the payload; both are attached here."""
    body = tools.get_latest(identifier_of(WITH_ALTITUDE)).to_dict()
    reading = body["data"]["readings"][0]

    assert reading["parameter_code"] == "H"
    assert reading["value"]["unit"] == "mm"
    assert reading["value"]["datum"] == "GAGE:F700000102"
    assert reading["parameter"].startswith("water level")


def test_the_missing_unit_is_stated_in_the_result(tools):
    body = tools.get_latest(identifier_of(WITH_ALTITUDE)).to_dict()
    assert any("publishes no unit with any value" in n for n in body["notes"])


def test_a_raw_reading_is_graded_provisional_not_approved(tools):
    body = tools.get_latest(identifier_of(WITH_ALTITUDE)).to_dict()
    assert body["data"]["readings"][0]["value"]["quality"] == "provisional"


def test_a_quantity_is_matched_by_the_word_a_question_uses(tools):
    """A model asking for level does not know this service calls it H."""
    body = tools.get_latest(identifier_of(WITH_ALTITUDE), parameters=["level"]).to_dict()
    assert [r["parameter_code"] for r in body["data"]["readings"]] == ["H"]


def test_a_quantity_the_station_does_not_publish_names_the_ones_it_does(tools):
    body = tools.get_latest(identifier_of(WITH_ALTITUDE), parameters=["turbidity"]).to_dict()

    assert body["error"] == ErrorCode.PARAMETER_NOT_MEASURED
    assert "no USGS parameter codes here" in body["repair"]


def test_a_level_converts_onto_the_national_datum(tools):
    """The American hazard in French vocabulary: a relative height plus the station's
    altitude is an elevation, and the two are not the same measurement."""
    from quantity_guard import Q

    tools.describe_location(identifier_of(WITH_ALTITUDE))
    body = tools.get_latest(identifier_of(WITH_ALTITUDE)).to_dict()
    level = body["data"]["readings"][0]["value"]

    on_national = Q(level["value"], "mm", datum="GAGE:F700000102").to_datum("NGF-IGN69")
    assert on_national.to("meter").magnitude == pytest.approx(25.92 + level["value"] / 1000)


# Search and series ------------------------------------------------------------------------


def test_the_french_network_is_searched_by_river_name(tools):
    body = tools.find_locations(country="FR", river="La Seine", limit=5).to_dict()

    assert body["ok"] is True
    assert body["data"]["count"] == 5
    assert all(loc["id"].startswith("FR-") for loc in body["data"]["locations"])


def test_a_search_with_no_filter_is_refused(tools):
    body = tools.find_locations(country="FR").to_dict()
    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "watercourse name" in body["repair"]


def test_river_is_refused_against_the_american_network(tools):
    body = tools.find_locations(river="La Seine").to_dict()
    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "French network only" in body["message"]


def test_a_daily_series_is_returned_in_litres_per_second(tools):
    body = tools.get_series(
        identifier_of(WITH_DAILY), "QmnJ", "2024-08-01", "2024-08-10"
    ).to_dict()

    assert body["data"]["summary"]["count"] == 10
    assert body["data"]["summary"]["unit"] == "l/s"
    assert any("lags the present" in n for n in body["notes"])


def test_a_malformed_date_is_still_an_argument_error_for_france(tools):
    body = tools.get_series(identifier_of(WITH_DAILY), "QmnJ", "not-a-date", "2024-08-10").to_dict()
    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "not a date" in body["message"]


# What this service does not publish -------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.get_peaks("FR-F700000102"),
        lambda t: t.get_forecast("FR-F700000102"),
        lambda t: t.get_model_forecast("FR-F700000102"),
        lambda t: t.navigate_network("FR-F700000102"),
        lambda t: t.get_basin("FR-F700000102"),
    ],
)
def test_a_tool_with_no_french_counterpart_refuses_by_name(tools, call):
    body = call(tools).to_dict()

    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "Hub'Eau station" in body["message"]
    assert "publishes no an " not in body["message"]  # the article bug, pinned
    assert "from memory" in body["repair"]


# Provenance --------------------------------------------------------------------------------


def test_a_french_retrieval_reaches_the_manifest(session):
    tools = Toolkit(session)
    tools.get_latest(identifier_of(WITH_ALTITUDE))
    manifest = session.manifest()

    collections = [r["collection"] for r in manifest["retrievals"]]
    assert collections[0] == "hubeau-stations"
    assert "hubeau-observations" in collections
    assert all(r["sha256"] for r in manifest["retrievals"])


def test_one_request_per_quantity_is_what_it_costs(session):
    """The endpoint filters on one grandeur_hydro at a time, so a station measuring a
    level and a flow costs two requests. Pinned because it doubles what a question spends."""
    Toolkit(session).get_latest(identifier_of(WITH_ALTITUDE))
    observations = [r for r in session.retrievals if r.collection == "hubeau-observations"]
    assert len(observations) == 2


# The three defects found on 2026-08-24 --------------------------------------------------------


def test_a_place_name_is_refused_where_the_service_matches_a_code(tools):
    """This endpoint ignores a filter it does not recognise and answers with the whole
    national network, so a name reaching it returns 6,468 stations reading as a result."""
    body = tools.find_locations(country="FR", state="ILE-DE-FRANCE").to_dict()

    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "is not one" in body["message"]
    assert "by code rather than by name" in body["repair"]


def test_only_verified_filters_are_ever_sent():
    """The guard behind the fix: a filter never checked against the live service would
    silently widen every search that used it."""
    from gagelink.hubeau import SEARCH_FILTERS, _refuse_unknown, HubeauError

    _refuse_unknown({"libelle_cours_eau": "La Seine"})
    with pytest.raises(HubeauError, match="not a filter this service honours"):
        _refuse_unknown({"libelle_region": "ILE-DE-FRANCE"})
    assert "libelle_region" not in SEARCH_FILTERS
    assert "libelle_commune" not in SEARCH_FILTERS


def test_a_level_over_a_range_is_refused_rather_than_answered_with_a_discharge(tools):
    """It used to return QmnJ whatever was asked for, so a question about water level got
    flow in litres per second and nothing said so."""
    body = tools.get_series(
        identifier_of(WITH_DAILY), "H", "2024-08-01", "2024-08-10"
    ).to_dict()

    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "elaborates no series" in body["message"]
    assert "HIXM" in body["repair"]


def test_each_elaborated_quantity_is_asked_for_by_its_own_name(tools):
    from gagelink.hubeau import ELABORATED, quantity_for

    assert quantity_for("daily discharge") == "QmnJ"
    assert quantity_for("monthly mean discharge") == "QmM"
    assert set(ELABORATED) == {"QmnJ", "QmM", "HIXM"}
    assert quantity_for("HmnJ") is None  # reads like a daily level; the service has none


def test_a_truncated_range_says_how_much_it_left(session):
    """The service caps a page and hands back a link. Summarising the first page as though
    it were the range is the failure this pins."""
    held, served = 11323, 5000

    def paged(url):
        if "referentiel/stations" in url:
            return 200, (FIXTURES / f"hubeau_station_{WITH_DAILY}.json").read_text()
        rows = json.loads((FIXTURES / f"hubeau_daily_{WITH_DAILY}.json").read_text())["data"]
        return 206, json.dumps({"count": held, "next": None, "data": rows[:served] or rows})

    session.france = Hydrometry(fetch=paged)
    body = Toolkit(session).get_series(
        identifier_of(WITH_DAILY), "QmnJ", "1990-01-01", "2020-12-31"
    ).to_dict()

    assert body["ok"] is True
    assert any("more values in this range than were fetched" in n for n in body["notes"])


def test_a_manifest_records_the_status_the_service_sent(session):
    """Not one the client worked out from the payload: a ledger holding a guess is worse
    than one holding nothing."""
    Toolkit(session).find_locations(country="FR", river="La Seine", limit=5)
    search = [r for r in session.retrievals if r.collection == "hubeau-stations"][0]
    assert search.status == 206  # the fixture was recorded as a partial page


def test_a_country_the_package_does_not_search_is_named_as_such(tools):
    body = tools.find_locations(country="DE", river="Rhein").to_dict()
    assert body["error"] == ErrorCode.INVALID_ARGUMENTS
    assert "no network is named" in body["message"]
