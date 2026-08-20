"""Forecasts and flood thresholds from the NOAA National Water Prediction Service.

This service answers the question a stage reading cannot, which is what the stage means.
A gage height of 3.02 ft is a number until it is set against the stage at which that river
floods, and the flood categories live here rather than with the observations.

Three things about this payload need handling and none of them is signposted in it.

Flow is published as `cfs` in the flood categories and as `kcfs` in the status block of the
same response, so a caller reading both and treating them alike is out by a factor of a
thousand. Both are opaque abbreviations of the kind the quantity-guard evaluation found no
model converts reliably.

Thresholds that were never set are published as -9999 rather than omitted, which is
dimensionally valid, plausible in sign only, and passes every check downstream. It is a
sentinel, and it is read as one here.

A stage is on the gage's own datum rather than on any national one. The observed stage
published here matches USGS parameter 00065 at the same station and time exactly, which is
the evidence for that reading, and it is why a flood stage can be differenced against a
gage height but not against a surveyed elevation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from quantity_guard import Q, datums

from .normalise import parse_unit

from .service import (
    USER_AGENT,
    Fetch,
    GagelinkError,
    Quota,
    Retrieval,
    ServiceUnavailable,
    _explain,
    _http,
    Cache,
    MemoryCache,
)

BASE_URL = "https://api.water.noaa.gov/nwps/v1"

#: Published where a threshold was never established. Not a low threshold, not a missing
#: key, and not a number: any arithmetic reaching it produces a plausible wrong answer.
SENTINEL = -9999

#: The service writes POSIX zone names, which state the standard and daylight
#: abbreviations together rather than naming a region.
TIMEZONES: dict[str, str] = {
    "EST5EDT": "America/New_York",
    "CST6CDT": "America/Chicago",
    "MST7MDT": "America/Denver",
    "PST8PDT": "America/Los_Angeles",
    "AKST9AKDT": "America/Anchorage",
    "MST": "Etc/GMT+7",
    "HST": "Pacific/Honolulu",
    "AST4ADT": "America/Halifax",
    "GMT": "UTC",
    "UTC": "UTC",
}

#: Categories in the order the service escalates them, which is what makes "the next
#: category up" answerable.
CATEGORY_ORDER = ("action", "minor", "moderate", "major")


class GaugeNotFound(GagelinkError):
    """No forecast gauge for that identifier.

    Distinct from a USGS location being unknown, because many gaged streams carry no
    forecast point at all and that is a fact about the river rather than a typo.
    """


def _measured(raw: Any) -> float | None:
    """A published number, or None where the service published its sentinel."""
    if raw is None:
        return None
    value = float(raw)
    return None if value <= SENTINEL else value


@dataclass(frozen=True)
class Threshold:
    """One flood category, as a stage and where published as a flow."""

    name: str
    stage: Q | None = None
    flow: Q | None = None


@dataclass(frozen=True)
class Gauge:
    """A forecast point, with the thresholds that give its readings meaning."""

    lid: str
    name: str
    usgs_id: str | None = None
    reach_id: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    stage_datum: str | None = None
    thresholds: tuple[Threshold, ...] = ()
    observed: Q | None = None
    observed_at: datetime | None = None
    observed_flow: Q | None = None
    forecast: Q | None = None
    forecast_at: datetime | None = None
    forecast_flow: Q | None = None
    observed_category: str | None = None
    forecast_category: str | None = None

    def threshold(self, name: str) -> Threshold | None:
        for entry in self.thresholds:
            if entry.name == name:
                return entry
        return None

    def freeboard_to(self, name: str) -> Q | None:
        """How far the current stage sits below a named threshold.

        Both are on the gage's own datum, so the difference is well defined and carries no
        datum, which is what a freeboard is. A comparison against a surveyed elevation is
        a different calculation and is refused one level up rather than approximated here.
        """
        target = self.threshold(name)
        if target is None or target.stage is None or self.observed is None:
            return None
        return target.stage - self.observed


def _stage_datum(payload: Mapping[str, Any]) -> str:
    """The frame a stage at this gauge is measured from.

    Where the gauge names a USGS station, its stages are on that station's datum, which is
    the frame gagelink already registers from the site record, so the two services' stages
    become comparable. Where it does not, the gauge gets a frame of its own and nothing is
    assumed about how it relates to anything else.
    """
    usgs = str(payload.get("usgsId") or "").strip()
    return f"GAGE:{usgs}" if usgs else f"NWPS:{payload.get('lid')}"


def gauge_from(payload: Mapping[str, Any]) -> Gauge:
    """Build a gauge from one `/gauges/{id}` response.

    The stage frame is registered here if nothing has registered it yet, without an
    offset, because this service publishes none. Registering it late would leave the
    thresholds as bare lengths, and a threshold that can be differenced against anything
    is the failure this package exists to prevent. Where the USGS site record is read
    afterwards it adds the offset to the same frame, since both name it the same way.
    """
    flood = payload.get("flood") or {}
    status = payload.get("status") or {}
    datum = _stage_datum(payload)
    if datum not in datums.datums:
        datums.register(
            datum,
            description=f"Stage datum at NWPS gauge {payload.get('lid')}, offset unpublished",
        )
    stage_unit = flood.get("stageUnits") or "ft"
    flow_unit_default = flood.get("flowUnits") or "cfs"

    thresholds = []
    for name in CATEGORY_ORDER:
        category = (flood.get("categories") or {}).get(name) or {}
        stage = _measured(category.get("stage"))
        flow = _measured(category.get("flow"))
        if stage is None and flow is None:
            continue
        thresholds.append(
            Threshold(
                name=name,
                stage=None if stage is None else Q(stage, stage_unit, datum=datum),
                flow=None if flow is None else Q(flow, flow_unit_default),
            )
        )

    def reading(
        block: Mapping[str, Any],
    ) -> tuple[Q | None, datetime | None, Q | None]:
        value = _measured(block.get("primary"))
        moment = block.get("validTime")
        # The secondary value is a flow, and it is published in kcfs here while the flood
        # categories in the same response are in cfs. Both are carried as the service
        # wrote them, so the difference is visible rather than resolved silently.
        flow = _measured(block.get("secondary"))
        flow_unit = block.get("secondaryUnit") or flow_unit_default
        return (
            None
            if value is None
            else Q(value, block.get("primaryUnit") or stage_unit, datum=datum),
            None if not moment else datetime.fromisoformat(str(moment).replace("Z", "+00:00")),
            None if flow is None else Q(flow, flow_unit),
        )

    observed, observed_at, observed_flow = reading(status.get("observed") or {})
    forecast, forecast_at, forecast_flow = reading(status.get("forecast") or {})

    return Gauge(
        lid=str(payload.get("lid") or ""),
        name=str(payload.get("name") or ""),
        usgs_id=str(payload.get("usgsId") or "") or None,
        reach_id=str(payload.get("reachId") or "") or None,
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
        timezone=TIMEZONES.get(str(payload.get("timeZone") or "")),
        stage_datum=datum,
        thresholds=tuple(thresholds),
        observed=observed,
        observed_at=observed_at,
        observed_flow=observed_flow,
        forecast=forecast,
        forecast_at=forecast_at,
        forecast_flow=forecast_flow,
        observed_category=(status.get("observed") or {}).get("floodCategory"),
        forecast_category=(status.get("forecast") or {}).get("floodCategory"),
    )


#: Series the model publishes, in the order of how far ahead they reach. Named as the
#: service names them in the reach record, rather than as the camel case its streamflow
#: response uses for the same things.
MODEL_SERIES = (
    "analysis_assimilation",
    "short_range",
    "medium_range",
    "medium_range_blend",
    "long_range",
)

#: The streamflow response keys the same series in camel case, so one name has two
#: spellings within one service and a caller should have to know neither.
_SERIES_KEY = {
    "analysis_assimilation": "analysisAssimilation",
    "short_range": "shortRange",
    "medium_range": "mediumRange",
    "medium_range_blend": "mediumRangeBlend",
    "long_range": "longRange",
}


@dataclass(frozen=True)
class ModelSeries:
    """A modelled streamflow series, which is not an observation.

    Kept apart from `Gauge` because the difference matters to an answer: a modelled flow
    at a reach with no gauge on it has no measurement behind it at all, and a reader who
    cannot tell the two apart will quote one as the other.
    """

    reach_id: str
    series: str
    reference_time: datetime | None
    values: tuple[tuple[datetime, Q], ...] = ()

    @property
    def is_forecast(self) -> bool:
        """Whether the series looks forward. Assimilation looks back."""
        return self.series != "analysis_assimilation"

    def at(self, index: int = -1) -> Q | None:
        return self.values[index][1] if self.values else None

    @property
    def peak(self) -> tuple[datetime, Q] | None:
        return max(self.values, key=lambda pair: pair[1].magnitude) if self.values else None


def model_series_from(
    reach_id: str, series: str, payload: Mapping[str, Any]
) -> ModelSeries | None:
    """Build a modelled series from a `/reaches/{id}/streamflow` response."""
    block = payload.get(_SERIES_KEY.get(series, series)) or {}
    inner = block.get("series") if isinstance(block, dict) else None
    if not isinstance(inner, dict) or not inner.get("data"):
        return None

    unit = parse_unit(inner.get("units"))
    reference = inner.get("referenceTime")
    values = []
    for point in inner["data"]:
        flow = _measured(point.get("flow"))
        moment = point.get("validTime")
        if flow is None or not moment:
            continue
        values.append(
            (
                datetime.fromisoformat(str(moment).replace("Z", "+00:00")),
                # No datum: a discharge has no vertical reference, and no quality grade,
                # because the service publishes none for model output and inventing one
                # would let a modelled value pass a floor that observations must meet.
                Q(flow, unit),
            )
        )

    return ModelSeries(
        reach_id=str(reach_id),
        series=series,
        reference_time=(
            datetime.fromisoformat(str(reference).replace("Z", "+00:00"))
            if reference
            else None
        ),
        values=tuple(values),
    )


class Forecasts:
    """A client for the forecast service.

    Separate from the USGS client rather than folded into it, because the two share no
    query grammar, no identifier space, and no failure vocabulary, and the only thing a
    common interface would buy is the appearance of symmetry.
    """

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
        self.quota = Quota()

    def get(self, path: str) -> tuple[dict[str, Any], Retrieval]:
        """Fetch one resource, with the record of having fetched it."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        cached = self._cache.get(url)
        if cached is not None:
            return json.loads(cached), Retrieval.of(
                f"nwps/{path.strip('/')}", url, {}, 200, cached, self.quota, from_cache=True
            )

        status, headers, body = self._fetch(
            url, {"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        if status == 404:
            raise GaugeNotFound(f"no forecast gauge at {url}")
        if status >= 400:
            raise ServiceUnavailable(f"the forecast service answered {status} for {url}{_explain(body)}")

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"the forecast service answered {status} with a body that is not JSON"
            ) from exc

        self._cache.set(url, body)
        return parsed, Retrieval.of(
            f"nwps/{path.strip('/')}", url, {}, status, body, Quota.from_headers(headers)
        )

    def reach_streamflow(
        self, reach_id: str, series: str = "short_range"
    ) -> tuple["ModelSeries | None", Retrieval]:
        """National Water Model streamflow for one river reach.

        The model is reached through the same host as the gauge forecasts, and its output
        is a modelled series rather than an observation, which is a distinction the
        payload does not draw and this one does.
        """
        payload, retrieval = self.get(f"reaches/{reach_id}/streamflow?series={series}")
        return model_series_from(reach_id, series, payload), retrieval

    def reach(self, reach_id: str) -> tuple[dict[str, Any], Retrieval]:
        """The reach record, which names the series it publishes."""
        return self.get(f"reaches/{reach_id}")

    def gauge(self, identifier: str) -> tuple[Gauge, Retrieval]:
        """A forecast point by its NWS location id or by its USGS station number."""
        payload, retrieval = self.get(f"gauges/{identifier}")
        return gauge_from(payload), retrieval
