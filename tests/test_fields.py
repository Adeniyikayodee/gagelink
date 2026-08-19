"""What the service actually publishes, pinned against recorded responses.

Normalisation depends on these field names and on the vocabulary of their values, and the
service is mid-migration off WaterServices, so a rename upstream should fail here rather
than surface as a missing unit somewhere downstream. Fixtures were recorded live on
2026-08-19 from USGS-07374000, Mississippi River at Baton Rouge.
"""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def location():
    return fixture("monitoring_location_07374000")["features"][0]["properties"]


@pytest.fixture
def readings():
    return [f["properties"] for f in fixture("latest_continuous_07374000")["features"]]


def test_the_vertical_datum_is_published_rather_than_left_to_be_guessed(location):
    """The old site service stated an altitude without saying what it was measured from,
    which is why quantity-guard refuses to convert one. This service names it."""
    assert location["vertical_datum"] == "NAVD88"
    assert location["vertical_datum_name"] == "North American Vertical Datum of 1988"


def test_the_timezone_is_published_as_an_abbreviation_plus_a_daylight_flag(location):
    """CST with daylight saving observed is not the same zone as CST without it, so both
    fields are needed to reach an IANA name."""
    assert location["time_zone_abbreviation"] == "CST"
    assert location["uses_daylight_savings"] in {"Y", "N"}


def test_a_reading_states_its_unit_and_its_approval_status(readings):
    """Approval arrives as a word here, where WaterServices used the letters P and A, so
    the quality mapping is not carried over unchanged."""
    for reading in readings:
        assert "unit_of_measure" in reading
        assert reading["approval_status"] in {"Provisional", "Approved"}


def test_values_arrive_as_strings(readings):
    """Parsing is the client's job, and doing it in normalisation keeps the precision the
    service published rather than whatever a float round-trip leaves."""
    values = [r["value"] for r in readings if r["value"] is not None]
    assert values
    assert all(isinstance(v, str) for v in values)


def test_a_missing_value_is_null_rather_than_a_sentinel(readings):
    """WaterServices published -999999 for no data, which is dimensionally valid and
    passes every downstream check. Here absence is absence, and the qualifier says why."""
    missing = [r for r in readings if r["value"] is None]
    assert missing, "the recorded page is expected to contain an equipment outage"
    assert missing[0]["qualifier"] == ["EQUIP"]


def test_the_parameter_name_is_absent_from_a_reading(readings):
    """Only the code comes back, so resolving 00065 to gage height needs the
    parameter-codes reference collection. That is what the lookup tool is for."""
    assert all(r.get("parameter_name") is None for r in readings)
    assert {"00060", "00065"} & {r["parameter_code"] for r in readings}


def test_index_units_carry_a_leading_underscore(readings):
    """Turbidity comes back as _FNU rather than FNU, so the unit map keys on what the
    service writes rather than on what the manual says."""
    units = {r["unit_of_measure"] for r in readings}
    assert any(u.startswith("_") for u in units)
