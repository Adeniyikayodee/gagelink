"""The two datasets that are files rather than services.

Both readers are written to the published format specifications and exercised against
files built here to those specifications. Neither has been run against the real
distribution, which is a gap the tests cannot close and the module says so.
"""

import struct
from pathlib import Path

import pytest
from quantity_guard import Q

from gagelink.datasets import (
    CAMELS_DIMENSIONLESS,
    CAMELS_UNITS,
    DatasetNotFound,
    read_camels,
    read_dbf,
    read_hydrobasins,
)


# CAMELS --------------------------------------------------------------------------------------


@pytest.fixture
def camels(tmp_path):
    (tmp_path / "camels_topo.txt").write_text(
        "gauge_id;gauge_lat;gauge_lon;elev_mean;slope_mean;area_gages2\n"
        "1013500;47.23;-68.58;250.31;21.64;2252.7\n"
        "1022500;44.60;-67.93;92.68;17.79;573.6\n"
    )
    (tmp_path / "camels_clim.txt").write_text(
        "gauge_id;p_mean;pet_mean;aridity;frac_snow\n"
        "1013500;3.13;1.97;0.63;0.31\n"
        "1022500;3.43;2.07;0.60;0.23\n"
    )
    return read_camels(tmp_path)


def test_a_bare_number_gets_the_unit_its_paper_states(camels):
    """The file says 573.6 and nothing else. The unit lives in the dataset description,
    which is the same failure as a service publishing an altitude with no datum."""
    basin = camels["01022500"]
    assert basin.attributes["area_gages2"] == Q(573.6, "kilometer**2")
    assert basin.attributes["p_mean"] == Q(3.43, "mm/day")


def test_the_leading_zero_every_other_source_keeps_is_restored(camels):
    """The tables drop it and every USGS identifier carries it, so a join against any
    other source here would silently miss."""
    assert set(camels) == {"01013500", "01022500"}


def test_tables_are_joined_on_the_catchment(camels):
    """Attributes come in separate files by theme and a question wants them together."""
    basin = camels["01013500"]
    assert "elev_mean" in basin.attributes and "p_mean" in basin.attributes


def test_a_ratio_is_left_without_a_unit_deliberately(camels):
    """Listed as dimensionless rather than absent from the table by oversight."""
    assert camels["01013500"].attributes["aridity"] == 0.63
    assert "aridity" in CAMELS_DIMENSIONLESS
    assert "aridity" not in CAMELS_UNITS


def test_only_the_labelled_attributes_are_offered_as_quantities(camels):
    labelled = camels["01013500"].labelled
    assert "elev_mean" in labelled
    assert "aridity" not in labelled


def test_a_missing_value_stays_missing(tmp_path):
    (tmp_path / "camels_topo.txt").write_text(
        "gauge_id;elev_mean\n1013500;NA\n1022500;-999\n"
    )
    read = read_camels(tmp_path)
    assert read["01013500"].attributes["elev_mean"] is None
    assert read["01022500"].attributes["elev_mean"] is None


def test_an_absent_download_says_what_to_fetch(tmp_path):
    """These are gigabyte archives with their own terms of use, which this package does
    not fetch on a caller's behalf."""
    with pytest.raises(DatasetNotFound, match="camels_topo.txt"):
        read_camels(tmp_path)


# HydroBASINS ---------------------------------------------------------------------------------


def write_dbf(path: Path, fields, rows):
    """A dBase III table, written to the format specification the reader implements."""
    header_length = 32 + 32 * len(fields) + 1
    record_length = 1 + sum(size for _, _, size in fields)
    header = struct.pack(
        "<BBBBIHH20x", 3, 126, 1, 1, len(rows), header_length, record_length
    )
    descriptors = b"".join(
        name.encode("latin-1").ljust(11, b"\x00")
        + kind.encode("latin-1")
        + b"\x00" * 4
        + bytes([size])
        + b"\x00" * 15
        for name, kind, size in fields
    )
    body = b""
    for row in rows:
        body += b" "
        for name, _, size in fields:
            body += str(row.get(name, "")).encode("latin-1")[:size].rjust(size)
    path.write_bytes(header + descriptors + b"\r" + body + b"\x1a")


@pytest.fixture
def basins(tmp_path):
    write_dbf(
        tmp_path / "hybas_na_lev06_v1c.dbf",
        [("HYBAS_ID", "N", 12), ("NEXT_DOWN", "N", 12), ("SUB_AREA", "N", 12),
         ("UP_AREA", "N", 12), ("ORDER", "N", 3)],
        [
            {"HYBAS_ID": 7060000010, "NEXT_DOWN": 7060000020, "SUB_AREA": 1523.4,
             "UP_AREA": 45230.1, "ORDER": 3},
            {"HYBAS_ID": 7060000020, "NEXT_DOWN": 0, "SUB_AREA": 880.2,
             "UP_AREA": 46110.3, "ORDER": 4},
        ],
    )
    return read_hydrobasins(tmp_path / "hybas_na_lev06_v1c.dbf")


def test_areas_arrive_as_quantities(basins):
    assert basins[0].area == Q(1523.4, "kilometer**2")
    assert basins[0].upstream_area == Q(45230.1, "kilometer**2")


def test_a_basin_draining_to_the_sea_has_no_downstream(basins):
    """NEXT_DOWN is zero there, which is a sentinel for absence rather than a basin id."""
    assert basins[0].downstream_id == "7060000020"
    assert basins[1].downstream_id is None


def test_the_geometry_is_never_opened(basins, tmp_path):
    """A basin boundary is thousands of coordinate pairs, and this package has taken the
    position everywhere else that a polygon is not an answer."""
    assert not (tmp_path / "hybas_na_lev06_v1c.shp").exists()
    assert all("geometry" not in b.attributes for b in basins)


def test_a_shape_file_path_is_redirected_to_its_attribute_table(tmp_path, basins):
    """So a caller can pass the name they have without knowing which sibling holds what."""
    read = read_hydrobasins(tmp_path / "hybas_na_lev06_v1c.shp")
    assert len(read) == 2


def test_reading_stops_at_the_limit(tmp_path, basins):
    assert len(read_hydrobasins(tmp_path / "hybas_na_lev06_v1c.dbf", limit=1)) == 1


def test_a_deleted_record_is_skipped(tmp_path):
    path = tmp_path / "t.dbf"
    write_dbf(path, [("A", "N", 4)], [{"A": 1}, {"A": 2}])
    raw = bytearray(path.read_bytes())
    header_length = struct.unpack("<H", raw[8:10])[0]
    raw[header_length] = ord("*")
    path.write_bytes(bytes(raw))
    assert [row["A"] for row in read_dbf(path)] == [2]


def test_a_missing_attribute_table_says_what_the_format_is(tmp_path):
    with pytest.raises(DatasetNotFound, match=r"\.dbf"):
        read_hydrobasins(tmp_path / "nothing.dbf")


def test_a_truncated_file_is_refused_rather_than_parsed(tmp_path):
    path = tmp_path / "short.dbf"
    path.write_bytes(b"\x03\x00")
    with pytest.raises(DatasetNotFound, match="too short"):
        list(read_dbf(path))
