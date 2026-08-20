"""Static datasets, read from a local copy.

CAMELS and HydroSHEDS have no service behind them. They are files, distributed as archives
of a few hundred megabytes to several gigabytes, downloaded once and kept. So the reader
takes a path rather than a URL, and there is no cache, no quota, and no retry, because
there is no request.

What there is, and what makes them worth reading through this package rather than with a
spreadsheet, is a table of bare numbers whose units live in a paper. A CAMELS catchment
has a `p_mean` of 3.1 and an `area_gages2` of 573, and nothing in the file says the first
is millimetres per day and the second square kilometres. That is the same failure as a
service publishing an altitude with no datum, arriving in a different medium.

Both readers are written to the published format specifications and are exercised against
files built here to those specifications. Neither has been run against the real
distribution, which is a gap worth closing the first time someone has a copy to hand.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from quantity_guard import Q

from .service import GagelinkError

# CAMELS ------------------------------------------------------------------------------------

#: Units for the catchment attributes, from the dataset description rather than from the
#: files, which state none. Anything absent from this table is returned as a bare number
#: and said to be one, since a wrong unit is worse than an acknowledged missing one.
CAMELS_UNITS: dict[str, str] = {
    # Topography
    "gauge_lat": "degree",
    "gauge_lon": "degree",
    "elev_mean": "meter",
    "slope_mean": "meter/kilometer",
    "area_gages2": "kilometer**2",
    "area_geospa_fabric": "kilometer**2",
    # Climate
    "p_mean": "mm/day",
    "pet_mean": "mm/day",
    "high_prec_dur": "day",
    "low_prec_dur": "day",
    "high_prec_freq": "day/year",
    "low_prec_freq": "day/year",
    # Hydrology
    "q_mean": "mm/day",
    "q5": "mm/day",
    "q95": "mm/day",
    "high_q_dur": "day",
    "low_q_dur": "day",
    "high_q_freq": "day/year",
    "low_q_freq": "day/year",
    "hfd_mean": "day",
    "zero_q_freq": "percent",
    # Soil and geology
    "soil_depth_pelletier": "meter",
    "soil_depth_statsgo": "meter",
    "max_water_content": "meter",
    "geol_porostiy": "dimensionless",
    "geol_permeability": "dimensionless",
    # Vegetation
    "lai_max": "dimensionless",
    "gvf_max": "dimensionless",
}

#: Attributes that are ratios or indices, listed so an absent unit is a deliberate absence
#: rather than an oversight.
CAMELS_DIMENSIONLESS = frozenset(
    {
        "aridity",
        "frac_snow",
        "runoff_ratio",
        "baseflow_index",
        "stream_elas",
        "slope_fdc",
        "p_seasonality",
        "frac_forest",
        "sand_frac",
        "silt_frac",
        "clay_frac",
        "carbonate_rocks_frac",
    }
)

#: The delimiter the attribute tables use, which is not a comma.
CAMELS_DELIMITER = ";"


class DatasetNotFound(GagelinkError):
    """No local copy of a dataset at the path given.

    These are files rather than services, so the failure is a missing download and the
    repair is to fetch one, which this package deliberately does not do on a caller's
    behalf: the archives are gigabytes and carry their own terms of use.
    """


@dataclass(frozen=True)
class Catchment:
    """One CAMELS basin, with its attributes labelled."""

    id: str
    attributes: dict[str, Any]

    def get(self, name: str) -> Q | float | str | None:
        return self.attributes.get(name)

    @property
    def labelled(self) -> dict[str, Q]:
        """Only the attributes carrying a unit, which is what most questions want."""
        return {k: v for k, v in self.attributes.items() if isinstance(v, Q)}


def _value(name: str, raw: str) -> Q | float | str | None:
    """One attribute, labelled where the dataset description gives a unit."""
    text = raw.strip()
    if not text or text.upper() in {"NA", "NAN", "-999"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    if name in CAMELS_UNITS:
        return Q(number, CAMELS_UNITS[name])
    return number


def read_camels(directory: str | Path) -> dict[str, Catchment]:
    """Read every CAMELS attribute table in a directory, merged by catchment.

    The tables are separate files by theme and share a `gauge_id`, so they are joined
    here. A catchment present in one table and absent from another keeps what it has
    rather than being dropped.
    """
    root = Path(directory)
    tables = sorted(root.glob("camels_*.txt"))
    if not tables:
        raise DatasetNotFound(
            f"no CAMELS attribute tables under {root}. They are named camels_topo.txt, "
            f"camels_clim.txt, camels_hydro.txt and so on, and come from the attributes "
            f"archive rather than the time series one."
        )

    merged: dict[str, dict[str, Any]] = {}
    for table in tables:
        for row in _delimited(table, CAMELS_DELIMITER):
            identifier = str(row.get("gauge_id") or "").strip()
            if not identifier:
                continue
            # Left padded to eight digits, since the files drop the leading zero that
            # every USGS station number carries and that every other source keeps.
            identifier = identifier.zfill(8)
            attributes = merged.setdefault(identifier, {})
            for name, raw in row.items():
                if name != "gauge_id":
                    attributes[name] = _value(name, raw)

    return {k: Catchment(id=k, attributes=v) for k, v in merged.items()}


def _delimited(path: Path, delimiter: str) -> Iterator[dict[str, str]]:
    lines = path.read_text().splitlines()
    if not lines:
        return
    header = [h.strip() for h in lines[0].split(delimiter)]
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split(delimiter)
        yield dict(zip(header, cells))


# HydroSHEDS ----------------------------------------------------------------------------------

#: Units for the HydroBASINS attributes, from its technical documentation. The attribute
#: table states none, as with CAMELS.
HYDROBASINS_UNITS: dict[str, str] = {
    "SUB_AREA": "kilometer**2",
    "UP_AREA": "kilometer**2",
    "DIST_SINK": "kilometer",
    "DIST_MAIN": "kilometer",
}


@dataclass(frozen=True)
class HydroBasin:
    """One HydroBASINS polygon's attributes, without its geometry.

    Named apart from the `Basin` the network index returns, which is a delineated
    catchment above a point and a different object entirely. One package holding two
    things called a basin is a confusion waiting to be acted on.

    The geometry is deliberately not read. It lives in the sibling shape file, a basin
    boundary is thousands of coordinate pairs, and this package has taken the position
    everywhere else that a polygon is not an answer.
    """

    id: str
    attributes: dict[str, Any]

    @property
    def area(self) -> Q | None:
        return self.attributes.get("SUB_AREA")

    @property
    def upstream_area(self) -> Q | None:
        return self.attributes.get("UP_AREA")

    @property
    def downstream_id(self) -> str | None:
        """The basin this one drains into, or None where it drains to the sea."""
        nxt = self.attributes.get("NEXT_DOWN")
        return None if nxt in (None, 0, "0") else str(nxt)


def read_hydrobasins(path: str | Path, limit: int | None = None) -> list[HydroBasin]:
    """Read basin attributes from a HydroBASINS attribute table.

    Reads the dBase file beside the shape file, which is a documented and stable format
    that needs no dependency, and does not open the geometry at all.
    """
    table = Path(path)
    if table.suffix.lower() == ".shp":
        table = table.with_suffix(".dbf")
    if not table.exists():
        raise DatasetNotFound(
            f"no attribute table at {table}. HydroBASINS is distributed as a shape file "
            f"with a .dbf beside it, and this reads the .dbf."
        )

    found = []
    for row in read_dbf(table, limit=limit):
        attributes: dict[str, Any] = {}
        for name, raw in row.items():
            if name in HYDROBASINS_UNITS and isinstance(raw, (int, float)):
                attributes[name] = Q(float(raw), HYDROBASINS_UNITS[name])
            else:
                attributes[name] = raw
        found.append(
            HydroBasin(id=str(row.get("HYBAS_ID") or ""), attributes=attributes)
        )
    return found


def read_dbf(path: str | Path, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Rows from a dBase III table.

    The format is fixed and old: a 32 byte header, then a 32 byte descriptor per field
    terminated by 0x0D, then fixed width records each prefixed by a deletion marker.
    Implementing it is a page of code and avoids a geospatial dependency for the sake of
    reading a table of numbers.
    """
    with open(path, "rb") as handle:
        header = handle.read(32)
        if len(header) < 32:
            raise DatasetNotFound(f"{path} is too short to be a dBase table")
        record_count, header_length, record_length = struct.unpack("<IHH", header[4:12])

        fields = []
        while True:
            descriptor = handle.read(32)
            if not descriptor or descriptor[0:1] == b"\r":
                break
            name = descriptor[:11].split(b"\x00")[0].decode("latin-1").strip()
            kind = descriptor[11:12].decode("latin-1")
            length = descriptor[16]
            fields.append((name, kind, length))

        handle.seek(header_length)
        for index in range(record_count if limit is None else min(record_count, limit)):
            record = handle.read(record_length)
            if not record or record[0:1] == b"\x1a":
                break
            if record[0:1] == b"*":
                continue  # marked deleted
            row: dict[str, Any] = {}
            offset = 1
            for name, kind, length in fields:
                raw = record[offset : offset + length].decode("latin-1").strip()
                offset += length
                row[name] = _dbf_value(kind, raw)
            yield row


def _dbf_value(kind: str, raw: str) -> Any:
    if not raw:
        return None
    if kind in "NF":
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return None
    if kind == "L":
        return raw.upper() in {"Y", "T"}
    return raw
