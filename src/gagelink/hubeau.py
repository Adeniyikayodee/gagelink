"""French stations, through Hub'Eau, the national water data platform.

Open, no account, and wider than the UK service: it publishes a station reference with
search, real-time levels and flows, and a daily record going back to 1925 at some stations.
So four tools answer here where two answer for the Environment Agency.

Three things about this service decide whether an answer is right, and the payload states
none of them.

**No value carries a unit.** A level comes back as `912.0` and a flow as `358000.0`, with
nothing beside either saying what they are. They are millimetres and litres per second. A
model reading 358000 for the Rhône and calling it cubic metres per second is out by a
thousand, in the direction of a flood, and nothing in the response would contradict it. The
USGS hazard is four spellings of one quantity; this is worse, because there is no spelling
to read at all and the magnitudes are plausible in the wrong unit.

**The datum is a code, not a name.** A station publishes `altitude_ref_alti_station`, the
height of its zero, and `code_systeme_alti_site`, an integer naming which vertical system
that height is on. The integer means nothing without the Sandre nomenclature, which is
published elsewhere, so the altitude arrives without a readable frame. The table is
recorded below rather than fetched, because a datum vocabulary that changes would be a
change worth noticing rather than absorbing.

**A reading is on the station's own zero.** Every observation carries
`code_systeme_alti_serie` 31, which is `Système local - hauteur relative`. That is gage
datum in French: the level is a relative height, the station's altitude is the offset onto
a national system, and the two are both lengths and are not the same measurement. Where the
station publishes no altitude, and many do not, the offset does not exist and the level
cannot be put on a national datum at all.

Record quality is published, unlike at the Environment Agency, but as two separate code
vocabularies that have to be read together.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from quantity_guard import Q
from quantity_guard.registry import datums

from .normalise import Location, Reading
from .service import USER_AGENT, Quota, Retrieval, _trust_store

BASE_URL = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"

#: The prefix a French identifier carries, matching `USGS-` and `EA-`.
PREFIX = "FR"

#: What the manifest calls these retrievals.
STATIONS_COLLECTION = "hubeau-stations"
OBSERVATIONS_COLLECTION = "hubeau-observations"
DAILY_COLLECTION = "hubeau-daily"

#: The units the service does not state, by the quantity code it does state. Recorded from
#: the Hub'Eau documentation, since no payload carries them. `H` is a height above the
#: station's own zero and `Q` is a discharge.
#:
#: These are the two that matter and the two the service publishes in real time. Anything
#: else is refused rather than guessed, because guessing a unit is the error this package
#: exists to prevent and an unfamiliar quantity code is exactly where it would happen.
UNITS: dict[str, str] = {
    "H": "millimeter",
    "Q": "liter/second",
    # The elaborated series, in the same units as their real-time counterparts. Each was
    # confirmed against a live value rather than taken from the code alone: QmM answered
    # 464615 for the Rhone in January 2024, which is litres per second, and HIXM answered
    # 2840 at Austerlitz, which is millimetres.
    "QmnJ": "liter/second",
    "QmM": "liter/second",
    "HIXM": "millimeter",
}

#: What each quantity code measures, for a model that has only the code.
QUANTITIES: dict[str, str] = {
    "H": "water level, above the station's own zero",
    "Q": "discharge",
    "QmnJ": "daily mean discharge",
    "QmM": "monthly mean discharge",
    "HIXM": "monthly maximum water level, above the station's own zero",
}

#: The quantities the elaborated endpoint serves over a date range, as against the two the
#: real-time endpoint serves at an instant. There is no daily level series: `HmnJ` reads
#: like one and the service rejects it, so a question about levels over a range is answered
#: by the monthly maximum or not at all.
ELABORATED = ("QmnJ", "QmM", "HIXM")

#: Words a question uses for a quantity this service names with a letter. A model asking
#: for discharge does not know that Hub'Eau calls it Q.
SYNONYMS: dict[str, str] = {
    "LEVEL": "H", "STAGE": "H", "HEIGHT": "H", "WATER LEVEL": "H",
    "FLOW": "Q", "DISCHARGE": "Q",
    "DAILY DISCHARGE": "QmnJ", "DAILY FLOW": "QmnJ", "DAILY MEAN DISCHARGE": "QmnJ",
    "MONTHLY DISCHARGE": "QmM", "MONTHLY FLOW": "QmM", "MONTHLY MEAN DISCHARGE": "QmM",
    "MONTHLY MAXIMUM LEVEL": "HIXM",
}


def quantity_for(text: str) -> str | None:
    """The service's code for a quantity named by code or by word, or None if unknown."""
    raw = str(text).strip()
    if raw in UNITS:
        return raw
    upper = raw.upper()
    for code in UNITS:
        if code.upper() == upper:
            return code
    return SYNONYMS.get(upper)

