"""The forecast service, and the three things in its payload that need handling."""

import json
from pathlib import Path

import pytest
from quantity_guard import Q
from quantity_guard.errors import DatumMismatch

from gagelink import ErrorCode, Session, Toolkit
from gagelink.nwps import SENTINEL, Forecasts, GaugeNotFound, gauge_from
from gagelink.service import ServiceUnavailable

FIXTURES = Path(__file__).parent / "fixtures"
GAUGE = json.loads((FIXTURES / "nwps_gauge_01646500.json").read_text())


def serving(payload=GAUGE, status=200):
    def fetch(url, headers):
        return status, {}, json.dumps(payload) if status < 400 else ""

    return fetch


@pytest.fixture
def gauge():
    return gauge_from(GAUGE)


# The sentinel ---------------------------------------------------------------------------


def test_the_sentinel_is_read_as_absence_rather_than_as_a_flow():
    """-9999 is dimensionally valid, plausible in sign only, and passes every check
    downstream. Minor, moderate, and major carry no flow threshold at this gauge."""
    assert GAUGE["flood"]["categories"]["minor"]["flow"] == SENTINEL
    assert gauge_from(GAUGE).threshold("minor").flow is None


def test_a_real_threshold_beside_a_sentinel_survives(gauge):
    """Dropping the whole block because part of it is sentinel would lose the one
    threshold that was actually set."""
    assert gauge.threshold("action").flow == Q(22000, "cfs")


def test_a_category_that_is_entirely_sentinel_is_not_reported():
    payload = json.loads(json.dumps(GAUGE))
    payload["flood"]["categories"]["major"] = {"stage": SENTINEL, "flow": SENTINEL}
    assert gauge_from(payload).threshold("major") is None


# Units ----------------------------------------------------------------------------------


def test_the_two_flow_units_in_one_payload_are_a_factor_of_a_thousand_apart():
    """Flood categories are published in cfs and the status block in kcfs, in the same
    response. A caller reading both and treating them alike is out by 1000."""
    assert GAUGE["flood"]["flowUnits"] == "cfs"
    assert GAUGE["status"]["observed"]["secondaryUnit"] == "kcfs"
    assert Q(1, "kcfs").to("cfs").magnitude == pytest.approx(1000)


# Datums ---------------------------------------------------------------------------------


def test_a_stage_is_on_the_gages_own_datum_and_not_a_national_one(gauge):
    """The observed stage here equals USGS parameter 00065 at the same station and time,
    which is the evidence for reading these as gage datum rather than as elevations."""
    assert gauge.stage_datum == "GAGE:01646500"
    assert gauge.observed.datum == "GAGE:01646500"
    assert gauge.threshold("minor").stage.datum == "GAGE:01646500"


def test_the_frame_matches_the_one_the_usgs_site_record_registers(gauge):
    """Both services name it the same way, which is what makes their stages comparable
    rather than merely both being in feet."""
    from gagelink import Location

    station = Location(id="USGS-01646500", number="01646500", name="Little Falls")
    assert station.gage_datum == gauge.stage_datum


def test_a_gauge_with_no_usgs_station_gets_a_frame_of_its_own():
    """Rather than being labelled with a station it has no relationship to."""
    payload = json.loads(json.dumps(GAUGE))
    payload["usgsId"] = ""
    assert gauge_from(payload).stage_datum == "NWPS:BRKM2"


def test_freeboard_is_a_delta_carrying_no_datum(gauge):
    """Two stages on one datum difference to a margin, which is not an elevation and must
    not present as one."""
    margin = gauge.freeboard_to("minor")
    assert margin.magnitude == pytest.approx(6.98)
    assert margin.datum is None


def test_a_threshold_cannot_be_differenced_against_a_surveyed_elevation(gauge):
    """The comparison a freeboard calculation reaches for, and the one that is wrong."""
    crest = Q(41.0, "ft", datum="NAVD88")
    with pytest.raises(DatumMismatch):
        crest - gauge.threshold("minor").stage


def test_freeboard_is_none_where_the_threshold_is_unset(gauge):
    assert gauge.freeboard_to("record") is None


# Everything else -------------------------------------------------------------------------


def test_the_posix_timezone_becomes_an_iana_one(gauge):
    """The service writes EST5EDT, which names no region."""
    assert GAUGE["timeZone"] == "EST5EDT"
    assert gauge.timezone == "America/New_York"


def test_a_missing_gauge_is_its_own_failure():
    """Distinct from an unknown station, because most gaged streams carry no forecast
    point and that is a fact about the river rather than a typo."""
    with pytest.raises(GaugeNotFound):
        Forecasts(fetch=serving(status=404)).gauge("nowhere")


def test_a_service_error_is_not_a_missing_gauge():
    with pytest.raises(ServiceUnavailable):
        Forecasts(fetch=serving(status=503)).gauge("brkm2")


def test_the_request_is_recorded_for_the_manifest():
    _, retrieval = Forecasts(fetch=serving()).gauge("01646500")
    record = retrieval.record()
    assert record["collection"] == "nwps/gauges/01646500"
    assert len(record["sha256"]) == 64


def test_a_repeated_gauge_is_served_from_cache():
    calls = []

    def counting(url, headers):
        calls.append(url)
        return 200, {}, json.dumps(GAUGE)

    client = Forecasts(fetch=counting)
    client.gauge("01646500")
    client.gauge("01646500")
    assert len(calls) == 1


# Through the toolkit ----------------------------------------------------------------------


def test_the_tool_states_the_frame_and_the_sentinel():
    with Session(forecasts=Forecasts(fetch=serving())) as work:
        out = Toolkit(work).get_forecast("USGS-01646500").to_dict()

    notes = " ".join(out["notes"])
    assert "GAGE:01646500" in notes
    assert "sentinel" in notes
    assert out["data"]["below_minor_flooding"] == {"value": 6.98, "unit": "ft"}


def test_a_station_without_a_forecast_point_says_what_is_still_available():
    with Session(forecasts=Forecasts(fetch=serving(status=404))) as work:
        result = Toolkit(work).get_forecast("USGS-99999999")
    assert result.error == ErrorCode.NO_DATA
    assert "get_latest" in result.repair


def test_the_forecast_appears_in_the_manifest():
    with Session(forecasts=Forecasts(fetch=serving())) as work:
        Toolkit(work).get_forecast("USGS-01646500")
        manifest = work.manifest()
    assert manifest["retrievals"][0]["collection"].startswith("nwps/")
    assert any(q["field"] == "minor_stage" for q in manifest["quantities"])
