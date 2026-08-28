"""Vertical datum conversion, which is the refusal this package is known for turning into
an answer.

The refusal was right and stays right where the conversion cannot be made. What changed is
that for most stations it can be: across 3,397 gaged stream stations sampled in four states,
58% publish their offset on NGVD29 while a modern survey is on NAVD88, so the majority case
was a comparison this package declined and NOAA can settle.
"""

import json
from pathlib import Path

import pytest
from quantity_guard import Q, datums

from gagelink import Service, Session, Toolkit
from gagelink.vdatum import (
    FRAMES,
    UNREACHABLE_STATES,
    Frame,
    Conversion,
    ConversionRefused,
    NoCoverage,
    VerticalDatums,
    tidal_datums,
    unreachable,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONVERTED = (FIXTURES / "vdatum_ngvd29_navd88_06730500.json").read_text()
BOULDER = json.loads((FIXTURES / "monitoring_location_06730500.json").read_text())
LOCATION = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())

EMPTY = {"type": "FeatureCollection", "features": []}


def answering(body, status=200):
    calls = []

    def fetch(url, headers):
        calls.append(url)
        return status, {}, body

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def locations(page):
    def fetch(url, headers):
        if "monitoring-locations/items" in url:
            return 200, {"X-RateLimit-Remaining": "998"}, json.dumps(page)
        return 200, {}, json.dumps(EMPTY)

    return fetch


# Asking ------------------------------------------------------------------------------------


def test_the_horizontal_frame_is_sent_with_the_vertical_one():
    """The service refuses NGVD29 without NAD27, and a caller who knows a station's
    vertical datum has no reason to know which horizontal frame it is realised against.
    Keeping the pairing here means forgetting it is impossible rather than a 412."""
    url = VerticalDatums().url_for(40.13, -105.02, Q(4860.0, "foot", datum="NGVD29"), "NAVD88")
    assert "s_v_frame=NGVD29" in url and "s_h_frame=NAD27" in url
    assert "t_v_frame=NAVD88" in url and "t_h_frame=NAD83_2011" in url


def test_a_datum_with_no_known_pairing_is_refused_before_a_request_is_spent():
    # Ordnance Datum is a real datum that quantity-guard knows and this service does not
    # cover, which is the case worth refusing: not a typo, just out of scope.
    with pytest.raises(ValueError, match="not a datum this converts"):
        VerticalDatums().url_for(51.5, -0.1, Q(10.0, "foot", datum="ODN"), "NAVD88")


def test_both_ends_are_asked_for_in_the_unit_the_question_arrived_in():
    url = VerticalDatums().url_for(40.13, -105.02, Q(4860.0, "foot", datum="NGVD29"), "NAVD88")
    assert "s_v_unit=us_ft" in url and "t_v_unit=us_ft" in url
    metres = VerticalDatums().url_for(-15.94, -46.10, Q(493.96, "meter", datum="EGM2008"), "NAVD88")
    assert "s_v_unit=m" in metres and "t_v_unit=m" in metres


# Reading -----------------------------------------------------------------------------------


def test_a_conversion_carries_the_datum_it_landed_on_and_its_uncertainty():
    """Recorded live at Boulder Creek, whose offset is published on NGVD29."""
    conversion, retrieval = VerticalDatums(fetch=answering(CONVERTED)).convert(
        40.1387777777778, -105.020222222222, Q(4860.0, "foot", datum="NGVD29")
    )
    assert conversion.elevation.magnitude == pytest.approx(4863.061)
    assert conversion.elevation.datum == "NAVD88"
    assert conversion.uncertainty.magnitude == pytest.approx(0.17)
    assert retrieval.collection == "vdatum/ngvd29/navd88"


def test_the_shift_is_feet_rather_than_inches():
    """Which is why leaving it out matters. Three feet at one station, in the same
    direction and the same magnitude as the error this package exists to refuse."""
    conversion, _ = VerticalDatums(fetch=answering(CONVERTED)).convert(
        40.1387777777778, -105.020222222222, Q(4860.0, "foot", datum="NGVD29")
    )
    assert abs(conversion.elevation.magnitude - 4860.0) > 3


def test_a_refusal_arrives_at_200_and_is_read_from_the_body_not_the_status():
    """The service answers 200 with an errorCode. Reading the status as the signal would
    take a refusal for an answer."""
    body = json.dumps({"errorCode": 412, "message": "Source Horizontal Frame should be NAD27"})
    with pytest.raises(ConversionRefused, match="Source Horizontal Frame"):
        VerticalDatums(fetch=answering(body)).convert(
            40.1, -105.0, Q(4860.0, "foot", datum="NGVD29")
        )


