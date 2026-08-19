"""Service payloads into typed quantities.

The service publishes a unit on every reading, a vertical datum on every location, an
approval status on every value, and a timezone on every station. This module attaches all
of it, so that what leaves here is a quantity carrying its own reference frame rather than
a float that a later step has to be trusted to remember things about.

Two fields are published without units and are the reason this package exists. `altitude`
and `drainage_area` come back as bare numbers, and the collection schema states no unit for
either, so the conventions are applied here explicitly and named where they are applied.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

import pint
from quantity_guard import Q, datums, ureg
from quantity_guard.registry import normalize_quality, worst_quality

from .service import GagelinkError


class UnknownUnit(GagelinkError):
    """A unit spelling the package has no mapping for.

    Raised rather than guessed, because a discharge read in the wrong unit is the error
    this whole package exists to prevent.
    """


class UnitsNotPublished(GagelinkError):
    """A value the service publishes without stating its unit."""


# Salinity in parts per thousand, which is what USGS parameter 00480 reports. It is given
# its own dimension because `ppt` reads as parts per trillion in most other contexts, and
# the two differ by a factor of 10^9 while being dimensionally identical, which is exactly
# the shape of error nothing downstream would catch.
try:
    ureg.define("salinity_ppt = [salinity]")
except (pint.errors.RedefinitionError, pint.errors.DefinitionSyntaxError):  # pragma: no cover
    pass

#: Unit strings as the service writes them, in the spelling pint understands. Matched
#: case-insensitively. The new API writes `ft^3/s` where WaterServices wrote `ft3/s`, and
#: prefixes index units with an underscore, so neither spelling is assumed.
UNITS: dict[str, str] = {
    "ft": "foot",
    "in": "inch",
    "m": "meter",
    "mm": "millimeter",
    "ft^3/s": "foot**3/second",
    "m^3/s": "meter**3/second",
    "ft^3/s/mi^2": "foot**3/second/mile**2",
    "ft/s": "foot/second",
    "mi^2": "mile**2",
    "ac-ft": "acre*foot",
    "mgd": "us_mgd",
    "mgal/d": "us_mgd",
    "degc": "degC",
    "degf": "degF",
    "mg/l": "milligram/liter",
    "ug/l": "microgram/liter",
    "us/cm": "microsiemens/centimeter",
    "us/cm @25c": "microsiemens/centimeter",
    "ph units": "pH_unit",
    "std units": "pH_unit",
    "ppt": "salinity_ppt",
    "%": "percent",
    "_fnu": "FNU",
    "_ntu": "NTU",
    "fnu": "FNU",
    "ntu": "NTU",
    "in/hr": "inch/hour",
    "tons/day": "ton/day",
}

#: Approval status as this service words it. WaterServices used the letters P and A, so
#: the mapping is not the one already in quantity-guard.
APPROVAL: dict[str, str] = {
    "provisional": "provisional",
    "approved": "approved",
    "accepted": "approved",
    "working": "provisional",
}

#: Condition codes, which describe the state of the measurement rather than its review.
#: An ice-affected or equipment-affected reading can be wrong by a wide margin while
#: remaining entirely plausible, so each is graded rather than dropped.
QUALIFIERS: dict[str, str] = {
    "ICE": "unverified",
    "EQUIP": "unverified",
    "BKW": "unverified",
    "FLD": "unverified",
    "MAINT": "unverified",
    "DIS": "unverified",
    "DISCONTINUED": "unverified",  # observed live; the series is no longer maintained
    "RAT": "estimated",
    "SSN": "estimated",
    "DRY": "estimated",
    "ZFL": "estimated",
    "EST": "estimated",
    "ESTIMATED": "estimated",  # observed live on revised daily record
    # Gage height not measured on the station's current datum, observed live on peak
    # record. It is the datum hazard arriving as a qualifier rather than as a frame, and
    # nothing downstream would otherwise see it, so it grades down.
    "DIFFDATUM": "unverified",
}

#: Qualifiers that describe the record without bearing on how far it can be trusted. A
#: revised value is better than an unrevised one, so grading it down would penalise the
#: agency for having corrected it.
NEUTRAL_QUALIFIERS = frozenset({"REVISED", "PROVISIONAL", "APPROVED"})

#: Parameters whose value is an elevation, and the frame each is measured against. Gage
#: height is on the station's own datum and is resolved per station; the rest name a
#: national datum directly.
PARAMETER_DATUM: dict[str, str] = {
    "00065": "GAGE",       # gage height, above the local station datum
    "63160": "NAVD88",     # stream water level elevation above NAVD88
    "62614": "NGVD29",     # lake or reservoir elevation above NGVD29
    "62615": "NAVD88",     # lake or reservoir elevation above NAVD88
}

#: Depth below a surface, measured downward. Deliberately carries no datum, because a
#: sign-inverted reference is not the same kind of object as an elevation and treating it
#: as one would let a depth be differenced against a stage.
DOWNWARD_PARAMETERS = frozenset({"72019", "72020", "61055"})

#: Statistic codes, of which four cover nearly all published record. A daily mean and a
#: daily maximum are different quantities and the code is the only thing distinguishing
#: them in a payload that otherwise looks identical.
STATISTICS: dict[str, str] = {
    "00001": "maximum",
    "00002": "minimum",
    "00003": "mean",
    "00006": "sum",
    "00008": "median",
    "00011": "instantaneous",
}

#: Neither of these is published with a unit, and the collection schema states none, so
#: the USGS convention is applied here rather than assumed silently somewhere later.
ALTITUDE_UNIT = "foot"
DRAINAGE_AREA_UNIT = "mile**2"

#: Station timezone from the abbreviation and the daylight saving flag. A zone that does
#: not observe daylight saving is given as a fixed offset, because it is one, and the
#: POSIX sign convention in these names is inverted: Etc/GMT+6 is UTC minus six hours.
#: The pair matters rather than the abbreviation alone, since MST without daylight saving
#: is Arizona and MST with it is Colorado, which differ by an hour for eight months a year.
TIMEZONES: dict[tuple[str, bool], str] = {
    ("EST", True): "America/New_York",
    ("EST", False): "Etc/GMT+5",
    ("CST", True): "America/Chicago",
    ("CST", False): "Etc/GMT+6",
    ("MST", True): "America/Denver",
    ("MST", False): "Etc/GMT+7",
    ("PST", True): "America/Los_Angeles",
    ("PST", False): "Etc/GMT+8",
    ("AKST", True): "America/Anchorage",
    ("AKST", False): "Etc/GMT+9",
    ("HST", True): "Pacific/Honolulu",
    ("HST", False): "Pacific/Honolulu",
    ("AST", True): "America/Halifax",
    ("AST", False): "Etc/GMT+4",
    ("ChST", True): "Pacific/Guam",
    ("ChST", False): "Pacific/Guam",
    ("SST", True): "Pacific/Pago_Pago",
    ("SST", False): "Pacific/Pago_Pago",
}


def parse_unit(published: str | None) -> str:
    """The pint spelling for a unit as the service wrote it."""
    if not published:
        raise UnitsNotPublished("the service returned no unit for this value")
    key = published.strip().lower()
    if key in UNITS:
        return UNITS[key]
    # The service writes exponents with a caret, which pint reads as exclusive-or, and
    # prefixes index units with an underscore. Both are mechanical and are tried before
    # giving up, so that a unit added upstream does not need a release here.
    candidate = key.lstrip("_").replace("^", "**")
    try:
        ureg.parse_units(candidate)
    except Exception:
        raise UnknownUnit(
            f"no mapping for the unit {published!r}; add it to gagelink.normalise.UNITS. "
            f"It is refused rather than guessed, because a value read in the wrong unit "
            f"is the error this package exists to prevent."
        ) from None
    # Parseable but unmapped, which is not the same as understood. `ppt` is the standing
    # example: pint reads it one way and USGS means another, and the two differ by 10^9.
    # The reading is allowed through and the assumption is made audible.
    warnings.warn(
        f"the unit {published!r} has no entry in gagelink.normalise.UNITS and was read as "
        f"{candidate!r} by pint; confirm that is what the service means by it",
        stacklevel=3,
    )
    return candidate


def grade(approval: str | None, qualifiers: list[str] | None) -> str | None:
    """The weakest grade implied by a reading's approval status and its condition codes.

    Approved record of an ice-affected measurement is not approved-quality data, so the
    two are combined by taking the worse rather than by preferring the review status.
    """
    grades: list[str | None] = []
    if approval:
        mapped = APPROVAL.get(approval.strip().lower())
        if mapped is None:
            warnings.warn(
                f"unmapped approval status {approval!r}, graded unverified", stacklevel=2
            )
            mapped = "unverified"
        grades.append(mapped)
    for code in qualifiers or []:
        key = str(code).strip().upper()
        if key in NEUTRAL_QUALIFIERS:
            continue
        if key in QUALIFIERS:
            grades.append(QUALIFIERS[key])
        else:
            # An unrecognised condition code is not evidence of good record, so it grades
            # down rather than being ignored.
            warnings.warn(f"unmapped qualifier {code!r}, graded unverified", stacklevel=2)
            grades.append("unverified")
    return worst_quality(*(normalize_quality(g) for g in grades)) if grades else None


def timezone_of(abbreviation: str | None, uses_daylight_savings: str | None) -> str | None:
    """IANA zone for a station, or None where the pair is not one this package knows."""
    if not abbreviation:
        return None
    observes = str(uses_daylight_savings or "").strip().upper() == "Y"
    return TIMEZONES.get((abbreviation.strip(), observes))


@dataclass(frozen=True)
class Location:
    """A monitoring location, with the reference frames its readings depend on."""

    id: str
    number: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    altitude: Q | None = None
    vertical_datum: str | None = None
    drainage_area: Q | None = None
    timezone: str | None = None
    agency: str | None = None
    site_type: str | None = None
    hydrologic_unit_code: str | None = None
    state: str | None = None

    @property
    def gage_datum(self) -> str:
        """The name of this station's own datum, which stage is measured from."""
        return f"GAGE:{self.number}"

    def register(self) -> str:
        """Register the station's local datum, and its offset where one is publishable.

        The datum is registered whether or not the altitude is known, so that a stage is
        always labelled with the frame it was measured from. Where the altitude and its
        vertical datum are both published, the offset is registered too and a stage can
        be shifted onto that datum. Where either is missing, the frame stands without an
        offset and `to_datum` refuses, which is the honest outcome: guessing what an
        altitude is measured from is the same class of error as guessing the offset.
        """
        name = self.gage_datum
        datums.register(name, description=f"Local datum for {self.id}, {self.name}")
        if self.altitude is not None and self.vertical_datum:
            datums.register_offset(
                name, self.vertical_datum, self.altitude.to("meter").magnitude
            )
        return name


