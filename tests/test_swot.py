"""Satellite elevations, and the geoid nobody mentions.

SWOT is the one source here that is neither an instrument in the water nor a model, and it
brings a datum, a sentinel, and a quality vocabulary of its own, none of them shared with
the two agencies the package already reads.
"""

import json
from pathlib import Path

import pytest
from quantity_guard import Q
from quantity_guard.errors import DatumMismatch

from gagelink import ErrorCode, Satellite, Session, Toolkit
from gagelink.swot import DATUM, SENTINEL_MAGNITUDE, NoObservations, pass_from
from gagelink.service import ServiceUnavailable

FIXTURES = Path(__file__).parent / "fixtures"
PAYLOAD = json.loads((FIXTURES / "swot_reach_63470800171.json").read_text())


def serving(payload=PAYLOAD, status=200):
    def fetch(url, headers):
        return status, {}, json.dumps(payload) if status < 400 else ""

    return fetch


@pytest.fixture
def passes():
    found, _ = Satellite(fetch=serving()).passes(
        "63470800171", "2024-02-01", "2024-06-30"
    )
    return found


# The datum -------------------------------------------------------------------------------


def test_an_elevation_from_orbit_is_on_a_geoid(passes):
    """Not NAVD88, not a gage datum, and nothing in the payload says so."""
    assert DATUM == "EGM2008"
    assert passes[0].elevation.datum == "EGM2008"


def test_it_cannot_be_differenced_against_a_national_datum(passes):
    """Both are lengths in plausible ranges measured from different surfaces, which is the
    comparison the package exists to refuse."""
    survey = Q(500.0, "m", datum="NAVD88")
    with pytest.raises(DatumMismatch):
        survey - passes[0].elevation


def test_it_cannot_be_differenced_against_a_gage_stage(passes):
    from gagelink import Location

    station = Location(id="USGS-01646500", number="01646500", name="x")
    station.register()
    with pytest.raises(DatumMismatch):
        passes[0].elevation - Q(3.03, "ft", datum=station.gage_datum)


def test_the_uncertainty_is_a_length_but_not_an_elevation(passes):
    """A spread about a value rather than a position above a surface, so it carries no
    datum and can be added to one."""
    assert passes[0].uncertainty.datum is None
    assert (passes[0].elevation + passes[0].uncertainty).datum == DATUM


# The sentinel ----------------------------------------------------------------------------


def test_the_third_sentinel_spelling_is_read_as_absence():
    """-9999 at the forecast service, -999999 at the old USGS one, and this."""
    properties = {
        "reach_id": "1", "time_str": "2024-02-08T13:48:48Z",
        "wse": "-999999999999.0", "wse_units": "m", "reach_q": "0",
    }
    assert pass_from(properties).elevation is None
    assert not pass_from(properties).is_usable


def test_the_sentinel_is_caught_by_magnitude_rather_than_by_exact_value():
    """The products use more than one fill and every one is far outside any physical
    range, so a threshold covers spellings a list would miss."""
    for fill in ("-999999999999.0", "-1e12", "999999999999"):
        assert abs(float(fill)) >= SENTINEL_MAGNITUDE


def test_a_real_elevation_is_not_mistaken_for_a_fill(passes):
    assert all(abs(p.elevation.magnitude) < SENTINEL_MAGNITUDE for p in passes if p.is_usable)


# Quality ---------------------------------------------------------------------------------


def test_a_good_pass_grades_no_better_than_provisional():
    """The flag describes the measurement, not a review status, and no product here is
    approved record in the sense a USGS review letter means."""
    good = {"reach_id": "1", "wse": "10", "wse_units": "m", "reach_q": "0"}
    assert pass_from(good).elevation.quality == "provisional"


def test_a_degraded_pass_grades_down():
    for flag, grade in (("1", "estimated"), ("2", "unverified"), ("3", "unverified")):
        properties = {"reach_id": "1", "wse": "10", "wse_units": "m", "reach_q": flag}
        assert pass_from(properties).elevation.quality == grade


# Retrieval -------------------------------------------------------------------------------


def test_the_centreline_geometry_never_reaches_the_answer():
    """A reach is a line of a few hundred coordinate pairs and says nothing a summary
    does not."""
    with Session(satellite=Satellite(fetch=serving())) as work:
        out = Toolkit(work).get_satellite_passes(
            "63470800171", "2024-02-01", "2024-06-30"
        ).to_dict()
    assert "LineString" not in json.dumps(out)
    assert "coordinates" not in json.dumps(out)


def test_the_tool_states_the_datum_it_cannot_convert_from():
    with Session(satellite=Satellite(fetch=serving())) as work:
        notes = " ".join(
            Toolkit(work)
            .get_satellite_passes("63470800171", "2024-02-01", "2024-06-30")
            .to_dict()["notes"]
        )
    assert "EGM2008" in notes and "varies with position" in notes


def test_an_empty_window_says_how_often_the_satellite_returns():
    empty = {"status": "200 OK", "results": {"geojson": {"features": []}}}
    with Session(satellite=Satellite(fetch=serving(empty))) as work:
        result = Toolkit(work).get_satellite_passes("63470800171", "2024-02-01", "2024-02-02")
    assert result.error == ErrorCode.NO_DATA
    assert "every few days" in result.repair


def test_an_unknown_reach_is_told_apart_from_an_empty_window():
    with pytest.raises(NoObservations):
        Satellite(fetch=serving(status=404)).passes("nope", "2024-01-01", "2024-02-01")


def test_a_service_error_is_not_an_absence_of_water():
    with pytest.raises(ServiceUnavailable):
        Satellite(fetch=serving(status=503)).passes("1", "2024-01-01", "2024-02-01")


def test_an_unknown_feature_kind_is_refused():
    with pytest.raises(ValueError, match="unknown feature"):
        Satellite(fetch=serving()).url_for("Ocean", "1", "2024-01-01", "2024-02-01")


def test_the_request_is_recorded_for_the_manifest():
    with Session(satellite=Satellite(fetch=serving())) as work:
        Toolkit(work).get_satellite_passes("63470800171", "2024-02-01", "2024-06-30")
        collections = [r["collection"] for r in work.manifest()["retrievals"]]
    assert collections == ["swot/reach/63470800171"]
