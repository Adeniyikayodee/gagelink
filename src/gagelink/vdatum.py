"""Vertical datum conversion, from NOAA's VDatum.

This is the one service here that exists to make a comparison well defined rather than to
report a measurement. A gage datum's altitude is published on some national datum, and the
survey or lidar product it is about to be differenced against is published on another. Both
are lengths above a surface and neither says which surface in a way arithmetic can catch,
which is the error this package was written to refuse. VDatum is what turns that refusal
into an answer.

The case is the common one, not a corner. Across 3,397 gaged stream stations sampled in
Colorado, Louisiana, Maryland and Washington, 58% of the altitudes published are on NGVD29
and 42% on NAVD88. A modern survey is on NAVD88. So for the majority of stations, the
offset the service hands back and the elevation somebody wants to compare it against are on
different datums, and the difference between them across the contiguous states runs to feet
rather than inches.

Three families of datum are converted between here, and they answer different questions.
The orthometric pair, NGVD29 and NAVD88, is the station-offset case above. The tidal datums
answer what a level is relative to the tide, which is the frame coastal flood work is stated
in and which no gage publishes. And EGM2008 is the geoid SWOT measures against, which is why
a satellite elevation has until now been comparable to nothing at all.

Six things about this API decide how it is called here, and every one of them was found by
being refused.

A vertical datum cannot be asked for alone. NGVD29 without NAD27 is answered `Source
Horizontal Frame should be NAD27`; EGM2008 without WGS84_G1674 is answered `Source
Horizontal Frame should be WGS84_G1674`, and then `Source Vertical Geoid is not correct!`
until the geoid model is named as well. None of that is derivable from what a station
publishes, so `FRAMES` holds it.

Conversion is reliable onto NAVD88 and not away from it. NAVD88 to NGVD29 was tried at
Little Falls and answered `Uncaught error, please contact NOAA VDatum Program Support team`.

An error arrives as HTTP 200 with an `errorCode` in the body. The status is not the signal
and reading it as one would take a refusal for an answer.

A point outside the coverage of a conversion answers 200, with no error code, and a `t_z` of
-999999 and an empty uncertainty. That is a fill value dressed as an elevation, from the
service whose entire job is to say what an elevation is measured from. It is the fourth
sentinel spelling across the services this package reads, after -9999, -999999999999.0, and
the old USGS one, and like the others it is dimensionally valid and passes every check
downstream. It is refused here, at the boundary.

A geoid conversion returns its uncertainty as the string `NaN`. That is a fifth spelling and
the worst of them, because `float` accepts it, every magnitude comparison against it is
False so a sentinel test lets it through, and adding it in quadrature turns the total into
NaN without raising. `_number` rejects it on finiteness rather than on magnitude.

Coverage is narrower than the region list suggests. `hi` is not a region the API accepts;
`ak` is, and is then refused with `GEOID18 only cover Contiguous, Chesapeak_delaware, West
Coast and PRVI`. Rather than work out a region and a geoid model per territory with no
station to test either against, this module converts within the contiguous states and
refuses elsewhere before a request is spent.
"""

from __future__ import annotations

import json
import math
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

from quantity_guard import Q, datums

from .results import significant
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

BASE_URL = "https://vdatum.noaa.gov/vdatumweb/api/convert"

#: The geoid model the service uses unless told otherwise, and the one every datum here is
#: realised against except the satellite geoid. Its coverage is narrower than the service's
#: region list, which is the reason `UNREACHABLE_STATES` exists below: asking for Alaska is
#: answered `GEOID18 only cover Contiguous, Chesapeak_delaware, West Coast and PRVI`.
DEFAULT_GEOID = "geoid18"


@dataclass(frozen=True)
class Frame:
    """A vertical datum, and the two things the service will not accept it without.

    Both couplings were found by being refused. Asking for NGVD29 without NAD27 is answered
    `Source Horizontal Frame should be NAD27`; asking for EGM2008 without WGS84_G1674 is
    answered `Source Horizontal Frame should be WGS84_G1674`, and then, once that is fixed,
    `Source Vertical Geoid is not correct!` until the geoid model is named too.

    None of this is derivable from the vertical datum a station publishes, which is all a
    caller has. Holding the pairing here means the cost of not knowing it is nothing rather
    than a 412, and it is the same shape of fact as a unit: a number that means nothing
    until you are told the frame it is in.
    """

    horizontal: str
    geoid: str = DEFAULT_GEOID
    #: Whether this datum is defined by the behaviour of the tide rather than by a fixed
    #: surface. It matters for what can be said about the answer, not for how it is asked:
    #: a tidal transformation's uncertainty is frequently larger than the shift it applies.
    tidal: bool = False


