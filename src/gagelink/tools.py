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
import inspect
import os
import statistics
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Iterable

from quantity_guard import Q

from . import ea
from .normalise import STATISTICS, Location, Reading, readings_from
from .results import DEFAULT_BUDGET_TOKENS, ErrorCode, Result, unit_text
from .nldi import DIRECTIONS, NotOnTheNetwork
from .nwps import MODEL_SERIES, GaugeNotFound
from .swot import DATUM as SATELLITE_DATUM
from .swot import NoObservations
from .service import QuotaExhausted, ServiceUnavailable, more_pages
from .session import Session

#: Ceiling on the series listed for one location. The service's own default is ten, which
#: is fewer than several stations publish, and it gives no count of what it withheld.
SERIES_PER_LOCATION = 200

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
    "00480": "salinity",
    "63160": "water surface elevation, above NAVD88",
    "63680": "turbidity",
    "99133": "nitrate",
    "62614": "lake or reservoir elevation, above NGVD29",
    "72019": "depth to water level, below land surface",
}


def _parameters(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows from the parameter-codes collection, which keys its code as `id`."""
    return [
        {
            "parameter_code": (f.get("properties") or {}).get("id"),
            "name": (f.get("properties") or {}).get("parameter_name"),
            "unit": (f.get("properties") or {}).get("unit_of_measure"),
            "description": (f.get("properties") or {}).get("parameter_description"),
        }
        for f in (page.get("features") or [])
    ]


def _peaks_note(result: Result, partial: bool, found: int, limit: int) -> Result:
    """State what the peak listing left out, so a cap does not read as coverage."""
    if found > limit:
        result.note(f"{found} peaks are on record and the largest {limit} are listed")
    if partial:
        result.note(
            "the peak record is longer than one page and was cut off, so the largest "
            "listed may not be the largest on record"
        )
    return result


#: Set to re-raise inside a tool instead of returning INTERNAL_ERROR. A catch-all makes a
#: server that survives a fault, and it also makes one that hides a fault from its own test
#: suite. This is the seam between the two, and the tests set it.
RAISE_INTERNAL = "GAGELINK_RAISE"


def _guarded(method: Callable[..., Result]) -> Callable[..., Result]:
    """Turn a failure inside a tool into a result the model can act on.

    A raised exception ends the turn. A failure carrying a repair keeps the model in the
    conversation, where the quantity-guard benchmark measured it recovering.

    Three kinds of failure are told apart, because each has a different repair. An
    exhausted quota is waited out or keyed. An unavailable service is retried. Anything
    else is a fault in this package, most likely a field renamed upstream, and the only
    honest repair is to report the data as unavailable rather than to try again.

    That last clause is why the catch-all is here rather than left to the transport. This
    package is a client of a service in active migration, whose field names are scheduled
    to change under it, and a KeyError on a renamed field is the likeliest failure it will
    ever see. Ending the turn on it would lose the quota count, the manifest entry, and any
    chance the model has of saying what went wrong.

    A wrong argument is not caught. It is the caller's error, Python already describes it
    well, and a library caller should get the TypeError a Python function owes them; the
    server turns it into a typed failure at the boundary where the caller is a model.
    """
    signature = inspect.signature(method)

    @wraps(method)
    def wrapper(self: "Toolkit", *args: Any, **kwargs: Any) -> Result:
        signature.bind(self, *args, **kwargs)  # a TypeError here is the caller's to fix
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
        except Exception as exc:
            if os.environ.get(RAISE_INTERNAL):
                raise
            result = Result.failure(
                ErrorCode.INTERNAL_ERROR,
                f"{method.__name__} failed inside gagelink: {type(exc).__name__}: {exc}",
                "This is a fault in the tool rather than in the request, so repeating the "
                "call will fail the same way. Report the data as unavailable and do not "
                "supply a value from memory. A field renamed by the service is the "
                "likeliest cause, and it is worth reporting at "
                "https://github.com/Adeniyikayodee/gagelink/issues.",
            )
        result.quota_remaining = self.session.quota_remaining
        return result

    return wrapper


#: What each tool needs that the Environment Agency service does not publish. Named per
#: tool rather than shared, because a model told only that a tool is unavailable will try
#: the next one, and a model told what is missing can say so in the answer.
_NOT_AT_EA: dict[str, str] = {
    "find_locations": "a station search",
    "get_series": "a time series over a date range",
    "slice_series": "a time series over a date range",
    "get_peaks": "an annual peak flow record",
    "get_forecast": "river forecasts or flood thresholds",
    "get_model_forecast": "modelled streamflow",
    "navigate_network": "a river network to navigate",
    "get_basin": "contributing basin boundaries",
    "lookup_parameter": "numeric parameter codes",
}


def _uk_refusal(tool: str, identifier: str) -> Result:
    """A UK identifier reaching a tool that only the American services answer.

    Refused by name rather than by falling through to a USGS lookup, which would report
    the station as unknown. It exists; this tool cannot answer about it, and those are
    different facts with different repairs.
    """
    missing = _NOT_AT_EA.get(tool, "this")
    return Result.failure(
        ErrorCode.INVALID_ARGUMENTS,
        f"{identifier} is an Environment Agency station and the flood-monitoring service "
        f"publishes no {missing}",
        "UK stations answer to describe_location and get_latest only. Report what is "
        "unavailable rather than substituting a figure from another source or from "
        "memory.",
    )


def _measured(readings: Iterable[Reading]) -> list[tuple[Reading, Q]]:
    """Readings that hold a value, each paired with it.

    A reading with no value is a gap in the record and not a measurement of anything, so
    every statistic here is over the ones that hold something. Pairing rather than
    filtering is what lets the value be used afterwards without a second check at each
    site, and without the checks that were missing being written as assumptions.
    """
    return [(reading, value) for reading in readings if (value := reading.value) is not None]


def _iso(value: str) -> datetime | None:
    """A date argument parsed, or None if it is not a date.

    `Z` is normalised because `fromisoformat` did not accept it before Python 3.11 and
    this package supports 3.10, and a date written the way the services write it should
    not fail on the interpreter version.
    """
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _bad_dates(**given: str | None) -> Result | None:
    """A failure naming any date argument that is not a date, or None if they all are.

    An unparseable date used to reach the service, which answered with no record for a
    range it could not read, and that arrived as NO_DATA. The repair on NO_DATA sends the
    model to widen the range or check the parameter, and neither is the fix: the data was
    never missing and the date was never read. A wrong argument has to say so.
    """
    named = [(name, value) for name, value in given.items() if value is not None]
    wrong = [f"{name}={value!r}" for name, value in named if _iso(value) is None]
    if wrong:
        return Result.failure(
            ErrorCode.INVALID_ARGUMENTS,
            f"not a date: {', '.join(wrong)}",
            "Dates are ISO 8601, as in 2026-08-01, or 2026-08-01T09:15:00Z where the time "
            "matters. Correct the argument; this is not a range that holds no record.",
        )

    parsed = {name: _iso(value) for name, value in named}
    start, end = parsed.get("start"), parsed.get("end")
    if start is not None and end is not None and start > end:
        return Result.failure(
            ErrorCode.INVALID_ARGUMENTS,
            f"start {given['start']!r} falls after end {given['end']!r}",
            "Put the earlier date first. A reversed range holds nothing, which would "
            "otherwise come back as though the location published no record.",
        )
    return None


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
        if ea.is_ea(identifier):
            return self._describe_ea(identifier)

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
        if ea.is_ea(identifier):
            return self._latest_ea(identifier, parameters, max_age_hours)

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
            # The service pages at ten by default, and a station can carry twice that in
            # separate series, so an unstated default drops parameters from a listing that
            # otherwise reads as complete.
            limit=SERIES_PER_LOCATION,
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
        if more_pages(page):
            result.note(
                f"this location publishes more than {SERIES_PER_LOCATION} series and the "
                f"listing is partial; ask for specific parameter codes to be sure of "
                f"reaching one"
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
        if ea.is_ea(identifier):
            return _uk_refusal("get_series", identifier)

        if resolution not in {"daily", "continuous"}:
            return Result.failure(
                ErrorCode.INVALID_ARGUMENTS,
                f"unknown resolution {resolution!r}",
                "Resolution is either daily or continuous.",
            )
        bad = _bad_dates(start=start, end=end)
        if bad is not None:
            return bad

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
        truncated = more_pages(page)
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
            + (
                " The range holds more than this and was cut off, so narrow the dates or "
                "use the daily resolution."
                if truncated
                else ""
            )
        )

    @_guarded
    def slice_series(
        self,
        handle: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Result:
        """Narrow a stored series and summarise what remains."""
        bad = _bad_dates(start=start, end=end)
        if bad is not None:
            return bad

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
        if ea.is_ea(identifier):
            return _uk_refusal("get_peaks", identifier)

        page = self.session.items(
            "peaks", monitoring_location_id=identifier, limit=1000
        )
        station = self.session.location(identifier)
        measured = _measured(readings_from(page, station))
        if not measured:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} publishes no annual peak record",
                "Peak record exists for gaged streams with a long enough history; a "
                "recently established location will have none.",
            )

        ranked = sorted(measured, key=lambda pair: pair[1].magnitude, reverse=True)[:limit]
        partial = more_pages(page)
        result = Result(
            ok=True,
            data={
                "location": identifier,
                "peaks": [
                    {
                        "date": self._stamp(reading),
                        "value": self.session.record("get_peaks", "peak", value),
                        "quality": value.quality,
                    }
                    for reading, value in ranked
                ],
                # The count of peaks, which is not the count of years: a water year can
                # carry more than one peak record.
                "peaks_in_record": len(measured),
            },
        )
        return _peaks_note(result, partial, len(measured), limit)

    # Forecasts ----------------------------------------------------------------------

    @_guarded
    def get_forecast(self, identifier: str) -> Result:
        """Observed stage, forecast stage, and the thresholds that give them meaning.

        A stage is a number until it is set against the stage at which the river floods,
        and the two come from different services, so this is where they meet.
        """
        if ea.is_ea(identifier):
            return _uk_refusal("get_forecast", identifier)

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
        for value, field in (
            (gauge.observed, "observed"),
            (gauge.forecast, "forecast"),
            (gauge.observed_flow, "observed_flow"),
            (gauge.forecast_flow, "forecast_flow"),
        ):
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
                    "flow": gauge.observed_flow,
                    "time": gauge.observed_at,
                    "category": gauge.observed_category,
                },
                "forecast": {
                    "stage": gauge.forecast,
                    "flow": gauge.forecast_flow,
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
        if gauge.observed_flow is not None and gauge.thresholds:
            published = next(
                (t.flow for t in gauge.thresholds if t.flow is not None), None
            )
            if published is not None and published.units != gauge.observed_flow.units:
                result.note(
                    f"the observed flow is published in "
                    f"{unit_text(gauge.observed_flow.units)} and the thresholds in "
                    f"{unit_text(published.units)}, in the same response; they differ by "
                    f"a factor of "
                    f"{published.to(gauge.observed_flow.units).magnitude / published.magnitude:g}"
                )

        missing = [t.name for t in gauge.thresholds if t.flow is None]
        if missing:
            result.note(
                f"no flow threshold is published for {', '.join(missing)}; the service "
                f"writes -9999 there, which is a sentinel and not a discharge"
            )
        return result

    @_guarded
    def get_model_forecast(
        self, identifier: str, series: str = "short_range"
    ) -> Result:
        """National Water Model streamflow for the reach a station sits on.

        These are modelled values and not measurements. The distinction matters to an
        answer, because the model covers reaches that carry no gauge at all, so a figure
        from it may have nothing observed behind it. Series are analysis_assimilation,
        which looks back, and short_range, medium_range, medium_range_blend, and
        long_range, which look forward. Not every reach publishes every series.
        """
        if ea.is_ea(identifier):
            return _uk_refusal("get_model_forecast", identifier)

        if series not in MODEL_SERIES:
            return Result.failure(
                ErrorCode.INVALID_ARGUMENTS,
                f"unknown series {series!r}",
                f"Use one of {', '.join(MODEL_SERIES)}.",
            )

        try:
            modelled, reach = self.session.model_streamflow(identifier, series)
        except GaugeNotFound:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} has no forecast gauge, so its reach is not resolvable",
                "Pass a National Water Model reach identifier directly if you have one, "
                "or use get_latest for observations at this location.",
            )

        if modelled is None or not modelled.values:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"the model publishes no {series} series for reach {reach or identifier}",
                f"Not every reach publishes every series. Try short_range, or "
                f"analysis_assimilation for recent modelled flow.",
            )

        # The series holds points, so a peak exists; the type allows for one that does
        # not and the answer below reads it, so the absence is handled rather than assumed.
        highest = modelled.peak
        peak_at, peak = highest if highest is not None else (None, None)
        for value, field in ((peak, "model_peak"), (modelled.at(), "model_last")):
            if value is not None:
                self.session.record("get_model_forecast", field, value)

        return Result(
            ok=True,
            data={
                "reach": reach,
                "series": series,
                "looks": "forward" if modelled.is_forecast else "back",
                "issued": modelled.reference_time,
                "points": len(modelled.values),
                "first": {"time": modelled.values[0][0], "flow": modelled.values[0][1]},
                "last": {"time": modelled.values[-1][0], "flow": modelled.at()},
                "peak": {"time": peak_at, "flow": peak},
            },
        ).note(
            "These are modelled flows, not measurements. The model covers reaches with no "
            "gauge on them, so a value here may have nothing observed behind it, and it "
            "carries no record-quality grade because the service publishes none."
        )

    @_guarded
    def get_satellite_passes(
        self, feature_id: str, start: str, end: str
    ) -> Result:
        """Water surface elevation measured from orbit, by the SWOT mission.

        Covers reaches that no gauge stands on, which is most of them. The elevation is
        referenced to the EGM2008 geoid, not to a national datum and not to any gage
        datum, so it cannot be differenced against a stage or a survey without an offset
        that varies with position and that nothing here publishes.

        A reach identifier is a SWORD river reach id, which is not a USGS station number.
        Dates are ISO.
        """
        bad = _bad_dates(start=start, end=end)
        if bad is not None:
            return bad

        try:
            passes = self.session.satellite_passes(feature_id, start, end)
        except NoObservations:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"the mission holds no passes for reach {feature_id}",
                "Reach identifiers come from the SWORD river database and are not USGS "
                "station numbers. Check the identifier, or widen the dates.",
            )

        usable = [p for p in passes if p.is_usable]
        if not usable:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"no usable elevation for reach {feature_id} between {start} and {end}"
                + (f", though {len(passes)} passes were recorded" if passes else ""),
                "The satellite revisits a given river every few days at best, and a pass "
                "can cross without producing a retrieval. Widen the dates.",
            )

        for observation in usable:
            if observation.elevation is not None:
                self.session.record(
                    "get_satellite_passes", "elevation", observation.elevation
                )

        return Result(
            ok=True,
            data={
                "reach": feature_id,
                "passes": len(passes),
                "with_an_elevation": len(usable),
                "datum": SATELLITE_DATUM,
                "observations": [
                    {
                        "time": p.observed_at,
                        "elevation": p.elevation,
                        "uncertainty": p.uncertainty,
                        "width": p.width,
                        "quality": p.quality,
                    }
                    for p in usable
                ],
            },
        ).note(
            f"Elevations here are on {SATELLITE_DATUM}, a geoid, and a stage or a survey "
            f"is not. Differencing them will be refused rather than answered, because the "
            f"offset between the two varies with position and neither service publishes "
            f"it."
        )

    # Network --------------------------------------------------------------------------

    @_guarded
    def navigate_network(
        self,
        identifier: str,
        direction: str = "upstream",
        distance_km: float = 50,
        limit: int = 20,
    ) -> Result:
        """Monitoring locations along the river from a starting point.

        Along the river rather than within a radius, which is the distinction that makes
        the answer useful: a gage two miles away on the next catchment is not upstream of
        anything here. Direction is upstream, upstream_main, downstream, or
        downstream_diversions, where upstream includes tributaries and upstream_main
        follows the main stem alone.
        """
        if ea.is_ea(identifier):
            return _uk_refusal("navigate_network", identifier)

        if direction not in DIRECTIONS:
            return Result.failure(
                ErrorCode.INVALID_ARGUMENTS,
                f"unknown direction {direction!r}",
                f"Use one of {', '.join(sorted(DIRECTIONS))}.",
            )

        try:
            sites = self.session.navigate(identifier, direction, distance_km)
        except NotOnTheNetwork:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} is not indexed to the river network",
                "Wells, tidal gages, and locations off the mapped hydrography are not on "
                "the network. Observations are still available through get_latest.",
            )

        if not sites:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"no monitoring location lies {direction} of {identifier} within "
                f"{distance_km:g} km along the river",
                "Increase distance_km, or try upstream rather than upstream_main, which "
                "follows the main stem alone and skips the tributaries.",
            )

        found = [
            {
                "id": s.identifier,
                "name": s.name,
                "latitude": s.latitude,
                "longitude": s.longitude,
            }
            for s in sites[:limit]
        ]
        result = Result(
            ok=True,
            data={
                "from": identifier,
                "direction": direction,
                "within_km": distance_km,
                "locations": found,
                "count": len(sites),
            },
        )
        if len(sites) > limit:
            result.note(
                f"{len(sites)} locations were found and the nearest {limit} are listed; "
                f"raise limit or reduce distance_km to change that"
            )
        return result

    @_guarded
    def get_basin(self, identifier: str) -> Result:
        """The area draining to a point.

        The polygon is held rather than returned, being a couple of thousand coordinate
        pairs that answer a mapping question and no question an agent asks.
        """
        if ea.is_ea(identifier):
            return _uk_refusal("get_basin", identifier)

        try:
            basin = self.session.basin(identifier)
        except NotOnTheNetwork:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} is not indexed to the river network, so no basin can be "
                f"delineated for it",
                "Locations off the mapped hydrography have no basin. A gaged stream site "
                "usually publishes a drainage area in its site record, which "
                "describe_location returns.",
            )

        if basin is None:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"the network index delineated no basin for {identifier}",
                "This happens at coastal and tidal locations, where there is no "
                "contributing area to trace.",
            )

        self.session.record_derived(basin.area, f"basin area at {identifier}")
        return Result(
            ok=True,
            data={
                "location": identifier,
                "area": basin.area,
                "bounding_box": {
                    "west": round(basin.bbox[0], 4),
                    "south": round(basin.bbox[1], 4),
                    "east": round(basin.bbox[2], 4),
                    "north": round(basin.bbox[3], 4),
                },
                "polygon_vertices": basin.vertices,
            },
        ).note(
            "The area is computed from the delineated polygon rather than published, and "
            "differs from the drainage area in a site record, which is surveyed. Where "
            "both exist the site record is the figure to quote."
        )

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

        # The collection filters on `id` and on an exact `parameter_name`, and ignores
        # anything else silently: `q=discharge` returns the first rows of the collection
        # with a 200, which reads as a result and is not one.
        #
        # Exact names are no better as an interface, because the published name for
        # specific conductance is "Specific cond at 25C" and nothing would guess it. So a
        # description is matched against the common parameters held here, and only a code
        # or an exact name is put to the service.
        if query.isdigit() and len(query) == 5:
            return self._parameter_by_code(query)

        wanted = query.strip().lower()
        matches = [
            {"parameter_code": code, "name": name}
            for code, name in COMMON_PARAMETERS.items()
            if wanted in name.lower() or name.lower() in wanted
        ]
        if matches:
            return Result(ok=True, data={"parameters": matches})

        page = self.session.items("parameter-codes", limit=20, parameter_name=query)
        found = _parameters(page)
        if found:
            return Result(ok=True, data={"parameters": found})

        return Result.failure(
            ErrorCode.NO_DATA,
            f"no parameter matches {query!r}",
            "Names are not searchable at this service, only matched in full, and its own "
            "spellings are not guessable: specific conductance is published as "
            "'Specific cond at 25C'. Look up a five-digit code directly, or ask "
            "get_latest without a parameter filter to see what this location measures "
            "and what each code is.",
        )

    @_guarded
    def _parameter_by_code(self, code: str) -> Result:
        """One parameter, by the code a reading carries."""
        if code in COMMON_PARAMETERS:
            return Result(
                ok=True,
                data={"parameter_code": code, "name": COMMON_PARAMETERS[code]},
            )
        page = self.session.items("parameter-codes", id=code)
        found = _parameters(page)
        if not found:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"no parameter has the code {code!r}",
                "Codes are five digits, as in 00060 for discharge.",
            )
        return Result(ok=True, data=found[0])

    # Internals ----------------------------------------------------------------------

    # The Environment Agency ------------------------------------------------------------

    def _describe_ea(self, identifier: str) -> Result:
        """A UK station, described in the same shape as an American one.

        The fields the flood-monitoring service does not publish are absent rather than
        filled: there is no site type, no catchment code, and no drainage area, and
        writing a blank into those would read as a station that reported none rather than
        as an agency that publishes none.
        """
        station = self.session.ea_location(identifier)
        if station is None:
            return Result.failure(
                ErrorCode.LOCATION_UNKNOWN,
                f"no Environment Agency station with the reference "
                f"{ea.reference_of(identifier)!r}",
                "UK identifiers are of the form EA-2604TH, using the agency's own station "
                "reference. There is no search tool for them; references come from "
                "https://environment.data.gov.uk/flood-monitoring/id/stations.",
            )

        described: dict[str, Any] = {
            "id": station.id,
            "name": station.name,
            "latitude": station.latitude,
            "longitude": station.longitude,
            "timezone": station.timezone,
            "gage_datum": station.gage_datum,
            "altitude_of_gage_datum": None
            if station.altitude is None
            else self.session.record(
                "describe_location", "altitude", station.altitude
            ),
        }
        result = Result(ok=True, data=described)

        if station.altitude is None:
            result.note(
                f"This station publishes no datum offset, so a level here carries "
                f"{station.gage_datum} and cannot be converted onto Ordnance Datum. "
                f"Comparing it against an absolute elevation will be refused rather than "
                f"answered. Most Environment Agency stations publish none."
            )
        else:
            result.note(
                f"A level here measured as mASD is on {station.gage_datum}, whose zero is "
                f"at {station.altitude.magnitude:g} m on Ordnance Datum Newlyn. A level "
                f"measured as mAOD is already on Ordnance Datum. The two are both metres "
                f"and are not interchangeable."
            )
        result.note(
            "This is an Environment Agency station. It answers describe_location and "
            "get_latest; the series, peak, forecast, network, and basin tools cover the "
            "American services only."
        )
        return result

    def _latest_ea(
        self,
        identifier: str,
        parameters: Iterable[str] | None,
        max_age_hours: float | None,
    ) -> Result:
        """The latest reading for each measure at a UK station.

        The staleness handling matters more here than it does upstream, because this
        service states no record grade at all: a station with a failed sensor keeps
        serving that sensor's last good value indefinitely, and nothing in the response
        marks it as old. Age is the only signal there is.
        """
        station = self.session.ea_location(identifier)
        if station is None:
            return Result.failure(
                ErrorCode.LOCATION_UNKNOWN,
                f"no Environment Agency station with the reference "
                f"{ea.reference_of(identifier)!r}",
                "UK identifiers are of the form EA-2604TH, using the agency's own station "
                "reference.",
            )

        readings = self.session.ea_readings(identifier)
        if not readings:
            return Result.failure(
                ErrorCode.NO_DATA,
                f"{identifier} publishes no current measurements",
                "The station record exists but holds no readings. Check the station at "
                "https://environment.data.gov.uk/flood-monitoring/id/stations.",
            )

        wanted = [str(p).lower() for p in parameters] if parameters else None
        if wanted:
            # Matched on the measure name rather than on a code, since this agency
            # publishes no numeric vocabulary. Substring rather than equality, because a
            # question asks for level and the service answers Water Level.
            matched = [
                r for r in readings
                if any(w in r.parameter_code.lower() for w in wanted)
            ]
            if not matched:
                published = sorted({r.parameter_code for r in readings})
                return Result.failure(
                    ErrorCode.PARAMETER_NOT_MEASURED,
                    f"{identifier} does not measure {', '.join(sorted(set(wanted)))}",
                    f"This station measures {', '.join(published)}. Names are the "
                    f"agency's own; there are no numeric parameter codes at this service.",
                )
            readings = matched

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
                f"dropped as older than {max_age_hours:g} hours: {', '.join(dropped)}"
            )
        result.note(
            "The Environment Agency publishes no record grade on live data, so these "
            "readings are neither provisional nor approved and are returned ungraded. "
            "Age is the only staleness signal this service gives."
        )
        return result

    def _render_reading(self, reading: Reading, now: datetime) -> dict[str, Any]:
        age = reading.age(now)
        rendered: dict[str, Any] = {
            "parameter_code": reading.parameter_code,
            # A USGS code is numeric and needs the lookup. An Environment Agency measure
            # is already the name the agency publishes, so it stands as its own.
            "parameter": COMMON_PARAMETERS.get(reading.parameter_code)
            or (None if reading.parameter_code.isdigit() else reading.parameter_code),
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
        measured = _measured(readings)
        if not measured:
            # Every caller filters before getting here, so this is a fault rather than an
            # empty range. It is raised rather than returned because the guard turns it
            # into a failure the model can read, which a wrong summary would not be.
            raise ValueError("a series summary needs at least one reading holding a value")

        values = [value for _, value in measured]
        magnitudes = [value.magnitude for value in values]
        unit = unit_text(values[0].units)
        peak, peak_value = max(measured, key=lambda pair: pair[1].magnitude)
        trough, trough_value = min(measured, key=lambda pair: pair[1].magnitude)
        grades = sorted({v.quality for v in values if v.quality})

        # Summary statistics are entered as derived, so an answer quoting the mean of a
        # series traces to something rather than reading as invented. The points
        # themselves never reach the answer, so nothing else would put them in the ledger.
        for value in (trough_value, peak_value):
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
            "minimum": {"time": self._stamp(trough), "value": trough_value.magnitude},
            "maximum": {"time": self._stamp(peak), "value": peak_value.magnitude},
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
            {"time": self._stamp(reading), "value": value.magnitude}
            for reading, value in _measured(chosen)
        ]