#: Vertical systems, from Sandre nomenclature 76, recorded on 2026-08-23. The payload gives
#: the integer alone, so without this an altitude has no readable frame. Kept here rather
#: than fetched: a vocabulary that changed under the package would be a thing to notice,
#: and one extra request per session to learn what is already known is a poor trade against
#: an hourly allowance.
ALTIMETRIC_SYSTEMS: dict[str, str] = {
    "0": "unknown vertical system",
    "1": "Bourdeloue 1857",
    "2": "NGF-Lallemand",
    "3": "NGF-IGN69",
    "4": "Nivellement General de la Corse",
    "5": "IGN 1978 (Corsica)",
    "6": "IGN 1958 (Reunion)",
    "7": "IGN 1989 (Reunion)",
    "8": "IGN 1955 (Martinique)",
    "9": "IGN 1987 (Martinique)",
    "10": "IGN 1951 (Guadeloupe)",
    "11": "IGN 1988 (Guadeloupe)",
    "12": "IGN 1988 (Guadeloupe Les Saintes)",
    "13": "IGN 1988 (Guadeloupe Marie-Galante)",
    "14": "IGN 1988 (Guadeloupe Saint-Martin)",
    "15": "IGN 1988 (Guadeloupe Saint-Barthelemy)",
    "16": "IGN 1942 (Guyane)",
    "17": "NG Guyane 1977",
    "18": "IGN 1950 (Mayotte)",
    "19": "Equipe 1979 (Mayotte)",
    "20": "Danger 1950 (St Pierre et Miquelon)",
    "21": "NGNC 1969 (New Caledonia)",
    "22": "IGN 1984 (Wallis and Futuna)",
    "23": "SHOM 1953 (Mayotte)",
    "24": "Tahiti IGN 1966 (Polynesia)",
    "25": "SHOM 1981 (Loyalty Islands)",
    "26": "SHOM 1976 (Loyalty Islands)",
    "27": "SHOM 1970 (Loyalty Islands)",
    "28": "IGN 1962 (Kerguelen)",
    "29": "EPF 1952 (Terre Adelie)",
    "30": "SHOM 1977 (Mozambique Channel Island)",
    "31": "local system, relative height",
    "32": "IGN 1992 (Guadeloupe La Desirade)",
    "33": "IGN 2023 Mayotte",
}

#: The station filters this service actually honours, checked against the live API on
#: 2026-08-23 by comparing a filtered count against the unfiltered 6,468.
#:
#: The check was needed rather than prudent. This endpoint answers 200 and ignores any
#: parameter it does not recognise, including a fabricated one, so a wrong filter name
#: returns the whole national network and reads as a result. `libelle_region`,
#: `libelle_commune`, and `libelle_departement` all look like filters, are documented
#: nowhere as not being filters, and all three returned every station in France. Only the
#: names below narrow anything, and nothing outside this set is ever sent.
SEARCH_FILTERS = frozenset({
    "code_station",
    "code_site",
    "code_departement",
    "code_region",
    "code_commune_station",
    "libelle_cours_eau",
    "libelle_station",
    "bbox",
    "en_service",
    "size",
    "format",
})

#: A department is two or three characters and may carry a letter, as in 2A for Corsica.
#: A region and a commune are digits. Checked because passing a place name where a code is
#: wanted is the mistake this service turns into a national listing.
_CODE_SHAPED = {
    "code_departement": lambda v: 1 <= len(v) <= 3 and v[0].isdigit(),
    "code_region": lambda v: v.isdigit(),
    "code_commune_station": lambda v: v.isdigit() and len(v) == 5,
}



#: The code for a station's own zero. A reading carries this, which is what makes it a
#: relative height rather than an elevation.
LOCAL_SYSTEM = "31"

#: The systems that are not a usable national datum: unknown, and the local one.
NO_NATIONAL_DATUM = frozenset({"0", LOCAL_SYSTEM})