#: Every datum this module will convert between, with what each one has to be asked for
#: alongside. Orthometric first, then the satellite geoid, then the tidal datums.
FRAMES: dict[str, Frame] = {
    "NAVD88": Frame("NAD83_2011"),
    "NGVD29": Frame("NAD27"),
    # SWOT publishes water surface elevation on this geoid, which is why it is here: it is
    # the frame a satellite elevation arrives in and cannot be compared out of.
    "EGM2008": Frame("WGS84_G1674", geoid="egm2008"),
    "MLLW": Frame("NAD83_2011", tidal=True),
    "MLW": Frame("NAD83_2011", tidal=True),
    "LMSL": Frame("NAD83_2011", tidal=True),
    "MTL": Frame("NAD83_2011", tidal=True),
    "DTL": Frame("NAD83_2011", tidal=True),
    "MHW": Frame("NAD83_2011", tidal=True),
    "MHHW": Frame("NAD83_2011", tidal=True),
}

#: What a conversion lands on unless another is asked for. Every station offset wants this
#: one; the tidal datums are asked for by name when the question is about the tide.
TARGET = "NAVD88"

#: The one region this module asks in, and the reason it is a constant rather than a
#: parameter. The service divides the world into a handful of coverage areas, and the geoid
#: model every datum here depends on covers only some of them. `hi` is not a region the API
#: accepts at all; `ak` is, and is then refused by the geoid. Rather than work out a region
#: and a geoid model per territory with no station to test either against, this converts
#: within the contiguous states and refuses elsewhere, before a request is spent.
CONTIGUOUS = "contiguous"

#: Where a conversion will not be attempted, by the state name the service publishes. Read
#: from the station record rather than from its coordinate, so this is a lookup on a
#: published fact rather than a guess from geometry.
UNREACHABLE_STATES = frozenset(
    {
        "Alaska",
        "Hawaii",
        "Puerto Rico",
        "Virgin Islands",
        "Guam",
        "American Samoa",
        "Northern Mariana Islands",
    }
)

#: How this service spells the units the quantities here are in. Feet are asked for as US
#: survey feet, which is what a national datum height in the United States is conventionally
#: published in. The difference from the international foot pint measures in is two parts
#: per million, which at the highest station altitude in the sample is a hundredth of a foot
#: against a published accuracy of ten.
UNITS: dict[str, str] = {"foot": "us_ft", "meter": "m"}

#: The largest separation between two of these datums that is physically credible, in
#: metres. Geoid-to-NAVD88 across the contiguous states is within a couple of metres, and a
#: tidal datum sits within a few of the land surface even where the range is greatest, so
#: ten is loose enough to pass every real answer and tight enough to catch what this is for.
#:
#: What it is for is a separation computed from two numbers that do not belong together: a
#: conversion answered for one position or height applied to another. That produces a shift
#: of hundreds of metres, in the right unit, on a quantity carrying the right datum, and
#: every check downstream passes it. It is the same failure as the subtraction this package
#: refuses, arriving from the machinery built to prevent it.
MAX_SEPARATION_METRES = 10.0

#: The fill the service writes when a point falls outside the coverage of a conversion, at
#: HTTP 200 and with no error code. Compared by magnitude rather than exactly, because a
#: fill is not a measurement and nothing is gained by being precise about which one it is.
SENTINEL_MAGNITUDE = 1e5


#: What each datum is, for the registry. A tidal datum is a surface defined by the average
#: of a tidal extreme over a nineteen-year epoch, which is not a thing anybody should be
#: expected to infer from four capital letters.
DESCRIPTIONS: dict[str, str] = {
    "NAVD88": "North American Vertical Datum of 1988",
    "NGVD29": "National Geodetic Vertical Datum of 1929",
    "EGM2008": "Earth Gravitational Model 2008 geoid, which SWOT measures against",
    "MLLW": "Mean lower low water, the average of the lower low tide over a tidal epoch",
    "MLW": "Mean low water",
    "LMSL": "Local mean sea level",
    "MTL": "Mean tide level, midway between mean low and mean high water",
    "DTL": "Diurnal tide level, midway between mean lower low and mean higher high water",
    "MHW": "Mean high water",
    "MHHW": "Mean higher high water, the average of the higher high tide over a tidal epoch",
}


