"""Network navigation, and the polygon that must not reach a context window."""

import json
from pathlib import Path

import pytest

from gagelink import ErrorCode, Network, Session, Toolkit, ring_area
from gagelink.nldi import DIRECTIONS, NotOnTheNetwork, basin_from
from gagelink.service import ServiceUnavailable

FIXTURES = Path(__file__).parent / "fixtures"
NAVIGATION = json.loads((FIXTURES / "nldi_upstream_01646500.json").read_text())
BASIN = json.loads((FIXTURES / "nldi_basin_01646500.json").read_text())

#: What USGS publishes as the drainage area for this station, from its site record.
PUBLISHED_AREA_MI2 = 11560.0


def serving(payload, status=200):
    def fetch(url, headers):
        return status, {}, json.dumps(payload) if status < 400 else ""

    return fetch


# Area ------------------------------------------------------------------------------------


def test_the_computed_area_matches_the_published_drainage_area():
    """The check that says the spherical area formula is right. A projection would need a
    zone chosen, and choosing one wrongly is a silent error of exactly the kind this
    package exists to prevent, so there is no projection."""
    basin = basin_from("USGS-01646500", BASIN)
    assert basin.area.to("mile**2").magnitude == pytest.approx(
        PUBLISHED_AREA_MI2, rel=0.005
    )


def test_the_area_is_rounded_to_a_precision_the_polygon_supports():
    """Twelve significant figures from a generalised boundary invites the figure being
    quoted against a surveyed one as though they were comparable. Four is what the
    geometry supports, so the raw 11552.5685709 is reported as 11550."""
    assert basin_from("USGS-01646500", BASIN).area.magnitude == 11550.0


def test_an_unclosed_ring_is_closed_before_it_is_measured():
    square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]
    assert ring_area(square) == pytest.approx(ring_area(square + [square[0]]))


def test_a_degenerate_ring_has_no_area():
    assert ring_area([(0.0, 0.0), (1.0, 1.0)]) == 0.0


# The polygon --------------------------------------------------------------------------


def test_the_polygon_is_kept_rather_than_returned():
    """1,750 coordinate pairs answer a mapping question and no question an agent asks."""
    with Session(network=Network(fetch=serving(BASIN))) as work:
        out = Toolkit(work).get_basin("USGS-01646500").to_dict()

    assert out["data"]["polygon_vertices"] == 1750
    assert "ring" not in json.dumps(out)
    assert len(json.dumps(out)) < 1200
    assert work.basins["USGS-01646500"].vertices == 1750


def test_the_computed_area_says_it_is_computed():
    """So it is not quoted against a surveyed figure as though the two were the same."""
    with Session(network=Network(fetch=serving(BASIN))) as work:
        notes = " ".join(Toolkit(work).get_basin("USGS-01646500").to_dict()["notes"])
    assert "computed" in notes
    assert "site record is the figure to quote" in notes


def test_the_largest_ring_is_the_basin():
    """Taking the first would sometimes return a sliver, since the response can carry
    islands and fragments alongside the boundary."""
    payload = {
        "features": [
            {
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [
                        [[[0.0, 0.0], [0.0, 0.1], [0.1, 0.1], [0.0, 0.0]]],
                        [[[0.0, 0.0], [0.0, 2.0], [2.0, 2.0], [2.0, 0.0], [0.0, 0.0]]],
                    ],
                }
            }
        ]
    }
    assert basin_from("x", payload).vertices == 5


def test_no_delineation_is_reported_rather_than_guessed():
    with Session(network=Network(fetch=serving({"features": []}))) as work:
        result = Toolkit(work).get_basin("USGS-99999999")
    assert result.error == ErrorCode.NO_DATA
    assert "coastal" in result.repair


# Navigation ---------------------------------------------------------------------------


def test_navigation_follows_the_river_and_not_a_radius():
    with Session(network=Network(fetch=serving(NAVIGATION))) as work:
        out = Toolkit(work).navigate_network(
            "USGS-01646500", "upstream_main", 50, limit=5
        ).to_dict()

    assert out["data"]["count"] == 22
    assert len(out["data"]["locations"]) == 5
    assert out["data"]["locations"][0]["id"] == "USGS-01645300"


def test_a_truncated_list_says_how_many_there_were():
    """A list cut to five that does not say so reads as five having been found."""
    with Session(network=Network(fetch=serving(NAVIGATION))) as work:
        notes = " ".join(
            Toolkit(work)
            .navigate_network("USGS-01646500", "upstream_main", 50, limit=5)
            .to_dict()["notes"]
        )
    assert "22 locations were found" in notes


def test_directions_are_words_rather_than_the_services_two_letter_codes():
    """A caller should not have to know that UT means upstream with tributaries."""
    assert DIRECTIONS["upstream"] == "UT"
    assert DIRECTIONS["upstream_main"] == "UM"

    with Session(network=Network(fetch=serving(NAVIGATION))) as work:
        result = Toolkit(work).navigate_network("USGS-01646500", "UT", 50)
    assert result.error == ErrorCode.INVALID_ARGUMENTS
    assert "upstream_main" in result.repair


def test_a_location_off_the_network_says_what_is_still_available():
    """A station can exist and not be indexed, which happens for wells and some tidal
    gages, and that is not the same failure as an unknown identifier."""
    with Session(network=Network(fetch=serving(None, status=404))) as work:
        result = Toolkit(work).navigate_network("USGS-99999999", "upstream", 50)
    assert result.error == ErrorCode.NO_DATA
    assert "get_latest" in result.repair


def test_an_empty_navigation_suggests_the_wider_search():
    with Session(network=Network(fetch=serving({"features": []}))) as work:
        result = Toolkit(work).navigate_network("USGS-01646500", "upstream_main", 1)
    assert result.error == ErrorCode.NO_DATA
    assert "upstream rather than upstream_main" in result.repair


def test_a_service_error_is_not_an_absent_network():
    with pytest.raises(ServiceUnavailable):
        Network(fetch=serving(None, status=503)).navigate("USGS-01646500")


def test_navigation_is_recorded_for_the_manifest():
    with Session(network=Network(fetch=serving(NAVIGATION))) as work:
        Toolkit(work).navigate_network("USGS-01646500", "upstream_main", 50)
        collections = [r["collection"] for r in work.manifest()["retrievals"]]
    assert collections[0].startswith("nldi/")
    assert "navigation/UM" in collections[0]
