"""The surface a model calls, driven from recorded responses."""

import json
from pathlib import Path

import pytest

from gagelink import ErrorCode, Result, Service, Session, Toolkit
from gagelink.results import fit, size_in_tokens
from gagelink.service import QuotaExhausted

FIXTURES = Path(__file__).parent / "fixtures"
LOCATION = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())
BOULDER = json.loads((FIXTURES / "monitoring_location_06730500.json").read_text())
LATEST = json.loads((FIXTURES / "latest_continuous_07374000.json").read_text())


EMPTY = {"type": "FeatureCollection", "features": []}


def serving(**pages):
    """A fetch answering each collection from a recorded page.

    Requests for a location the recorded page does not hold come back empty, as the
    service would answer them, so a test for an unknown identifier tests the tool rather
    than the fake.
    """

    def fetch(url, headers):
        for collection, page in pages.items():
            if f"/{collection.replace('_', '-')}/items" not in url:
                continue
            asked = _asked_for(url)
            held = {
                (f.get("properties") or {}).get("id")
                or (f.get("properties") or {}).get("monitoring_location_id")
                for f in page.get("features") or []
            }
            if asked and held and asked not in held:
                return 200, {}, json.dumps(EMPTY)
            return 200, {"X-RateLimit-Remaining": "998"}, json.dumps(page)
        return 200, {}, json.dumps(EMPTY)

    return fetch


def _asked_for(url):
    for key in ("id=", "monitoring_location_id="):
        if key in url:
            return url.split(key, 1)[1].split("&", 1)[0]
    return None


@pytest.fixture
def kit():
    fetch = serving(monitoring_locations=LOCATION, latest_continuous=LATEST)
    with Session(service=Service(fetch=fetch), question="how high is the river?") as work:
        yield Toolkit(work)


# Contract ------------------------------------------------------------------------------


def test_every_failure_carries_a_repair(kit):
    """A raised exception ends the turn; a failure with a repair keeps the model in the
    conversation where it can correct itself."""
    failed = kit.describe_location("USGS-00000000")
    assert failed.ok is False
    assert failed.error == ErrorCode.LOCATION_UNKNOWN
    assert failed.repair
    assert "USGS-01646500" in failed.repair


def test_a_transport_failure_is_returned_rather_than_raised():
    def refusing(url, headers):
        return 429, {"X-RateLimit-Limit": "50"}, ""

    with Session(service=Service(fetch=refusing)) as work:
        result = Toolkit(work).describe_location("USGS-07374000")
    assert result.ok is False
    assert result.error == ErrorCode.QUOTA_EXHAUSTED
    assert "do not" in result.repair.lower()


def test_the_remaining_allowance_travels_with_the_result(kit):
    """An agent that knows it has nine requests left can plan around it."""
    assert kit.describe_location("USGS-07374000").to_dict()[
        "requests_remaining_this_hour"
    ] == 998


def test_a_search_with_no_filter_is_refused_rather_than_run(kit):
    result = kit.find_locations()
    assert result.error == ErrorCode.INVALID_ARGUMENTS


# Quantities out --------------------------------------------------------------------------


def test_a_value_leaves_with_its_frame_attached(kit):
    stage = next(
        r
        for r in kit.get_latest("USGS-07374000").to_dict()["data"]["readings"]
        if r["parameter_code"] == "00065"
    )
    assert stage["value"] == {
        "value": 8.09,
        "unit": "ft",
        "datum": "GAGE:07374000",
        "quality": "provisional",
    }


def test_units_are_written_the_way_the_service_writes_them(kit):
    """pint's short form spaces its operators, and a model reading `ft ** 3 / s` has to
    reassemble it before it can match it against anything."""
    discharge = next(
        r
        for r in kit.get_latest("USGS-07374000").to_dict()["data"]["readings"]
        if r["parameter_code"] == "00060"
    )
    assert discharge["value"]["unit"] == "ft^3/s"


def test_a_missing_value_says_so_rather_than_arriving_as_an_absent_key(kit):
    gap = next(
        r
        for r in kit.get_latest("USGS-07374000").to_dict()["data"]["readings"]
        if r["parameter_code"] == "99133"
    )
    assert "value" not in gap
    assert "not a value of zero" in gap["measurement"]
    assert gap["qualifiers"] == ["EQUIP"]


def test_describing_a_station_states_the_offset_a_stage_needs(kit):
    notes = " ".join(kit.describe_location("USGS-07374000").to_dict()["notes"])
    assert "GAGE:07374000" in notes
    assert "NAVD88" in notes


def test_a_station_without_a_publishable_offset_says_that_instead():
    """Rather than leaving the model to discover it when a conversion is refused."""
    bare = json.loads(json.dumps(LOCATION))
    bare["features"][0]["properties"]["vertical_datum"] = None
    with Session(service=Service(fetch=serving(monitoring_locations=bare))) as work:
        notes = " ".join(
            Toolkit(work).describe_location("USGS-07374000").to_dict()["notes"]
        )
    assert "cannot be converted" in notes


def test_drainage_area_says_the_unit_the_service_omits(kit):
    notes = " ".join(kit.describe_location("USGS-07374000").to_dict()["notes"])
    assert "square miles" in notes


# Series ---------------------------------------------------------------------------------