def register_datums() -> None:
    """Make every datum this converts onto nameable by a quantity.

    Called at import, because a conversion produces a quantity carrying its target datum
    and a quantity refuses a datum the registry has not been told about. Several of the
    tidal ones are not registered by quantity-guard, and one that is goes by another name:
    what this service calls LMSL is registered there as MSL. Registering under the name the
    service uses keeps the two spellings from being quietly treated as one datum.
    """
    for name, description in DESCRIPTIONS.items():
        if name not in datums.datums:
            datums.register(name, description=description)


register_datums()


class NoCoverage(GagelinkError):
    """The service holds no conversion for this point.

    Its own answer for this is a number, so this exists to stop that number travelling.
    """


class ConversionRefused(GagelinkError):
    """The service declined the conversion and said why."""


@dataclass(frozen=True)
class Conversion:
    """One elevation moved onto another datum, and how well that move is known."""

    #: The elevation on the target datum, carrying that datum.
    elevation: Q
    #: The uncertainty the service publishes for the transformation. A length rather than
    #: an elevation, so it carries no datum: it is a spread about a value and not a
    #: position above a surface.
    uncertainty: Q | None
    source_datum: str
    target_datum: str

    def combined_with(self, published: Q | None) -> Q | None:
        """The total uncertainty once the station's own published accuracy is included.

        Added in quadrature, which assumes the two are independent. They are: one is how
        well the gage datum's height was determined and the other is how well two national
        surfaces are known to relate, and nothing links them.

        Worth doing because the two terms are usually of very different sizes and the
        larger one decides the answer. At most stations the published accuracy dominates
        by an order of magnitude, so a freeboard quoted with only the conversion's
        uncertainty would claim a precision the station never had.
        """
        if published is None:
            return self.uncertainty
        if self.uncertainty is None:
            return published
        # Combined in the unit the conversion came back in, which is the unit the question
        # arrived in. A station offset is in feet and a satellite elevation is in metres,
        # and neither should have to travel through the other to be added to itself.
        unit = self.uncertainty.units
        here = published.to(unit).magnitude
        there = self.uncertainty.to(unit).magnitude
        # Rounded, because the arithmetic produces more figures than either input had and
        # an uncertainty printed to twelve of them is a precision claim of its own. Ten
        # feet plus a fifth of one is ten feet, and saying 10.001444895613833 invites the
        # figure being read as though somebody knew it that well.
        return Q(significant((here**2 + there**2) ** 0.5), unit)