#: Record grade from Sandre nomenclature 505, matched on the code rather than the label.
#: The label is grammatically inflected in the payload, which publishes `Bonne` and
#: `Non qualifiée` where the nomenclature lists `Bon` and `Non qualifié`, so matching text
#: would fail on agreement rather than on meaning.
QUALIFICATION: dict[str, str] = {
    "20": "approved",
    "16": "provisional",
    "12": "unverified",
    "0": "unverified",
}

#: Processing stage from Sandre nomenclature 510, on the same terms. Raw data is
#: provisional however it is later qualified, which is why both are read.
STATUS: dict[str, str] = {
    "16": "approved",
    "12": "provisional",
    "8": "provisional",
    "4": "provisional",
    "0": "unverified",
}

#: Worst-first, so that combining two grades takes the lower. Mirrors the ranking in
#: quantity-guard rather than inventing a second one.
_WORST_FIRST = ["unverified", "provisional", "estimated", "approved"]

#: A fetch returns the status alongside the body. The status is recorded in the manifest,
#: and a client that deduced it from the payload would be putting a guess in the ledger.
Fetch = Callable[[str], tuple[int, str]]

#: The service's own ceiling on a page, which it enforces with a 400.
MAX_PAGE = 20000

#: How many pages one call will follow. A range longer than this is reported as truncated
#: rather than fetched, since a question that needs 200,000 daily values is not a question
#: answered inside a tool call.
MAX_PAGES = 3


class HubeauError(Exception):
    """The service answered with something this client cannot read."""


class StationNotFound(HubeauError):
    """No station answers to that code."""


class NotACode(HubeauError):
    """A place name was given where the service only matches a code."""


class UnknownQuantity(HubeauError):
    """A quantity code with no recorded unit, which is refused rather than guessed."""