def test_a_fill_value_dressed_as_an_elevation_does_not_travel():
    """Outside the coverage of a conversion the service answers 200, no error code, and a
    t_z of -999999 with an empty uncertainty. It is dimensionally valid, it is in feet, and
    it passes every check downstream. It is the fourth sentinel spelling across the services
    this package reads, and it comes from the one whose job is saying what an elevation is
    measured from."""
    body = json.dumps({"t_z": "-999999", "uncertainty": ""})
    with pytest.raises(NoCoverage) as raised:
        VerticalDatums(fetch=answering(body)).convert(
            40.1, -105.0, Q(1500.0, "foot", datum="NGVD29")
        )
    assert "999999" not in str(raised.value)


def test_a_body_that_is_not_json_is_a_service_failure_rather_than_a_parse_crash():
    from gagelink.service import ServiceUnavailable

    with pytest.raises(ServiceUnavailable):
        VerticalDatums(fetch=answering("<html>maintenance</html>")).convert(
            40.1, -105.0, Q(4860.0, "foot", datum="NGVD29")
        )


# Uncertainty --------------------------------------------------------------------------------


def test_the_two_uncertainties_combine_and_the_larger_one_decides():
    """At Boulder Creek the station's own accuracy is 10 ft and the conversion's is 0.17.
    A freeboard quoted with only the conversion's uncertainty would claim a precision the
    station never had, by a factor of sixty."""
    conversion = Conversion(
        elevation=Q(4863.061, "foot", datum="NAVD88"),
        uncertainty=Q(0.17, "foot"),
        source_datum="NGVD29",
        target_datum="NAVD88",
    )
    combined = conversion.combined_with(Q(10.0, "foot"))
    assert combined.magnitude == 10.0


def test_a_station_with_no_published_accuracy_keeps_the_conversion_uncertainty_alone():
    conversion = Conversion(
        elevation=Q(4863.061, "foot", datum="NAVD88"),
        uncertainty=Q(0.17, "foot"),
        source_datum="NGVD29",
        target_datum="NAVD88",
    )
    assert conversion.combined_with(None).magnitude == pytest.approx(0.17)


# Through the session and the toolkit ----------------------------------------------------------


def kit_for(page, vdatum_body=CONVERTED, status=200):
    fetch = answering(vdatum_body, status)
    session = Session(
        service=Service(fetch=locations(page)),
        vertical=VerticalDatums(fetch=fetch),
    )
    return session, fetch


def test_the_offset_is_returned_on_the_datum_that_was_asked_for():
    session, _ = kit_for(BOULDER)
    with session as work:
        data = Toolkit(work).describe_location(
            "USGS-06730500", on_datum="NAVD88"
        ).to_dict()["data"]
    assert data["altitude_of_gage_datum"] == {"value": 4860.0, "unit": "ft", "datum": "NGVD29"}
    assert data["altitude_on_requested_datum"]["datum"] == "NAVD88"
    assert data["altitude_on_requested_datum"]["value"] == pytest.approx(4863.061)
    assert data["conversion_uncertainty"]["value"] == pytest.approx(0.17)
    # Ten feet plus a fifth of one is ten feet. Carrying the arithmetic's twelve figures
    # into the answer would be a precision claim the inputs never supported.
    assert data["offset_uncertainty"]["value"] == 10.0


def test_converting_registers_the_offset_so_a_stage_can_actually_be_shifted():
    """The note is not the point. Until now a stage on this station's own zero had no
    offset onto a national datum and `to_datum` refused; the conversion is what supplies
    one."""
    session, _ = kit_for(BOULDER)
    with session as work:
        Toolkit(work).describe_location("USGS-06730500", on_datum="NAVD88")
    assert datums.can_convert("GAGE:06730500", "NAVD88")


def test_nothing_is_asked_of_the_datum_service_when_the_offset_is_already_there():
    """Baton Rouge publishes on NAVD88. Converting NAVD88 to NAVD88 would spend a request
    to be told the number it started with."""
    session, fetch = kit_for(LOCATION)
    with session as work:
        result = Toolkit(work).describe_location(
            "USGS-07374000", on_datum="NAVD88"
        ).to_dict()
    assert fetch.calls == []
    assert "already on NAVD88" in " ".join(result["notes"])


def test_asking_twice_in_one_session_spends_one_request():
    """A conversion is a pure function of the position, the height and the pair of datums."""
    session, fetch = kit_for(BOULDER)
    with session as work:
        kit = Toolkit(work)
        kit.describe_location("USGS-06730500", on_datum="NAVD88")
        kit.describe_location("USGS-06730500", on_datum="NAVD88")
    assert len(fetch.calls) == 1