def series_page(count, unit="ft^3/s", start_day=1):
    return {
        "features": [
            {
                "properties": {
                    "monitoring_location_id": "USGS-07374000",
                    "parameter_code": "00060",
                    "value": str(1000 + i),
                    "unit_of_measure": unit,
                    "time": f"2026-08-{start_day + i // 24:02d}T{i % 24:02d}:00:00+00:00",
                    "approval_status": "Approved",
                    "statistic_id": "00011",
                }
            }
            for i in range(count)
        ]
    }


@pytest.fixture
def series_kit():
    fetch = serving(monitoring_locations=LOCATION, continuous=series_page(500))
    with Session(service=Service(fetch=fetch)) as work:
        yield Toolkit(work)


def test_a_series_returns_a_handle_and_a_summary_rather_than_its_points(series_kit):
    """A year of 15-minute record is 35,000 points and no answer needs them in context."""
    data = series_kit.get_series(
        "USGS-07374000", "00060", "2026-08-01", "2026-08-21", resolution="continuous"
    ).to_dict()["data"]

    assert data["summary"]["count"] == 500
    assert data["summary"]["maximum"]["value"] == 1499
    assert data["summary"]["mean"] == 1249.5
    assert len(data["preview"]) == 20
    assert data["handle"].startswith("series-")


def test_the_handle_is_derived_from_the_query_so_a_replay_reproduces_it(series_kit):
    """Handles issued in sequence would differ between a run and its replay, and a
    manifest that cannot be compared is not a manifest."""
    args = ("USGS-07374000", "00060", "2026-08-01", "2026-08-21")
    first = series_kit.get_series(*args, resolution="continuous").to_dict()
    second = series_kit.get_series(*args, resolution="continuous").to_dict()
    assert first["data"]["handle"] == second["data"]["handle"]


def test_a_stored_series_can_be_narrowed_without_fetching_again(series_kit):
    handle = series_kit.get_series(
        "USGS-07374000", "00060", "2026-08-01", "2026-08-21", resolution="continuous"
    ).to_dict()["data"]["handle"]
    before = len(series_kit.session.retrievals)

    narrowed = series_kit.slice_series(handle, start="2026-08-02").to_dict()
    assert narrowed["data"]["summary"]["count"] < 500
    assert len(series_kit.session.retrievals) == before


def test_an_unknown_handle_says_where_handles_come_from(series_kit):
    result = series_kit.slice_series("series-nope")
    assert result.error == ErrorCode.UNKNOWN_HANDLE
    assert "get_series" in result.repair


def test_an_empty_range_is_a_failure_rather_than_an_empty_summary(series_kit):
    assert series_kit.get_series(
        "USGS-07374000", "00060", "2026-01-01", "2026-01-02", resolution="daily"
    ).error == ErrorCode.NO_DATA


# Budget ----------------------------------------------------------------------------------


def test_a_result_over_budget_is_trimmed_and_says_so():
    """A result that dropped 900 of 1,000 items and does not say so is worse than one
    that failed, because it reads as complete."""
    body = {"ok": True, "data": {"points": list(range(4000))}}
    trimmed = fit(body, budget_tokens=200)

    assert size_in_tokens(trimmed) <= 260
    assert len(trimmed["data"]["points"]) < 4000
    assert any("dropped" in note for note in trimmed["notes"])


def test_a_result_inside_its_budget_is_left_alone():
    body = {"ok": True, "data": {"points": [1, 2, 3]}}
    assert fit(body, budget_tokens=200) == body


def test_notes_survive_serialisation(kit):
    result = Result(ok=True, data={"x": 1}).note("something worth knowing")
    assert result.to_dict()["notes"] == ["something worth knowing"]


def test_a_partial_listing_says_so_rather_than_reading_as_complete():
    """The service pages at ten by default and publishes no match count, so the only
    signal is a next link. Without checking it, a station's discharge can be absent from
    a listing that appears to be everything the station measures."""
    page = json.loads(json.dumps(LATEST))
    page["links"] = [{"rel": "next", "href": "https://example/next"}]
    fetch = serving(monitoring_locations=LOCATION, latest_continuous=page)
    with Session(service=Service(fetch=fetch)) as work:
        notes = " ".join(Toolkit(work).get_latest("USGS-07374000").to_dict()["notes"])
    assert "listing is partial" in notes


def test_a_complete_listing_makes_no_such_claim():
    """The recorded page carries a next link, having been captured at the service's own
    default of ten, which is itself the evidence that the default truncates."""
    assert any(l.get("rel") == "next" for l in LATEST["links"])

    complete = json.loads(json.dumps(LATEST))
    complete["links"] = [l for l in complete["links"] if l.get("rel") != "next"]
    fetch = serving(monitoring_locations=LOCATION, latest_continuous=complete)
    with Session(service=Service(fetch=fetch)) as work:
        notes = " ".join(Toolkit(work).get_latest("USGS-07374000").to_dict().get("notes", []))
    assert "partial" not in notes


def test_a_parameter_is_found_by_description_rather_than_by_the_services_own_name():
    """The service ignores a text query silently, answering 200 with the first rows of
    the collection, and its exact names are not guessable: specific conductance is
    published as 'Specific cond at 25C'. Neither is usable as an interface."""
    with Session(service=Service(fetch=serving())) as work:
        kit = Toolkit(work)
        found = kit.lookup_parameter("conductance").to_dict()["data"]["parameters"]
        assert found[0]["parameter_code"] == "00095"

        assert kit.lookup_parameter("00060").to_dict()["data"]["parameter_code"] == "00060"

        missing = kit.lookup_parameter("sausages")
        assert missing.error == ErrorCode.NO_DATA
        assert "not searchable" in missing.repair