@dataclass(frozen=True)
class Reading:
    """One value, with everything needed to know what it is.

    `value` is None when the service published no measurement, which it marks with a
    qualifier rather than with a sentinel. A missing value is not a measurement and is
    never filled in.
    """

    location_id: str
    parameter_code: str
    value: Q | None
    observed_at: datetime | None
    observed_on: date | None = None
    approval: str | None = None
    qualifiers: tuple[str, ...] = ()
    unit_published: str | None = None
    value_published: str | None = None
    time_series_id: str | None = None
    statistic_id: str | None = None

    @property
    def is_missing(self) -> bool:
        return self.value is None

    @property
    def statistic(self) -> str | None:
        """What the value is a statistic of, where the service said."""
        return STATISTICS.get(self.statistic_id or "")

    def age(self, now: datetime | None = None) -> timedelta | None:
        """How old the reading is, or None if it carries no time at all.

        A daily value is aged from the end of the day it describes, which is the earliest
        moment it could have been complete. Aging it from the start would report it as a
        day older than it is.
        """
        moment = self.observed_at
        if moment is None and self.observed_on is not None:
            moment = datetime.combine(
                self.observed_on, datetime.min.time(), tzinfo=timezone.utc
            ) + timedelta(days=1)
        if moment is None:
            return None
        return (now or datetime.now(timezone.utc)) - moment

    def is_stale(self, max_age: timedelta, now: datetime | None = None) -> bool:
        """Whether the reading is older than a caller is willing to accept.

        The service returns the last value it holds for each parameter independently, so
        one response can mix a discharge from this morning with a turbidity from 2019.
        """
        age = self.age(now)
        return age is None or age > max_age


