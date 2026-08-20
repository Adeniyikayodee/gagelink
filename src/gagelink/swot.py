"""Satellite water surface elevation, from the SWOT mission through NASA's Hydrocron.

SWOT measures the height of rivers and lakes from orbit, which fills in the reaches no
gauge stands on. It is the one source here that is neither an observation from an
instrument in the water nor a model, and it needs handling that neither of those does.

Three things about this payload matter and none is signposted in it.

The elevation is referenced to the EGM2008 geoid, not to NAVD88 and not to any gage datum.
An elevation from here and a stage from a gage are both lengths, both plausibly in the same
range, and are measured from different surfaces, so differencing them is the error this
package exists to refuse. The datum is attached here for that reason.

Fill values are written as -999999999999.0, which is a third sentinel spelling across the
three services this package reads, after -9999 at the forecast service and -999999 at the
old USGS one. Each is dimensionally valid and each passes every check downstream.

Quality arrives as a summary flag rather than a review status, and it describes the
measurement rather than the record. No SWOT product is approved record in the sense a USGS
review letter means, so the mapping below grades conservatively and says why.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from quantity_guard import Q

from .normalise import parse_unit
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

BASE_URL = "https://soto.podaac.earthdatacloud.nasa.gov/hydrocron/v1/timeseries"

#: The geoid SWOT heights are referenced to. Registered by quantity-guard, and the offset
#: to any national datum varies with position, so none is assumed here.
DATUM = "EGM2008"

#: Anything at or beyond this magnitude is a fill value rather than a measurement. Written
#: as a threshold rather than as the exact number, because the products use more than one
#: fill and every one of them is far outside any physical range.
SENTINEL_MAGNITUDE = 1e11

#: The summary quality flag, which describes the measurement and not a review status. A
#: good SWOT observation is not approved record in the sense a USGS review letter means, so
#: the best grade offered here is provisional rather than approved.
QUALITY = {
    "0": "provisional",  # good
    "1": "estimated",    # suspect
    "2": "unverified",   # degraded
    "3": "unverified",   # bad
}

#: What can be asked for. A reach is a river segment; a node is a point along one.
FEATURES = ("Reach", "Node", "PriorLake")

#: Fields worth returning. The centreline geometry is deliberately not among them: a reach
#: is a line of a few hundred coordinate pairs and says nothing a summary does not.
FIELDS = (
    "reach_id",
    "time_str",
    "wse",
    "wse_u",
    "width",
    "slope",
    "reach_q",
)


class NoObservations(GagelinkError):
    """The mission has no passes over that feature in that window.

    A distinct failure from an unknown identifier, because SWOT revisits a given river
    every few days at best and an empty window is the normal answer rather than a mistake.
    """


def _measured(raw: Any) -> float | None:
    """A published number, or None where the product wrote a fill value."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if abs(value) >= SENTINEL_MAGNITUDE else value


@dataclass(frozen=True)
class Pass:
    """One satellite overpass of one feature."""

    feature_id: str
    observed_at: datetime | None
    elevation: Q | None = None
    uncertainty: Q | None = None
    width: Q | None = None
    slope: Q | None = None
    quality: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether the pass produced an elevation at all.

        A pass with every field filled is common: the satellite crossed, the retrieval
        failed, and the row exists to say so.
        """
        return self.elevation is not None


def pass_from(properties: Mapping[str, Any]) -> Pass:
    """Build one pass from a Hydrocron feature's properties.

    Each value carries its unit in a sibling field, so nothing is read in an assumed unit.
    """
    grade = QUALITY.get(str(properties.get("reach_q") or "").strip())

    def quantity(field: str, datum: str | None = None) -> Q | None:
        value = _measured(properties.get(field))
        unit = properties.get(f"{field}_units")
        if value is None or not unit:
            return None
        return Q(value, parse_unit(unit), datum=datum, quality=grade)

    moment = properties.get("time_str")
    return Pass(
        feature_id=str(properties.get("reach_id") or properties.get("node_id") or ""),
        observed_at=(
            datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
            if moment and str(moment)[0].isdigit()
            else None
        ),
        elevation=quantity("wse", DATUM),
        # The uncertainty is a length but not an elevation, so it carries no datum: it is
        # a spread about a value rather than a position above a surface.
        uncertainty=quantity("wse_u"),
        width=quantity("width"),
        slope=quantity("slope"),
        quality=grade,
    )


class Satellite:
    """A client for the Hydrocron time series service."""

    def __init__(
        self,
        *,
        fetch: Fetch | None = None,
        cache: Cache | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.base_url = base_url
        self._fetch: Fetch = fetch or _http
        self._cache: Cache = cache if cache is not None else MemoryCache()

    def url_for(
        self, feature: str, feature_id: str, start: str, end: str
    ) -> str:
        if feature not in FEATURES:
            raise ValueError(f"unknown feature {feature!r}; use one of {FEATURES}")
        query = urllib.parse.urlencode(
            {
                "feature": feature,
                "feature_id": feature_id,
                "start_time": f"{start}T00:00:00Z",
                "end_time": f"{end}T00:00:00Z",
                "output": "geojson",
                "fields": ",".join(FIELDS),
            }
        )
        return f"{self.base_url}?{query}"

    def passes(
        self,
        feature_id: str,
        start: str,
        end: str,
        feature: str = "Reach",
    ) -> tuple[list[Pass], Retrieval]:
        """Every overpass of a feature in a window, with the record of asking."""
        url = self.url_for(feature, feature_id, start, end)
        cached = self._cache.get(url)
        if cached is not None:
            return _passes(json.loads(cached)), Retrieval.of(
                f"swot/{feature.lower()}/{feature_id}", url, {}, 200, cached,
                Quota(), from_cache=True,
            )

        status, headers, body = self._fetch(
            url, {"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        if status == 404:
            raise NoObservations(f"the mission holds no passes for {feature_id}")
        if status >= 400:
            raise ServiceUnavailable(
                f"the satellite service answered {status} for {feature_id}{_explain(body)}"
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"the satellite service answered {status} with a body that is not JSON"
            ) from exc

        self._cache.set(url, body)
        return _passes(payload), Retrieval.of(
            f"swot/{feature.lower()}/{feature_id}", url, {}, status, body,
            Quota.from_headers(headers),
        )


def _passes(payload: Mapping[str, Any]) -> list[Pass]:
    """Every pass in a response, in the order the mission observed them."""
    results = payload.get("results") or {}
    features = (results.get("geojson") or {}).get("features") or []
    found = [pass_from(f.get("properties") or {}) for f in features]
    return sorted(found, key=lambda p: p.observed_at or datetime.min.replace(tzinfo=None))
