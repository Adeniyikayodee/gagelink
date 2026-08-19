"""The surface a model calls.

Organised by verb rather than by agency, because a model degrades as its tool list grows
and a list mirroring three APIs is three times longer than one covering three verbs. Which
service answers a call is a detail of this layer, not a choice put to the caller.

Every tool returns a Result rather than raising, so a failure arrives as something the
model can read and act on. Every series is returned as a handle with a summary rather than
as its points, because a year of 15-minute record is 35,000 values and no answer needs them
in a context window.
"""

from __future__ import annotations

import hashlib
import statistics
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Iterable

from quantity_guard import Q

from .normalise import STATISTICS, Reading, readings_from
from .results import DEFAULT_BUDGET_TOKENS, ErrorCode, Result, unit_text
from .nwps import GaugeNotFound
from .service import QuotaExhausted, ServiceUnavailable
from .session import Session

#: How many points of a series are shown alongside its summary. Enough to see a shape,
#: not enough to answer from, which is the intended reading: the summary is the answer and
#: the sample is orientation.
PREVIEW_POINTS = 20

#: Parameters worth naming without a lookup, being the ones nearly every question needs.
#: Anything else resolves through lookup_parameter against the reference collection.
COMMON_PARAMETERS: dict[str, str] = {
    "00060": "discharge",
    "00065": "gage height, above the station's own datum",
    "00010": "water temperature",
    "00095": "specific conductance",
    "00300": "dissolved oxygen",
    "00400": "pH",
    "63160": "water surface elevation, above NAVD88",
    "62614": "lake or reservoir elevation, above NGVD29",
    "72019": "depth to water level, below land surface",
}


def _guarded(method: Callable[..., Result]) -> Callable[..., Result]:
    """Turn a transport failure into a result the model can act on.

    A raised exception ends the turn. A failure carrying a repair keeps the model in the
    conversation, where the quantity-guard benchmark measured it recovering.
    """

    @wraps(method)
    def wrapper(self: "Toolkit", *args: Any, **kwargs: Any) -> Result:
        try:
            result = method(self, *args, **kwargs)
        except QuotaExhausted as exc:
            result = Result.failure(
                ErrorCode.QUOTA_EXHAUSTED,
                str(exc),
                "Wait for the hourly allowance to reset, or configure an API key. Do not "
                "answer from memory in the meantime; report that the data was "
                "unavailable.",
            )
        except ServiceUnavailable as exc:
            result = Result.failure(
                ErrorCode.SERVICE_UNAVAILABLE,
                str(exc),
                "Retry once. If it fails again, report that the service is unavailable "
                "rather than supplying a value from memory.",
            )
        result.quota_remaining = self.session.quota_remaining
        return result

    return wrapper


