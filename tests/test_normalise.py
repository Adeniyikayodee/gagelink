"""Turning payloads into quantities, and refusing to guess what is not published."""

import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from quantity_guard import Q
from quantity_guard.errors import DatumMismatch

from gagelink import (
    Location,
    UnitsNotPublished,
    UnknownUnit,
    grade,
    location_from,
    parse_unit,
    reading_from,
    readings_from,
    timezone_of,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def location():
    page = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())
    return location_from(page["features"][0])


@pytest.fixture
def readings(location):
    page = json.loads((FIXTURES / "latest_continuous_07374000.json").read_text())
    return {r.parameter_code: r for r in readings_from(page, location)}


# Units ----------------------------------------------------------------------------------


def test_the_caret_spelling_of_discharge_is_read_correctly():
    """WaterServices wrote ft3/s and this service writes ft^3/s, which pint reads as an
    exclusive-or unless it is translated."""
    assert Q(1, parse_unit("ft^3/s")).to("m**3/s").magnitude == pytest.approx(0.0283168, rel=1e-4)


def test_an_index_unit_keeps_its_own_dimension():
    """Turbidity in FNU and pH are both dimensionless in the ordinary sense, and letting
    them share a dimension would allow one to be compared against the other."""
    assert parse_unit("_FNU") == "FNU"
    with pytest.raises(Exception):
        Q(5.4, "FNU").to("pH_unit")


def test_parts_per_thousand_does_not_become_parts_per_trillion():
    """USGS reports salinity in ppt meaning parts per thousand, while ppt reads as parts
    per trillion nearly everywhere else. The two differ by 10^9 and are dimensionally
    identical, so they are not allowed to share a unit."""
    assert parse_unit("ppt") == "salinity_ppt"


def test_unit_matching_ignores_case_and_the_underscore_prefix():
    assert parse_unit("DEGC") == "degC"
    assert parse_unit("_NTU") == "NTU"


def test_a_unit_pint_cannot_read_is_refused_rather_than_guessed():
    with pytest.raises(UnknownUnit) as caught:
        parse_unit("smoots")
    assert "refused rather than guessed" in str(caught.value)


def test_a_parseable_but_unmapped_unit_is_allowed_through_audibly():
    """Parseable is not the same as understood. ppt is the standing example, where pint
    reads parts per trillion and USGS means parts per thousand."""
    with pytest.warns(UserWarning, match="no entry in gagelink.normalise.UNITS"):
        assert parse_unit("furlong/fortnight") == "furlong/fortnight"


def test_a_value_published_without_a_unit_is_refused():
    with pytest.raises(UnitsNotPublished):
        parse_unit(None)


# Quality --------------------------------------------------------------------------------


def test_approval_arrives_as_a_word_not_a_letter():
    """WaterServices used P and A; this service uses Provisional and Approved."""
    assert grade("Provisional", None) == "provisional"
    assert grade("Approved", None) == "approved"


def test_a_condition_code_grades_below_its_review_status():
    """Approved record of an ice-affected measurement is not approved-quality data."""
    assert grade("Approved", ["ICE"]) == "unverified"
    assert grade("Approved", ["RAT"]) == "estimated"


def test_an_unrecognised_qualifier_grades_down_rather_than_being_ignored():
    """Absence of a mapping is not evidence of good record."""
    with pytest.warns(UserWarning, match="unmapped qualifier"):
        assert grade("Approved", ["NEWCODE"]) == "unverified"


def test_a_reading_with_no_flags_carries_no_grade():
    assert grade(None, None) is None


# Timezones ------------------------------------------------------------------------------


def test_the_daylight_saving_flag_separates_arizona_from_colorado():
    """Both publish MST, and they differ by an hour for eight months of the year."""
    assert timezone_of("MST", "Y") == "America/Denver"
    assert timezone_of("MST", "N") == "Etc/GMT+7"


def test_an_unknown_zone_is_none_rather_than_utc():
    assert timezone_of("ZZZ", "Y") is None
    assert timezone_of(None, "Y") is None


# Locations ------------------------------------------------------------------------------


def test_altitude_and_area_are_given_the_units_the_service_omits(location):
    """Neither field states a unit and the collection schema states none either, so the
    convention is applied here, where it is visible, rather than downstream."""
    assert location.altitude == Q(0.0, "foot", datum="NAVD88")
    assert location.drainage_area.to("mile**2").magnitude == pytest.approx(1125810.0)