def test_a_conversion_that_cannot_be_made_leaves_the_comparison_refused():
    """Not approximated, and not silently absent: the field is missing and a note says the
    difference is still not well defined."""
    body = json.dumps({"t_z": "-999999", "uncertainty": ""})
    session, _ = kit_for(BOULDER, vdatum_body=body)
    with session as work:
        result = Toolkit(work).describe_location(
            "USGS-06730500", on_datum="NAVD88"
        ).to_dict()
    assert "altitude_on_requested_datum" not in result["data"]
    assert "stays refused" in " ".join(result["notes"])


def test_a_datum_service_that_cannot_be_reached_is_told_apart_from_one_that_says_no():
    """A failure to ask is not an answer of no, and the repairs differ: one is retried and
    the other is not."""
    session, _ = kit_for(BOULDER, vdatum_body="", status=503)
    with session as work:
        result = Toolkit(work).describe_location(
            "USGS-06730500", on_datum="NAVD88"
        ).to_dict()
    notes = " ".join(result["notes"])
    assert result["ok"] is True
    assert "failure to ask rather than an answer of no" in notes


def test_the_conversion_is_recorded_in_the_manifest_like_any_other_retrieval():
    """A converted offset is a retrieved fact, so a replay has to be able to see where it
    came from."""
    session, _ = kit_for(BOULDER)
    with session as work:
        Toolkit(work).describe_location("USGS-06730500", on_datum="NAVD88")
        collections = [r["collection"] for r in work.manifest()["retrievals"]]
    assert "vdatum/ngvd29/navd88" in collections


def test_the_pairings_this_module_knows_are_the_ones_it_will_convert():
    assert {"NGVD29", "NAVD88", "EGM2008"} <= set(FRAMES)
    assert set(tidal_datums()) == {"MLLW", "MLW", "LMSL", "MTL", "DTL", "MHW", "MHHW"}


def test_a_stage_travels_from_the_gage_zero_to_a_modern_datum_end_to_end():
    """The chain the package exists for, in one assertion. A level read from the station's
    own zero, resolved onto the datum the station publishes, and then onto the one a survey
    would be on. The third step is the one that was a refusal until now, and the README
    quotes these three numbers."""
    from gagelink.normalise import readings_from

    session, _ = kit_for(BOULDER)
    with session as work:
        Toolkit(work).describe_location("USGS-06730500", on_datum="NAVD88")
        station = work.location("USGS-06730500")
        observed = {
            "features": [
                {
                    "properties": {
                        "monitoring_location_id": "USGS-06730500",
                        "parameter_code": "00065",
                        "value": "9.11",
                        "unit_of_measure": "ft",
                        "approval_status": "Provisional",
                        "time": "2026-08-28T12:00:00+00:00",
                    }
                }
            ]
        }
        stage = readings_from(observed, station)[0].value

    assert stage.datum == "GAGE:06730500"
    assert stage.to_datum("NGVD29").magnitude == pytest.approx(4869.11)
    assert stage.to_datum("NAVD88").magnitude == pytest.approx(4872.17, abs=0.01)


# Tidal datums ---------------------------------------------------------------------------------


TIDAL = (FIXTURES / "vdatum_navd88_mllw_grand_isle.json").read_text()
GEOID = (FIXTURES / "vdatum_egm2008_navd88_baton_rouge.json").read_text()


def test_a_tidal_datum_is_asked_for_like_any_other():
    """Recorded live at the Grand Isle tide station. Tidal datums answer a different
    question from the orthometric ones: not how high above the land, but how high relative
    to the tide, which is the frame coastal flood work is stated in and which no gage
    publishes."""
    conversion, _ = VerticalDatums(fetch=answering(TIDAL)).convert(
        29.263, -89.957, Q(0.0, "foot", datum="NAVD88"), "MLLW"
    )
    assert conversion.elevation.datum == "MLLW"
    assert conversion.elevation.magnitude == pytest.approx(0.354)
    assert conversion.uncertainty.magnitude == pytest.approx(0.529)


def test_a_tidal_transformation_can_be_less_certain_than_the_shift_it_makes():
    """At Grand Isle the uncertainty is 0.529 ft on a shift of 0.354. The shift is still
    the right correction; it is not a figure to quote to the inch, and a caller told only
    the shift would never know that."""
    conversion, _ = VerticalDatums(fetch=answering(TIDAL)).convert(
        29.263, -89.957, Q(0.0, "foot", datum="NAVD88"), "MLLW"
    )
    assert conversion.uncertainty.magnitude > abs(conversion.elevation.magnitude)