def _number(raw: Any) -> float | None:
    """A float from the service, or None where it wrote something that is not one.

    Three spellings of "no answer" arrive here and every one of them parses. An empty
    string is the tidal and out-of-coverage case; -999999 is the fill; and `NaN`, which the
    geoid conversions return for their uncertainty, is a float in Python, is not caught by
    a magnitude test because every comparison against it is False, and would travel through
    an addition in quadrature turning the total into NaN without raising anything.
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return None if abs(value) >= SENTINEL_MAGNITUDE else value


def tidal_datums() -> list[str]:
    """The datums here that are defined by the tide, in the order a client should offer them."""
    return [name for name, frame in FRAMES.items() if frame.tidal]


def unreachable(state: str | None) -> bool:
    """Whether a conversion here would be refused for want of coverage.

    Checked before a request rather than after one, because the answer is known from the
    station record and spending a call to be told it is a call not spent on the question.
    """
    return (state or "") in UNREACHABLE_STATES


class VerticalDatums:
    """A client for the VDatum conversion API."""

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

    def url_for(self, latitude: float, longitude: float, elevation: Q, target_datum: str) -> str:
        """The request for one conversion, with every coupling the service enforces filled in.

        The source datum is read off the quantity rather than passed beside it. An elevation
        that does not carry the frame it was measured from is not an elevation this package
        will send anywhere, and taking it as an argument would make it possible to send one
        frame's number under another frame's name.
        """
        source_datum = elevation.datum
        if not source_datum:
            raise ValueError(
                "this elevation carries no datum, so there is nothing to convert it from"
            )
        source, target = _frame(source_datum), _frame(target_datum)

        unit = UNITS.get(str(elevation.units))
        if unit is None:
            known = ", ".join(sorted(UNITS))
            raise ValueError(f"the datum service takes {known}, not {elevation.units}")

        query = urllib.parse.urlencode(
            {
                "region": CONTIGUOUS,
                "s_x": f"{longitude:.6f}",
                "s_y": f"{latitude:.6f}",
                "s_z": f"{elevation.magnitude:g}",
                "s_h_frame": source.horizontal,
                "s_v_frame": source_datum,
                "s_v_unit": unit,
                "s_v_geoid": source.geoid,
                "t_h_frame": target.horizontal,
                "t_v_frame": target_datum,
                "t_v_unit": unit,
                "t_v_geoid": target.geoid,
            }
        )
        return f"{self.base_url}?{query}"

    def convert(
        self, latitude: float, longitude: float, elevation: Q, target_datum: str = TARGET
    ) -> tuple[Conversion, Retrieval]:
        """Move one elevation onto another datum, with the record of having asked."""
        source_datum = elevation.datum or ""
        url = self.url_for(latitude, longitude, elevation, target_datum)
        collection = f"vdatum/{source_datum}/{target_datum}".lower()
        unit = UNITS[str(elevation.units)]

        cached = self._cache.get(url)
        if cached is not None:
            return (
                _conversion(json.loads(cached), source_datum, target_datum, elevation.units),
                Retrieval.of(collection, url, {}, 200, cached, Quota(), from_cache=True),
            )

        status, headers, body = self._fetch(
            url, {"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        if status >= 400:
            raise ServiceUnavailable(
                f"the datum service answered {status} converting {source_datum} to "
                f"{target_datum}{_explain(body)}"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"the datum service answered {status} with a body that is not JSON"
            ) from exc

        conversion = _conversion(payload, source_datum, target_datum, elevation.units)
        self._cache.set(url, body)
        return conversion, Retrieval.of(
            collection, url, {}, status, body, Quota.from_headers(headers)
        )


def _frame(datum: str) -> Frame:
    """What a datum has to be asked for alongside, or a refusal naming what is on offer."""
    frame = FRAMES.get(datum)
    if frame is None:
        known = ", ".join(sorted(FRAMES))
        raise ValueError(f"{datum!r} is not a datum this converts; it knows {known}")
    return frame


def _conversion(
    payload: Mapping[str, Any], source_datum: str, target_datum: str, unit: Any
) -> Conversion:
    """One conversion from a response, or the reason there is not one.

    Both failure modes arrive at HTTP 200, and they are different failures. An `errorCode`
    is the service declining and saying why, which is worth passing on verbatim: its
    messages name the argument that was wrong. A fill in `t_z` is the service answering
    with a number that is not one, which is worth refusing with a message that does not
    quote it, because quoting it is how it escapes.
    """
    if payload.get("errorCode") is not None:
        said = str(payload.get("message") or "").strip() or "no reason given"
        raise ConversionRefused(
            f"the datum service refused to convert {source_datum} to {target_datum}: {said}"
        )

    height = _number(payload.get("t_z"))
    if height is None:
        raise NoCoverage(
            f"the datum service holds no {source_datum} to {target_datum} conversion at "
            f"this position"
        )

    spread = _number(payload.get("uncertainty"))
    return Conversion(
        elevation=Q(height, unit, datum=target_datum),
        uncertainty=None if spread is None else Q(spread, unit),
        source_datum=source_datum,
        target_datum=target_datum,
    )


__all__ = [
    "BASE_URL",
    "CONTIGUOUS",
    "DEFAULT_GEOID",
    "FRAMES",
    "MAX_SEPARATION_METRES",
    "TARGET",
    "UNITS",
    "UNREACHABLE_STATES",
    "Conversion",
    "ConversionRefused",
    "DESCRIPTIONS",
    "Frame",
    "NoCoverage",
    "VerticalDatums",
    "register_datums",
    "tidal_datums",
    "unreachable",
]
