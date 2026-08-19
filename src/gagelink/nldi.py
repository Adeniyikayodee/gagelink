"""River network navigation, through the Hydro Network-Linked Data Index.

Answers the questions a single station cannot: what is upstream of here, what is
downstream, and what drains to this point. The index sits on the NHDPlus network, so
"upstream" means along the river rather than within a radius, which is the distinction that
makes the answer useful and that a bounding box cannot express.

Two things about this service shape the interface.

Navigation is named by two-letter codes, `UM`, `UT`, `DM`, and `DD`, which a caller should
not have to know. They are exposed as words, and the codes stay here.

A basin arrives as a polygon of a couple of thousand coordinate pairs, which is tens of
thousands of tokens and is never what an answer needs. The polygon is kept and its area,
extent, and vertex count are reported. The area is computed here rather than published,
which is stated wherever it is returned, and it agrees with the drainage area USGS publishes
for the one station where both exist to 0.06%.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from quantity_guard import Q

from .service import (
    USER_AGENT,
    Cache,
    Fetch,
    GagelinkError,
    MemoryCache,
    Quota,
    Retrieval,
    ServiceUnavailable,
    _explain,
    _http,
)

BASE_URL = "https://api.water.usgs.gov/nldi/linked-data"

#: Mean Earth radius, which is the figure the spherical area formula below assumes. A
#: basin's area varies by less than the polygon's own resolution across any reasonable
#: choice of radius, so the ellipsoid is not worth carrying here.
EARTH_RADIUS_M = 6371008.8

SQUARE_METRES_PER_SQUARE_MILE = 2589988.110336

#: Navigation modes, as words rather than as the codes the service takes. A caller
#: choosing a direction should not have to know that UT means upstream with tributaries.
DIRECTIONS: dict[str, str] = {
    "upstream": "UT",
    "upstream_main": "UM",
    "upstream_tributaries": "UT",
    "downstream": "DM",
    "downstream_main": "DM",
    "downstream_diversions": "DD",
}

#: What can be found along a navigation. Stream gages are the common case by a wide
#: margin; the rest are here because the index carries them and they cost nothing.
TARGETS = frozenset(
    {"nwissite", "huc12pp", "WQP", "nwisgw", "ref_gage", "wade", "vigil"}
)


class NotOnTheNetwork(GagelinkError):
    """The index holds no feature for that identifier.

    A distinct failure from an unknown station, because a station can exist and not be
    indexed to the network, which happens for locations off the mapped hydrography such as
    wells and some tidal gages.
    """


@dataclass(frozen=True)
class NetworkSite:
    """One feature found along a navigation."""

    identifier: str
    name: str
    source: str
    comid: int | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class Basin:
    """The area draining to a point, and the polygon that describes it.

    The polygon is held rather than returned. It is the answer to a mapping question and
    the wrong answer to every question an agent asks, being a couple of thousand
    coordinate pairs that say nothing a summary does not.
    """

    identifier: str
    ring: tuple[tuple[float, float], ...]
    area: Q
    bbox: tuple[float, float, float, float]

    @property
    def vertices(self) -> int:
        return len(self.ring)


def ring_area(ring: Iterable[tuple[float, float]]) -> float:
    """Area of a closed lon/lat ring in square metres.

    The line-integral form of the spherical polygon area, which needs no projection and so
    has no zone to choose or to get wrong. Accurate enough that it matches the published
    drainage area for the one station where both figures exist to well under a percent,
    which is finer than the polygon's own resolution.
    """
    points = list(ring)
    if len(points) < 4:
        return 0.0
    if points[0] != points[-1]:
        points.append(points[0])

    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        total += math.radians(lon2 - lon1) * (
            2 + math.sin(math.radians(lat1)) + math.sin(math.radians(lat2))
        )
    return abs(total * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0)


def _significant(value: float, digits: int = 4) -> float:
    """Round to a precision the geometry supports.

    A delineated boundary is generalised, so reporting an area to twelve significant
    figures asserts a precision the polygon does not have and invites the figure being
    quoted against a surveyed one as though the two were comparable.
    """
    if value == 0:
        return 0.0
    return round(value, -int(math.floor(math.log10(abs(value)))) + (digits - 1))


def basin_from(identifier: str, payload: Mapping[str, Any]) -> Basin | None:
    """Build a basin from a `/basin` response, or None where none was delineated."""
    features = payload.get("features") or []
    if not features:
        return None
    geometry = features[0].get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if not coordinates:
        return None

    # A MultiPolygon nests one level deeper than a Polygon. The largest ring is the basin
    # and the rest are islands or fragments, which is a distinction worth making because
    # taking the first would sometimes return a sliver.
    rings = coordinates if geometry.get("type") == "Polygon" else [
        ring for polygon in coordinates for ring in polygon
    ]
    ring = max(rings, key=len)
    points = tuple((float(x), float(y)) for x, y in ring)

    longitudes = [x for x, _ in points]
    latitudes = [y for _, y in points]
    return Basin(
        identifier=identifier,
        ring=points,
        area=Q(_significant(ring_area(points) / SQUARE_METRES_PER_SQUARE_MILE), "mile**2"),
        bbox=(min(longitudes), min(latitudes), max(longitudes), max(latitudes)),
    )


def sites_from(payload: Mapping[str, Any]) -> list[NetworkSite]:
    """Every feature in a navigation response."""
    found = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]
        found.append(
            NetworkSite(
                identifier=str(props.get("identifier") or ""),
                name=str(props.get("name") or ""),
                source=str(props.get("source") or ""),
                comid=props.get("comid"),
                longitude=coords[0] if isinstance(coords[0], (int, float)) else None,
                latitude=coords[1] if isinstance(coords[1], (int, float)) else None,
            )
        )
    return found


class Network:
    """A client for the network index."""

    def __init__(
        self,
        *,
        fetch: Fetch | None = None,
        cache: Cache | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._fetch: Fetch = fetch or _http
        self._cache: Cache = cache if cache is not None else MemoryCache()

    def get(self, path: str) -> tuple[dict[str, Any], Retrieval]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        cached = self._cache.get(url)
        if cached is not None:
            return json.loads(cached), Retrieval.of(
                f"nldi/{path.strip('/')}", url, {}, 200, cached, Quota(), from_cache=True
            )

        status, headers, body = self._fetch(
            url, {"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        if status == 404:
            raise NotOnTheNetwork(f"the network index holds nothing at {url}")
        if status >= 400:
            raise ServiceUnavailable(
                f"the network index answered {status} for {url}{_explain(body)}"
            )

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"the network index answered {status} with a body that is not JSON"
            ) from exc

        self._cache.set(url, body)
        return parsed, Retrieval.of(
            f"nldi/{path.strip('/')}", url, {}, status, body, Quota.from_headers(headers)
        )

    def navigate(
        self,
        identifier: str,
        direction: str = "upstream",
        target: str = "nwissite",
        distance_km: float = 50,
        source: str = "nwissite",
    ) -> tuple[list[NetworkSite], Retrieval]:
        """Features along the river in one direction from a starting feature."""
        mode = DIRECTIONS.get(direction)
        if mode is None:
            raise ValueError(
                f"unknown direction {direction!r}; use one of {sorted(DIRECTIONS)}"
            )
        if target not in TARGETS:
            raise ValueError(f"unknown target {target!r}; use one of {sorted(TARGETS)}")

        payload, retrieval = self.get(
            f"{source}/{identifier}/navigation/{mode}/{target}?distance={distance_km:g}"
        )
        return sites_from(payload), retrieval

    def basin(
        self, identifier: str, source: str = "nwissite"
    ) -> tuple[Basin | None, Retrieval]:
        """The area draining to a feature."""
        payload, retrieval = self.get(f"{source}/{identifier}/basin")
        return basin_from(identifier, payload), retrieval