def _time(text: str | None) -> tuple[datetime | None, date | None]:
    """An instant and a date, of which a reading has one or the other.

    Continuous record is stamped with an instant carrying an offset. Daily and peak record
    is stamped with a date alone, which is not an instant and is not converted into one:
    a daily mean describes a day, and giving it a clock time would assert something the
    service did not publish.
    """
    if not text:
        return None, None
    cleaned = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None, None
    if "T" not in cleaned and " " not in cleaned:
        return None, parsed.date()
    # An offset is required rather than assumed. A naive timestamp from a service that
    # publishes offsets everywhere else is a sign of a changed payload, not of local time.
    return (parsed, None) if parsed.tzinfo else (None, None)


def location_from(feature: Mapping[str, Any]) -> Location:
    """Build a location from one `monitoring-locations` feature."""
    props = dict(feature.get("properties") or {})
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None]

    altitude = None
    if props.get("altitude") is not None and props.get("vertical_datum"):
        altitude = Q(
            float(props["altitude"]), ALTITUDE_UNIT, datum=str(props["vertical_datum"])
        )

    area = None
    if props.get("drainage_area") is not None:
        area = Q(float(props["drainage_area"]), DRAINAGE_AREA_UNIT)

    return Location(
        id=str(props.get("id") or ""),
        number=str(props.get("monitoring_location_number") or ""),
        name=str(props.get("monitoring_location_name") or ""),
        latitude=coords[1],
        longitude=coords[0],
        altitude=altitude,
        vertical_datum=props.get("vertical_datum"),
        drainage_area=area,
        timezone=timezone_of(
            props.get("time_zone_abbreviation"), props.get("uses_daylight_savings")
        ),
        agency=props.get("agency_code"),
        site_type=props.get("site_type"),
        hydrologic_unit_code=props.get("hydrologic_unit_code"),
        state=props.get("state_name"),
    )