def test_every_tidal_datum_offered_can_be_carried_by_a_quantity():
    """A conversion produces a quantity carrying its target datum, and a quantity refuses a
    datum the registry has not been told about. Several of these are not registered by
    quantity-guard, so this module registers them at import."""
    for name in tidal_datums():
        assert Q(1.0, "foot", datum=name).datum == name


def test_the_tidal_datums_are_asked_for_against_the_modern_horizontal_frame():
    url = VerticalDatums().url_for(29.263, -89.957, Q(0.0, "foot", datum="NAVD88"), "MHHW")
    assert "t_v_frame=MHHW" in url and "t_h_frame=NAD83_2011" in url


# The satellite geoid ---------------------------------------------------------------------------


def test_the_geoid_a_satellite_measures_against_carries_its_own_pairing():
    """EGM2008 is refused without WGS84_G1674, and then refused again until the geoid model
    is named as well. Neither is derivable from the datum a payload states."""
    url = VerticalDatums().url_for(
        30.445, -91.191944, Q(10.0, "meter", datum="EGM2008"), "NAVD88"
    )
    assert "s_v_frame=EGM2008" in url
    assert "s_h_frame=WGS84_G1674" in url
    assert "s_v_geoid=egm2008" in url
    assert "t_v_geoid=geoid18" in url


def test_a_geoid_conversion_publishes_no_uncertainty_and_does_not_invent_one():
    """The service answers `NaN`, which is a float in Python, is not caught by a magnitude
    test because every comparison against it is False, and would turn a total into NaN if
    it were added in quadrature. It arrives here as an absence."""
    conversion, _ = VerticalDatums(fetch=answering(GEOID)).convert(
        30.445, -91.191944, Q(10.0, "meter", datum="EGM2008"), "NAVD88"
    )
    assert conversion.elevation.magnitude == pytest.approx(10.045)
    assert conversion.uncertainty is None


def test_a_not_a_number_uncertainty_does_not_survive_being_combined():
    body = json.dumps({"t_z": "10.045", "uncertainty": "NaN"})
    conversion, _ = VerticalDatums(fetch=answering(body)).convert(
        30.445, -91.191944, Q(10.0, "meter", datum="EGM2008"), "NAVD88"
    )
    combined = conversion.combined_with(Q(0.5, "meter"))
    assert combined.magnitude == 0.5


def test_a_conversion_answers_in_the_unit_the_question_arrived_in():
    """A station offset is in feet and a satellite elevation is in metres. Neither should
    have to travel through the other."""
    conversion, _ = VerticalDatums(fetch=answering(GEOID)).convert(
        30.445, -91.191944, Q(10.0, "meter", datum="EGM2008"), "NAVD88"
    )
    assert str(conversion.elevation.units) == "meter"


# Coverage --------------------------------------------------------------------------------------


def test_a_station_outside_the_coverage_is_refused_without_spending_a_request():
    """The geoid model this depends on covers the contiguous states, Chesapeake/Delaware,
    the West Coast and PRVI. Asking for Alaska is answered by the service; asking the
    station record is answered for nothing, and the allowance is fifty an hour."""
    alaskan = json.loads(json.dumps(BOULDER))
    alaskan["features"][0]["properties"]["state_name"] = "Alaska"
    session, fetch = kit_for(alaskan)
    with session as work:
        result = Toolkit(work).describe_location(
            "USGS-06730500", on_datum="NAVD88"
        ).to_dict()
    assert fetch.calls == []
    assert "does not cover Alaska" in " ".join(result["notes"])


@pytest.mark.parametrize("state", sorted(UNREACHABLE_STATES))
def test_every_state_named_unreachable_is_treated_as_one(state):
    assert unreachable(state) is True


# Satellite elevations ---------------------------------------------------------------------------


SWOT = json.loads((FIXTURES / "swot_reach_63470800171.json").read_text())


def american_reach(elevations=(10.0, 10.4, 9.7)):
    """A SWOT payload on the Mississippi at Baton Rouge, built rather than recorded.

    The recorded reach is in Brazil, which is the ordinary case for this mission and the
    right fixture for the refusal. It is the wrong fixture for a successful conversion: the
    datum service covers the United States, so a reach it can answer for has to be one in
    it. Built to the shape the mission publishes, at the position the recorded VDatum
    conversion was taken, so the two belong together.
    """
    return {
        "results": {
            "geojson": {
                "features": [
                    {
                        "properties": {
                            "reach_id": "74250100011",
                            "time_str": f"2024-02-0{n + 1}T12:00:00Z",
                            "wse": str(wse),
                            "wse_units": "m",
                            "wse_u": "0.11",
                            "wse_u_units": "m",
                            "reach_q": "0",
                        },
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [-91.19, 30.44],
                                [-91.191944, 30.445],
                                [-91.193, 30.45],
                            ],
                        },
                    }
                    for n, wse in enumerate(elevations)
                ]
            }
        }
    }