def test_the_station_timezone_is_resolved(location):
    assert location.timezone == "America/Chicago"


def test_registering_a_station_makes_its_stage_convertible(location):
    """The altitude and its datum are both published here, so the offset is registered
    and a stage can be moved onto NAVD88."""
    location.register()
    stage = Q(8.09, "ft", datum=location.gage_datum)
    assert stage.to_datum("NAVD88").magnitude == pytest.approx(8.09)


def test_a_station_with_no_published_altitude_still_gets_its_own_frame():
    """The frame is registered whether or not it can be converted, so a stage is labelled
    with what it was measured from. Guessing the datum is the same class of error as
    guessing the offset, arriving one step earlier."""
    unknown = Location(id="USGS-99999999", number="99999999", name="Nowhere")
    unknown.register()
    stage = Q(4.0, "ft", datum=unknown.gage_datum)
    with pytest.raises(Exception):
        stage.to_datum("NAVD88")


# Readings -------------------------------------------------------------------------------


def test_gage_height_lands_on_the_station_datum(readings):
    assert readings["00065"].value.datum == "GAGE:07374000"


def test_an_elevation_parameter_lands_on_the_datum_it_names(readings):
    """63160 is a water surface elevation above NAVD88, not a stage."""
    assert readings["63160"].value.datum == "NAVD88"


def test_a_stage_cannot_be_differenced_against_an_elevation(readings):
    """The two are both lengths and both 8.09 ft here, so nothing dimensional separates
    them. This is the comparison the package exists to refuse."""
    with pytest.raises(DatumMismatch):
        readings["63160"].value - readings["00065"].value


def test_the_same_difference_is_allowed_once_the_frames_agree(readings, location):
    location.register()
    on_navd = readings["00065"].value.to_datum("NAVD88")
    assert (readings["63160"].value - on_navd).magnitude == pytest.approx(0.0)


def test_a_discharge_keeps_the_unit_it_was_published_in(readings):
    """Published in ft^3/s, so it stays there, and a conversion is something a caller
    asks for rather than something retrieval performs."""
    assert readings["00060"].value.to("ft**3/s").magnitude == pytest.approx(243000)
    assert readings["00060"].unit_published == "ft^3/s"


def test_provisional_record_is_carried_on_the_value(readings):
    assert readings["00060"].value.quality == "provisional"


def test_a_missing_value_is_none_and_says_why(readings):
    """No sentinel is invented, and the qualifier explains the gap."""
    missing = readings["99133"]
    assert missing.is_missing
    assert missing.value is None
    assert missing.qualifiers == ("EQUIP",)


def test_age_is_measurable_because_latest_is_not_current(readings):
    """One response can mix a discharge from this morning with a turbidity from 2019."""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    assert readings["00060"].age(now) == timedelta(hours=2, minutes=45)
    assert readings["00060"].is_stale(timedelta(hours=1), now)
    assert not readings["00060"].is_stale(timedelta(hours=6), now)


def test_a_reading_with_no_time_counts_as_stale():
    """Unknown age is not evidence of freshness."""
    reading = reading_from({"properties": {"parameter_code": "00060", "value": None}})
    assert reading.age() is None
    assert reading.is_stale(timedelta(hours=1))


def test_one_unmapped_unit_costs_its_own_series_and_not_the_station():
    page = {
        "features": [
            {"properties": {"parameter_code": "00060", "value": "1", "unit_of_measure": "ft^3/s"}},
            {"properties": {"parameter_code": "99999", "value": "1", "unit_of_measure": "smoots"}},
        ]
    }
    with pytest.warns(UserWarning, match="skipping parameter 99999"):
        kept = readings_from(page)
    assert [r.parameter_code for r in kept] == ["00060"]


def test_a_station_datum_can_itself_be_on_ngvd29():
    """Boulder Creek publishes its altitude on NGVD29, so a stage there converts onto
    NGVD29 and not onto NAVD88, and the two differ by about a foot in Colorado. Assuming
    NAVD88 because it is the modern datum is the freeboard error one step earlier."""
    page = json.loads((FIXTURES / "monitoring_location_06730500.json").read_text())
    boulder = location_from(page["features"][0])
    boulder.register()
    assert boulder.vertical_datum == "NGVD29"

    stage = Q(9.11, "ft", datum=boulder.gage_datum)
    assert stage.to_datum("NGVD29").magnitude == pytest.approx(4869.11)
    with pytest.raises(Exception):
        stage.to_datum("NAVD88")