class Toolkit:
    """Tools bound to one session, so that every call is recorded in its manifest."""

    def __init__(self, session: Session, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> None:
        self.session = session
        self.budget_tokens = budget_tokens

    # Discovery ---------------------------------------------------------------------

    @_guarded
    def find_locations(
        self,
        state: str | None = None,
        county: str | None = None,
        hydrologic_unit_code: str | None = None,
        site_type: str | None = None,
        bbox: str | None = None,
        limit: int = 10,
    ) -> Result:
        """Search for monitoring locations.

        `bbox` is west,south,east,north in decimal degrees. At least one filter is
        required, since an unfiltered search returns the national network.
        """
        filters = {
            "state_name": state,
            "county_name": county,
            "hydrologic_unit_code": hydrologic_unit_code,
            "site_type": site_type,
            "bbox": bbox,
        }
        if not any(filters.values()):
            return Result.failure(
                ErrorCode.INVALID_ARGUMENTS,
                "a search with no filter would return the national network",
                "Supply at least one of state, county, hydrologic_unit_code, site_type, "
                "or bbox as west,south,east,north.",
            )

        page = self.session.items(
            "monitoring-locations", limit=min(limit, 100), **filters
        )
        features = page.get("features") or []
        if not features:
            return Result.failure(
                ErrorCode.NO_DATA,
                "no monitoring location matched those filters",
                "Widen the search, or check the spelling of the state or county, which "
                "are matched in full rather than by prefix.",
            )

        found = []
        for feature in features:
            props = feature.get("properties") or {}
            found.append(
                {
                    "id": props.get("id"),
                    "name": props.get("monitoring_location_name"),
                    "site_type": props.get("site_type"),
                    "state": props.get("state_name"),
                }
            )
        return Result(ok=True, data={"locations": found, "count": len(found)})

    @_guarded
    def describe_location(self, identifier: str) -> Result:
        """Metadata for one location, including the frames its readings depend on."""
        station = self.session.location(identifier)
        if station is None:
            return Result.failure(
                ErrorCode.LOCATION_UNKNOWN,
                f"no monitoring location with the identifier {identifier!r}",
                "Identifiers are of the form USGS-01646500, with the agency prefix. Use "
                "find_locations to search by state, county, or bounding box.",
            )

        described: dict[str, Any] = {
            "id": station.id,
            "name": station.name,
            "site_type": station.site_type,
            "state": station.state,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "hydrologic_unit_code": station.hydrologic_unit_code,
            "drainage_area": None
            if station.drainage_area is None
            else self.session.record(
                "describe_location", "drainage_area", station.drainage_area
            ),
            "timezone": station.timezone,
            "gage_datum": station.gage_datum,
            "altitude_of_gage_datum": None
            if station.altitude is None
            else self.session.record(
                "describe_location", "altitude", station.altitude
            ),
        }
        result = Result(ok=True, data=described)

        if station.altitude is None or not station.vertical_datum:
            result.note(
                f"This location publishes no altitude on a named vertical datum, so a "
                f"stage here carries {station.gage_datum} and cannot be converted onto a "
                f"national datum. Comparing it against an absolute elevation will be "
                f"refused rather than answered."
            )
        else:
            result.note(
                f"A stage here is measured from {station.gage_datum}, whose zero is at "
                f"{station.altitude.magnitude:g} ft on {station.vertical_datum}. Add that "
                f"offset before comparing a stage against an elevation."
            )
        if station.drainage_area is not None:
            result.note(
                "The service publishes drainage area without a unit; it is square miles."
            )
        return result

    # Observations -------------------------------------------------------------------

    @_guarded
    def get_latest(
        self,
        identifier: str,
        parameters: Iterable[str] | None = None,
        max_age_hours: float | None = None,
    ) -> Result:
        """The most recent value the service holds for each parameter.

        Latest and current are not the same thing. The service returns the last value it
        holds for each parameter independently, so one response can carry a discharge from
        this morning beside a turbidity from several years ago. `max_age_hours` drops the
        stale ones and says which were dropped.
        """
        station = self.session.location(identifier)
        if station is None:
            return Result.failure(
                ErrorCode.LOCATION_UNKNOWN,
                f"no monitoring location with the identifier {identifier!r}",
                "Identifiers are of the form USGS-01646500. Use find_locations to search.",
            )

        wanted = [str(p) for p in parameters] if parameters else None
        page = self.session.items(
            "latest-continuous",
            monitoring_location_id=identifier,
            parameter_code=",".join(wanted) if wanted else None,
        )
        readings = readings_from(page, station)
        if not readings:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} publishes no continuous record"
                + (f" for {', '.join(wanted)}" if wanted else ""),
                "Check the parameter code with lookup_parameter, or try get_series "
                "against the daily record, which some locations publish and others do "
                "not.",
            )

        if wanted:
            missing = set(wanted) - {r.parameter_code for r in readings}
            if missing == set(wanted):
                return Result.failure(
                    ErrorCode.PARAMETER_NOT_MEASURED,
                    f"{identifier} does not measure {', '.join(sorted(missing))}",
                    "Call get_latest without a parameter filter to see what this "
                    "location does measure.",
                )

        now = datetime.now(timezone.utc)
        dropped: list[str] = []
        kept: list[Reading] = []
        for reading in readings:
            if max_age_hours is not None and reading.is_stale(
                timedelta(hours=max_age_hours), now
            ):
                dropped.append(reading.parameter_code)
            else:
                kept.append(reading)

        result = Result(
            ok=True,
            data={
                "location": station.id,
                "readings": [self._render_reading(r, now) for r in kept],
            },
        )
        if dropped:
            result.note(
                f"dropped as older than {max_age_hours} hours: "
                f"{', '.join(sorted(dropped))}"
            )
        if any(r.is_missing for r in kept):
            result.note(
                "a reading with a null value is a gap in the record, not a measurement "
                "of zero; the qualifier says why"
            )
        return result

    @_guarded
    def get_series(
        self,
        identifier: str,
        parameter: str,
        start: str,
        end: str,
        resolution: str = "daily",
    ) -> Result:
        """A time range, returned as a handle with a summary rather than as its points.

        Dates are ISO, as in 2026-08-01. Resolution is daily or continuous, where
        continuous is the 15-minute record and is large.
        """
        if resolution not in {"daily", "continuous"}:
            return Result.failure(
                ErrorCode.INVALID_ARGUMENTS,
                f"unknown resolution {resolution!r}",
                "Resolution is either daily or continuous.",
            )

        station = self.session.location(identifier)
        page = self.session.items(
            resolution,
            monitoring_location_id=identifier,
            parameter_code=parameter,
            datetime=f"{start}/{end}",
            limit=10000,
        )
        readings = [r for r in readings_from(page, station) if r.value is not None]
        if not readings:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} published no {resolution} record for parameter "
                f"{parameter} between {start} and {end}",
                "Check the parameter with lookup_parameter, widen the dates, or try the "
                "other resolution. A location publishing continuous record does not "
                "necessarily publish daily record, and the reverse is also true.",
            )

        handle = self._store(identifier, parameter, resolution, start, end, readings)
        return Result(
            ok=True,
            data={
                "handle": handle,
                "summary": self._summarise(readings),
                "preview": self._preview(readings),
            },
        ).note(
            f"{len(readings)} points are held under this handle; the preview is a sample. "
            f"Use slice_series with the handle to narrow or aggregate."
        )

    @_guarded
    def slice_series(
        self,
        handle: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Result:
        """Narrow a stored series and summarise what remains."""
        stored = self.session.series.get(handle)
        if stored is None:
            return Result.failure(
                ErrorCode.UNKNOWN_HANDLE,
                f"no series is held under the handle {handle!r}",
                "Handles come from get_series and last for the session. Call get_series "
                "again to recreate one.",
            )

        readings = stored["readings"]
        if start:
            readings = [r for r in readings if self._stamp(r) >= start]
        if end:
            readings = [r for r in readings if self._stamp(r) <= end]
        if not readings:
            return Result.failure(
                ErrorCode.NO_DATA,
                "no points fall inside that window",
                f"The series runs from {stored['summary']['first']['time']} to "
                f"{stored['summary']['last']['time']}.",
            )

        return Result(
            ok=True,
            data={
                "handle": handle,
                "summary": self._summarise(readings),
                "preview": self._preview(readings),
            },
        )

    @_guarded
    def get_peaks(self, identifier: str, limit: int = 10) -> Result:
        """Annual peak flow record, largest first."""
        page = self.session.items(
            "peaks", monitoring_location_id=identifier, limit=1000
        )
        station = self.session.location(identifier)
        readings = [r for r in readings_from(page, station) if r.value is not None]
        if not readings:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} publishes no annual peak record",
                "Peak record exists for gaged streams with a long enough history; a "
                "recently established location will have none.",
            )

        ranked = sorted(readings, key=lambda r: r.value.magnitude, reverse=True)[:limit]
        return Result(
            ok=True,
            data={
                "location": identifier,
                "peaks": [
                    {
                        "date": self._stamp(r),
                        "value": self.session.record("get_peaks", "peak", r.value),
                        "quality": r.value.quality,
                    }
                    for r in ranked
                ],
                # The count of peaks, which is not the count of years: a water year can
                # carry more than one peak record.
                "peaks_in_record": len(readings),
            },
        )

    # Forecasts ----------------------------------------------------------------------

    @_guarded
    def get_forecast(self, identifier: str) -> Result:
        """Observed stage, forecast stage, and the thresholds that give them meaning.

        A stage is a number until it is set against the stage at which the river floods,
        and the two come from different services, so this is where they meet.
        """
        try:
            gauge = self.session.gauge(identifier)
        except GaugeNotFound:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} has no forecast point",
                "Most gaged streams carry no forecast location, which is a fact about "
                "the river rather than a mistake in the identifier. Observations are "
                "still available through get_latest.",
            )

        thresholds = {
            t.name: {"stage": t.stage, "flow": t.flow} for t in gauge.thresholds
        }
        for t in gauge.thresholds:
            if t.stage is not None:
                self.session.record("get_forecast", f"{t.name}_stage", t.stage)
        for value, field in ((gauge.observed, "observed"), (gauge.forecast, "forecast")):
            if value is not None:
                self.session.record("get_forecast", field, value)

        result = Result(
            ok=True,
            data={
                "gauge": gauge.lid,
                "name": gauge.name,
                "usgs_id": gauge.usgs_id,
                "timezone": gauge.timezone,
                "observed": {
                    "stage": gauge.observed,
                    "time": gauge.observed_at,
                    "category": gauge.observed_category,
                },
                "forecast": {
                    "stage": gauge.forecast,
                    "time": gauge.forecast_at,
                    "category": gauge.forecast_category,
                },
                "thresholds": thresholds,
            },
        )

        margin = gauge.freeboard_to("minor")
        if margin is not None:
            self.session.record_derived(margin, "stage below minor flooding")
            result.data["below_minor_flooding"] = margin

        result.note(
            f"Stages here and the thresholds beside them are all on {gauge.stage_datum}, "
            f"the gage's own datum, so differencing them is well defined. A surveyed "
            f"elevation is not on that datum and comparing one against these directly "
            f"will be refused."
        )
        missing = [t.name for t in gauge.thresholds if t.flow is None]
        if missing:
            result.note(
                f"no flow threshold is published for {', '.join(missing)}; the service "
                f"writes -9999 there, which is a sentinel and not a discharge"
            )
        return result

    # Reference ----------------------------------------------------------------------

    @_guarded
    def lookup_parameter(self, query: str) -> Result:
        """Resolve a parameter code, or find one by name.

        Readings carry a code and no name, so this is how a code becomes meaningful and
        how a description becomes a code that can be requested.
        """
        query = query.strip()
        if query in COMMON_PARAMETERS:
            return Result(
                ok=True,
                data={"parameter_code": query, "name": COMMON_PARAMETERS[query]},
            )

        page = self.session.items("parameter-codes", limit=20, q=query)
        found = [
            {
                "parameter_code": (f.get("properties") or {}).get("parameter_code")
                or (f.get("properties") or {}).get("id"),
                "name": (f.get("properties") or {}).get("parameter_name"),
                "unit": (f.get("properties") or {}).get("unit_of_measure"),
            }
            for f in (page.get("features") or [])
        ]
        if not found:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"no parameter matched {query!r}",
                "Try a single word such as discharge, stage, temperature, or turbidity.",
            )
        return Result(ok=True, data={"parameters": found})

    # Internals ----------------------------------------------------------------------

    def _render_reading(self, reading: Reading, now: datetime) -> dict[str, Any]:
        age = reading.age(now)
        rendered: dict[str, Any] = {
            "parameter_code": reading.parameter_code,
            "parameter": COMMON_PARAMETERS.get(reading.parameter_code),
            "statistic": reading.statistic,
            "value": None
            if reading.value is None
            else self.session.record(
                "get_latest", reading.parameter_code, reading.value
            ),
            "time": reading.observed_at or reading.observed_on,
            "age_hours": None if age is None else round(age.total_seconds() / 3600, 1),
            "qualifiers": list(reading.qualifiers) or None,
        }
        if reading.is_missing:
            # Stated rather than left as an absent key, since a reading rendered without a
            # value looks like a rendering fault and this one is a fact about the record.
            rendered["measurement"] = "missing from the record, not a value of zero"
        return rendered

    @staticmethod
    def _stamp(reading: Reading) -> str:
        moment = reading.observed_at or reading.observed_on
        return moment.isoformat() if moment else ""

    def _store(
        self,
        identifier: str,
        parameter: str,
        resolution: str,
        start: str,
        end: str,
        readings: list[Reading],
    ) -> str:
        """Keep a series under a handle derived from the query that produced it.

        Derived rather than issued in sequence, so that the same request inside a replay
        produces the same handle and a manifest stays comparable across runs.
        """
        key = f"{identifier}|{parameter}|{resolution}|{start}|{end}"
        handle = f"series-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
        self.session.series[handle] = {
            "query": key,
            "readings": readings,
            "summary": self._summarise(readings),
        }
        return handle

    def _summarise(self, readings: list[Reading]) -> dict[str, Any]:
        """The statistics an answer usually needs, so the points do not have to travel."""
        values = [r.value for r in readings if r.value is not None]
        magnitudes = [v.magnitude for v in values]
        unit = unit_text(values[0].units) if values else None
        peak = max(readings, key=lambda r: r.value.magnitude)
        trough = min(readings, key=lambda r: r.value.magnitude)
        grades = sorted({v.quality for v in values if v.quality})

        # Summary statistics are entered as derived, so an answer quoting the mean of a
        # series traces to something rather than reading as invented. The points
        # themselves never reach the answer, so nothing else would put them in the ledger.
        for value in (trough.value, peak.value):
            self.session.record("get_series", "series", value)
        if magnitudes:
            self.session.record_derived(
                Q(statistics.fmean(magnitudes), values[0].units), "series mean"
            )

        return {
            "count": len(readings),
            "unit": unit,
            "statistic": readings[0].statistic,
            "first": {"time": self._stamp(readings[0]), "value": magnitudes[0]},
            "last": {"time": self._stamp(readings[-1]), "value": magnitudes[-1]},
            "minimum": {"time": self._stamp(trough), "value": trough.value.magnitude},
            "maximum": {"time": self._stamp(peak), "value": peak.value.magnitude},
            "mean": round(statistics.fmean(magnitudes), 4),
            "quality": grades or None,
        }

    def _preview(self, readings: list[Reading]) -> list[dict[str, Any]]:
        """An evenly spaced sample, so the shape is visible without the bulk."""
        if len(readings) <= PREVIEW_POINTS:
            chosen = readings
        else:
            step = len(readings) / PREVIEW_POINTS
            chosen = [readings[int(i * step)] for i in range(PREVIEW_POINTS)]
        return [
            {"time": self._stamp(r), "value": r.value.magnitude} for r in chosen
        ]