def satellite_kit(vdatum_body=GEOID, swot=None):
    from gagelink import Satellite

    fetch = answering(vdatum_body)
    session = Session(
        service=Service(fetch=locations(EMPTY)),
        satellite=Satellite(fetch=answering(json.dumps(swot if swot is not None else american_reach()))),
        vertical=VerticalDatums(fetch=fetch),
    )
    return session, fetch


def test_satellite_elevations_stay_on_the_geoid_unless_a_datum_is_asked_for():
    session, fetch = satellite_kit()
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "74250100011", "2024-01-01", "2024-03-01"
        ).to_dict()
    assert fetch.calls == []
    assert result["data"]["datum"] == "EGM2008"
    assert "will be refused" in " ".join(result["notes"])


def test_a_reach_outside_the_united_states_says_which_service_is_the_reason():
    """The fixture reach is in Brazil, which is the ordinary case: SWOT observes every river
    on the planet and this datum service covers one country. The service answers such a
    point with -999999, so the refusal has to be made here rather than read off the reply."""
    session, _ = satellite_kit(
        vdatum_body=json.dumps({"t_z": "-999999", "uncertainty": ""}), swot=SWOT
    )
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "63470800171", "2024-01-01", "2024-03-01", on_datum="NAVD88"
        ).to_dict()
    notes = " ".join(result["notes"])
    assert "outside the datum service's coverage" in notes
    assert "datum_separation" not in result["data"]
    assert all("elevation_on_requested_datum" not in o for o in result["data"]["observations"])


def test_one_conversion_moves_every_pass_on_a_reach():
    """The separation between two vertical surfaces is a property of the position, not of
    the height above it, checked against the service at 10, 20 and 400 m and identical to
    four decimals. So a reach costs one request rather than one per overpass."""
    session, fetch = satellite_kit()
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "74250100011", "2024-01-01", "2024-03-01", on_datum="NAVD88"
        ).to_dict()

    assert len(fetch.calls) == 1
    observations = result["data"]["observations"]
    assert len(observations) > 1
    assert all("elevation_on_requested_datum" in o for o in observations)
    assert all(o["elevation_on_requested_datum"]["datum"] == "NAVD88" for o in observations)


def test_the_shift_applied_to_each_pass_is_the_separation_and_not_the_conversion():
    """Every pass moves by the same amount, which is what makes one conversion enough. A
    reach whose passes each landed on the converted value would have lost the record."""
    session, _ = satellite_kit()
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "74250100011", "2024-01-01", "2024-03-01", on_datum="NAVD88"
        ).to_dict()

    separation = result["data"]["datum_separation"]["value"]
    shifts = [
        o["elevation_on_requested_datum"]["value"] - o["elevation"]["value"]
        for o in result["data"]["observations"]
    ]
    assert len(set(round(s, 6) for s in shifts)) == 1
    assert shifts[0] == pytest.approx(separation)


def test_an_unstated_conversion_uncertainty_is_said_rather_than_left_out():
    """The geoid conversions publish none. A caller who is not told that will read the
    mission's per-pass uncertainty as covering the shift, and it does not."""
    session, _ = satellite_kit()
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "74250100011", "2024-01-01", "2024-03-01", on_datum="NAVD88"
        ).to_dict()
    assert "publishes no uncertainty for a geoid conversion" in " ".join(result["notes"])


def test_a_separation_no_two_of_these_datums_could_have_is_discarded():
    """The check that caught this: a conversion answered for one reach applied to another
    produces a shift of hundreds of metres, in the right unit, on a quantity carrying the
    right datum, and nothing downstream can tell. Geoid-to-NAVD88 across the contiguous
    states is within a couple of metres."""
    session, _ = satellite_kit(swot=american_reach(elevations=(493.9646,)))
    with session as work:
        result = Toolkit(work).get_satellite_passes(
            "74250100011", "2024-01-01", "2024-03-01", on_datum="NAVD88"
        ).to_dict()

    notes = " ".join(result["notes"])
    assert "larger than any real separation" in notes
    assert "datum_separation" not in result["data"]
    assert all(
        "elevation_on_requested_datum" not in o for o in result["data"]["observations"]
    )