def _http(url: str) -> tuple[int, str]:  # pragma: no cover - live service only
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_trust_store()) as response:
            return response.status, str(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The service explains a rejected argument in the body, and that explanation is
        # worth more to the caller than the status alone.
        return exc.code, exc.read().decode(errors="replace")


def _complaint(payload: Mapping[str, Any]) -> str:
    """What the service said was wrong with a request, in one line."""
    errors = payload.get("field_errors")
    if isinstance(errors, list) and errors:
        return "; ".join(
            f"{e.get('field')}: {e.get('message')}" for e in errors if isinstance(e, dict)
        )
    return str(payload.get("message") or payload)


def _refuse_unknown(filters: Mapping[str, Any]) -> None:
    """Stop a filter this client has not verified from reaching the service.

    A guard rather than a formality. The endpoint ignores an unrecognised parameter and
    answers 200 with the whole collection, so a filter added here by name and never checked
    would silently widen every search that used it instead of failing.
    """
    unknown = sorted(set(filters) - SEARCH_FILTERS)
    if unknown:
        raise HubeauError(
            f"{', '.join(unknown)} is not a filter this service honours; it would be "
            f"ignored and the search would return the national network"
        )


def is_french(identifier: str) -> bool:
    """Whether an identifier names a Hub'Eau station."""
    return identifier.strip().upper().startswith(f"{PREFIX}-")


def reference_of(identifier: str) -> str:
    """The station code, which is what the service answers to."""
    text = identifier.strip()
    return text.split("-", 1)[1] if is_french(text) else text


def identifier_of(code: str) -> str:
    return f"{PREFIX}-{code}"


def _ensure_registered(name: str) -> str:
    """Register a vertical system the shared registry does not already hold.

    `NGF-IGN69` is already there, which is the one nearly every mainland station uses. The
    rest, being the overseas systems and the two historical mainland ones, are registered
    on first use with no offset to anything: naming a frame is what lets an altitude be
    labelled with it, and an offset between two national systems is a survey question this
    package will not answer by guessing.
    """
    if name not in datums.datums:
        datums.register(name, description=f"French vertical system, Sandre nomenclature 76: {name}")
    return name


def datum_of(code: Any) -> str | None:
    """The vertical system a code names, or None where it names none.

    None for an unknown or local system, since both mean the altitude is not on a national
    datum and returning the words `unknown vertical system` as a datum would let it be
    differenced against something.
    """
    text = str(code) if code is not None else ""
    if text in NO_NATIONAL_DATUM or text not in ALTIMETRIC_SYSTEMS:
        return None
    return _ensure_registered(ALTIMETRIC_SYSTEMS[text])


def grade_of(qualification: Any, status: Any) -> str:
    """One record grade from the two vocabularies the service publishes.

    The lower of the two is taken. A value marked raw is provisional whatever its
    qualification says, because qualification describes the measurement and status
    describes how far through review it has been, and a reading is only as good as the
    weaker of those. An unrecognised code grades to unverified rather than being dropped,
    which is the same grade-down-and-warn default the USGS qualifiers get.
    """
    grades = [
        QUALIFICATION.get(str(qualification), "unverified"),
        STATUS.get(str(status), "unverified"),
    ]
    return min(grades, key=_WORST_FIRST.index)


def unit_of(quantity: str) -> str:
    """The unit for a quantity code, or a refusal naming what is unmapped."""
    if quantity not in UNITS:
        raise UnknownQuantity(
            f"no recorded unit for Hub'Eau quantity {quantity!r}; this service publishes "
            f"no units in its payloads, so the value cannot be read. Known codes are "
            f"{', '.join(sorted(UNITS))}."
        )
    return UNITS[quantity]


def location_from(row: Mapping[str, Any]) -> Location:
    """A station row as the location type the rest of the package carries.

    The altitude is attached only where the system it is on is a national one. Where the
    code says unknown or local, the number is real and its frame is not, and a height with
    an unusable frame is what this package refuses to carry.
    """
    code = str(row.get("code_station") or "")
    system = datum_of(row.get("code_systeme_alti_site"))
    altitude = row.get("altitude_ref_alti_station")
    return Location(
        id=identifier_of(code),
        number=code,
        name=str(row.get("libelle_station") or code),
        latitude=row.get("latitude_station"),
        longitude=row.get("longitude_station"),
        altitude=None if altitude is None or system is None else Q(
            float(altitude), "meter", datum=system
        ),
        vertical_datum=system if altitude is not None else None,
        timezone="UTC",
        agency=PREFIX,
        site_type=str(row.get("libelle_cours_eau")) if row.get("libelle_cours_eau") else None,
        state=str(row.get("libelle_region")) if row.get("libelle_region") else None,
        hydrologic_unit_code=str(row.get("code_departement")) if row.get("code_departement") else None,
    )


def reading_from(row: Mapping[str, Any], identifier: str, datum: str | None) -> Reading:
    """One observation, given the unit and the frame the payload leaves out."""
    quantity = str(row.get("grandeur_hydro") or row.get("grandeur_hydro_elab") or "")
    unit = unit_of(quantity)
    raw = row.get("resultat_obs")
    if raw is None:
        raw = row.get("resultat_obs_elab")
    grade = grade_of(
        row.get("code_qualification_obs", row.get("code_qualification")),
        row.get("code_statut"),
    )
    stamp = row.get("date_obs") or row.get("date_obs_elab")
    observed = _moment(str(stamp)) if stamp else None
    # A level is a relative height on the station's own zero; a discharge is measured from
    # nothing and carries no datum at all.
    on = datum if quantity.startswith("H") else None
    return Reading(
        location_id=identifier,
        parameter_code=quantity,
        value=None if raw is None else Q(float(raw), unit, datum=on, quality=grade),
        observed_at=observed,
        approval=grade,
        unit_published=None,
    )


def _moment(text: str) -> datetime:
    """A service timestamp, which is UTC whether or not it says so.

    The real-time endpoint stamps with a trailing Z and the daily one publishes a bare
    date. Reading the second as naive would leave a reading with no frame for its time,
    which is the same class of omission as a value with no unit.
    """
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Hydrometry:
    """The Hub'Eau hydrometry API, returning what a session records."""

    def __init__(self, fetch: Fetch | None = None, base_url: str = BASE_URL) -> None:
        self._fetch: Fetch = fetch or _http
        self.base_url = base_url

    def _get(
        self, path: str, collection: str, url: str | None = None, **params: Any
    ) -> tuple[dict[str, Any], Retrieval]:
        query = {k: v for k, v in params.items() if v is not None}
        if url is None:
            url = f"{self.base_url}/{path}?{urllib.parse.urlencode(query)}"
        status, body = self._fetch(url)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HubeauError(f"Hub'Eau answered {path} with something that is not JSON") from exc
        if status >= 400:
            raise HubeauError(f"Hub'Eau refused {path}: {_complaint(payload)}")
        return payload, Retrieval.of(
            collection=collection,
            url=url,
            params=query,
            status=status,
            body=body,
            quota=Quota(),
        )

    def stations(
        self,
        river: str | None = None,
        commune: str | None = None,
        department: str | None = None,
        region: str | None = None,
        station_name: str | None = None,
        bbox: str | None = None,
        limit: int = 10,
        in_service: bool = True,
    ) -> tuple[list[Location], Retrieval]:
        """Stations matching a filter, using only the filters the service honours.

        The commune and region arguments are codes rather than names: this endpoint
        matches `code_commune_station` and `code_region` and ignores their `libelle_`
        counterparts silently, so a name here would return the national network.
        """
        filters: dict[str, Any] = {
            "libelle_cours_eau": river,
            "code_commune_station": commune,
            "code_departement": department,
            "code_region": region,
            "libelle_station": station_name,
            "bbox": bbox,
        }
        for name, value in filters.items():
            shape = _CODE_SHAPED.get(name)
            if value is not None and shape is not None and not shape(str(value).strip()):
                raise NotACode(
                    f"{name} matches a code and {value!r} is not one; this endpoint "
                    f"ignores an unmatched filter and answers with the whole network"
                )
        _refuse_unknown(filters)

        payload, retrieval = self._get(
            "referentiel/stations",
            STATIONS_COLLECTION,
            en_service="true" if in_service else None,
            size=min(limit, MAX_PAGE),
            format="json",
            **filters,
        )
        return [location_from(r) for r in payload.get("data") or []], retrieval

    def station(self, code: str) -> tuple[Location, Retrieval]:
        payload, retrieval = self._get(
            "referentiel/stations", STATIONS_COLLECTION, code_station=code, format="json"
        )
        rows = payload.get("data") or []
        if not rows:
            raise StationNotFound(f"no Hub'Eau station with the code {code!r}")
        return location_from(rows[0]), retrieval

    def observations(
        self, code: str, quantities: Iterable[str] = ("H", "Q"), datum: str | None = None
    ) -> tuple[list[Reading], list[Retrieval]]:
        """The latest value for each quantity, which is one request per quantity.

        The endpoint filters on one `grandeur_hydro` at a time, so a station measuring both
        a level and a flow costs two requests. Stated because it doubles what a question
        spends and there is no batching parameter to use instead.
        """
        identifier = identifier_of(code)
        readings: list[Reading] = []
        retrievals: list[Retrieval] = []
        for quantity in quantities:
            payload, retrieval = self._get(
                "observations_tr",
                OBSERVATIONS_COLLECTION,
                code_entite=code,
                grandeur_hydro=quantity,
                size=1,
                sort="desc",
            )
            retrievals.append(retrieval)
            for row in payload.get("data") or []:
                readings.append(reading_from(row, identifier, datum))
        return readings, retrievals

    def elaborated(
        self, code: str, start: str, end: str, quantity: str = "QmnJ", datum: str | None = None
    ) -> tuple[list[Reading], list[Retrieval], int]:
        """An elaborated series over a range, following the service's own paging.

        Returns what was read, what it cost, and how many rows the service holds beyond
        what was fetched. That last number is the point: the endpoint caps a page at 20,000
        and hands back a `next` link, and a client that took the first page and said
        nothing would summarise a third of a record as though it were the whole of it. A
        31-year request answered 5,000 days ending in 2003 before this was fixed.
        """
        if quantity not in ELABORATED:
            raise UnknownQuantity(
                f"Hub'Eau elaborates no {quantity!r} series over a date range; it serves "
                f"{', '.join(ELABORATED)}. A level is real-time only, so a range of levels "
                f"has to be asked for as HIXM, the monthly maximum."
            )

        identifier = identifier_of(code)
        readings: list[Reading] = []
        retrievals: list[Retrieval] = []
        url: str | None = None
        held = 0
        for _ in range(MAX_PAGES):
            payload, retrieval = self._get(
                "obs_elab",
                DAILY_COLLECTION,
                url=url,
                code_entite=code,
                grandeur_hydro_elab=quantity,
                date_debut_obs_elab=start,
                date_fin_obs_elab=end,
                size=MAX_PAGE,
            )
            retrievals.append(retrieval)
            held = int(payload.get("count") or 0)
            readings.extend(
                reading_from(row, identifier, datum) for row in payload.get("data") or []
            )
            url = payload.get("next")
            if not url:
                break
        return readings, retrievals, max(0, held - len(readings))


__all__ = [
    "ALTIMETRIC_SYSTEMS",
    "BASE_URL",
    "Hydrometry",
    "PREFIX",
    "QUANTITIES",
    "StationNotFound",
    "UNITS",
    "UnknownQuantity",
    "datum_of",
    "grade_of",
    "identifier_of",
    "is_french",
    "location_from",
    "reading_from",
    "reference_of",
    "unit_of",
]