def reading_from(
    feature: Mapping[str, Any], location: Location | None = None
) -> Reading:
    """Build a reading from one observation feature.

    A location is optional, and supplying it is what allows a gage height to be labelled
    with the station's own datum rather than left as a bare length.
    """
    props = dict(feature.get("properties") or {})
    code = str(props.get("parameter_code") or "")
    qualifiers = tuple(str(q) for q in (props.get("qualifier") or []))
    raw = props.get("value")

    value = None
    if raw is not None:
        datum = PARAMETER_DATUM.get(code)
        if datum == "GAGE":
            datum = location.gage_datum if location is not None else None
        value = Q(
            float(raw),
            parse_unit(props.get("unit_of_measure")),
            datum=datum,
            quality=grade(props.get("approval_status"), list(qualifiers)),
        )

    at, on = _time(props.get("time"))
    return Reading(
        location_id=str(props.get("monitoring_location_id") or ""),
        parameter_code=code,
        value=value,
        observed_at=at,
        observed_on=on,
        approval=props.get("approval_status"),
        qualifiers=qualifiers,
        unit_published=props.get("unit_of_measure"),
        value_published=None if raw is None else str(raw),
        time_series_id=props.get("time_series_id"),
        statistic_id=props.get("statistic_id"),
    )


def readings_from(
    page: Mapping[str, Any], location: Location | None = None
) -> list[Reading]:
    """Every reading in a page, skipping any whose unit has no mapping.

    One unmapped unit should cost its own series rather than the whole station read, so
    the failure is reported as a warning naming the parameter and the rest is returned.
    """
    out = []
    for feature in page.get("features") or []:
        try:
            out.append(reading_from(feature, location))
        except (UnknownUnit, UnitsNotPublished) as exc:
            code = (feature.get("properties") or {}).get("parameter_code")
            warnings.warn(f"skipping parameter {code}: {exc}", stacklevel=2)
    return out
