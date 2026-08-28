"""UK stations, through the Environment Agency flood-monitoring service.

The client is not written here. `quantity_guard.packs.ea` already reads this service and
already knows the thing that matters about it, which is that a level is published either
as `mASD`, metres above the station's own datum, or as `mAOD`, metres above Ordnance Datum
Newlyn, and that differencing one against the other is dimensionally valid and physically
wrong. That is the same hazard as gage datum against NAVD88 in a different vocabulary, and
reimplementing it here would be forking a working answer.

What this module does is adapt: it turns the pack's `Station` and `Reading` into the types
the rest of gagelink already carries, and it routes every request through the session so a
UK retrieval reaches the manifest on the same terms as an American one. Provenance that
covered only one agency would be provenance in name.

Two limits are worth stating rather than discovering.

The pack reads a station and its latest readings, and nothing else. There is no time
series, no peak record, no forecast, and no river network, because the pack offers none of
those and inventing them here would be writing a second client rather than wiring in the
first. Tools that need what is missing say so against the identifier rather than failing as
though the station did not exist.

A station search is the one exception, and it is written here. It was left out for the same
reason at first, and the reason did not survive contact with the coverage: without a search
a UK question needs a station reference the asker does not have, which made the whole
service reachable only by someone already holding one. The exception is narrow on purpose.
This queries the station list and returns names and references, and it reads no measurement
and no datum, so the argument against a second client does not apply to it: nothing here
interprets a value, which is the part the pack exists to get right.

The service publishes no record grade on live data. Neither a measure nor a reading carries
one, and `qualifier` names the measurement position rather than the quality of the record,
so a UK reading arrives ungraded. That is a fact about the source, and it means the
provisional-against-approved distinction the USGS tools rest on has no counterpart here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from quantity_guard.packs import ea as pack

from .normalise import Location, Reading
from .service import USER_AGENT, Quota, Retrieval, ServiceUnavailable, _trust_store

#: The prefix an Environment Agency identifier carries, matching the `USGS-` one so that a
#: model reads the agency off the identifier rather than being told which tool to use.
PREFIX = "EA"

#: What the manifest calls these retrievals. Named for the resource rather than for the
#: agency, so a manifest reads the same way across services.
STATION_COLLECTION = "ea-station"
MEASURES_COLLECTION = "ea-measures"

#: The frame the service stamps its times in. Stated because the USGS tools report a
#: station's local timezone here and the two are not the same kind of fact.
TIMEZONE = "UTC"

Fetch = Callable[[str], tuple[int, str]]


def _http(url: str) -> tuple[int, str]:  # pragma: no cover - live service only
    """Fetch a body and its status, verifying against the same trust store as everything else.

    The pack ships its own fetch and it calls `urlopen` with no SSL context, so it fails
    verification on an interpreter installed without a system trust store, which is the
    default for a python.org build on macOS. The USGS and NOAA clients here already solved
    that with certifi, and a UK request failing where an American one succeeds would be a
    difference with no reason behind it.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_trust_store()) as response:
            return response.status, str(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


class StationNotFound(Exception):
    """No station answers to that reference."""


def is_ea(identifier: str) -> bool:
    """Whether an identifier names an Environment Agency station."""
    return identifier.strip().upper().startswith(f"{PREFIX}-")


def reference_of(identifier: str) -> str:
    """The agency's own reference, which is what the service answers to."""
    return identifier.strip().split("-", 1)[1] if is_ea(identifier) else identifier.strip()


def identifier_of(reference: str) -> str:
    return f"{PREFIX}-{reference}"


@dataclass
class _Recorder:
    """A fetch that keeps what it fetched, so the pack's calls reach the manifest.

    The pack hands its fetch a URL and wants a body back, which is the right shape for a
    library that does not care about provenance. This one does, so the status travels
    alongside the body here and only the body is passed on. Recording a status the client
    assumed rather than the one the server sent would put a guess in the ledger.
    """

    fetch: Fetch
    seen: list[tuple[str, int, str]]

    def __call__(self, url: str) -> str:
        status, body = self.fetch(url)
        self.seen.append((url, status, body))
        return body


def _retrieval(collection: str, url: str, status: int, body: str) -> Retrieval:
    """One recorded request, carrying the status the service actually returned."""
    return Retrieval.of(
        collection=collection,
        url=url,
        params={},
        status=status,
        body=body,
        quota=Quota(),
    )


#: What the station list is asked for and what it filters on. The service takes a free-text
#: `search` over the label as well, which is the filter a place name should reach.
SEARCH_COLLECTION = "ea-stations"

#: A ceiling on one search. The station list runs to several thousand and a question wants
#: the few that match, not the network.
MAX_RESULTS = 50


@dataclass(frozen=True)
class StationSummary:
    """One station as the list describes it, which is not as the station record does.

    Deliberately not a `Location`. The list carries a name, a river and a town, and no
    altitude, no datum and no timezone, so building the type the rest of the package
    carries would mean a location whose gage datum was invented by the constructor. A
    search says which stations exist; `describe_location` says what one is measured from.
    """

    id: str
    name: str
    river: str | None = None
    town: str | None = None
    catchment: str | None = None


def summary_from(item: Mapping[str, Any]) -> StationSummary:
    """One row of the station list."""
    reference = str(item.get("stationReference") or item.get("notation") or "")
    return StationSummary(
        id=identifier_of(reference),
        name=str(item.get("label") or reference),
        river=_text(item.get("riverName")),
        town=_text(item.get("town")),
        catchment=_text(item.get("catchmentName")),
    )


def _text(value: Any) -> str | None:
    """A field the service sometimes publishes as a list of spellings rather than one.

    A station on a river with two names comes back with both, and picking the first is the
    honest reading: they are alternatives for one river, not two rivers.
    """
    if isinstance(value, list):
        value = value[0] if value else None
    text = str(value).strip() if value is not None else ""
    return text or None


def location_from(station: pack.Station) -> Location:
    """The pack's station as the location type the rest of the package carries.

    The datum name is taken from the pack rather than derived here. The pack has already
    registered `GAUGE:<reference>` with `quantity-guard`, and registered the offset onto
    Ordnance Datum where the service publishes one, so a name generated independently
    would label a level with a frame nothing can convert.
    """
    return Location(
        id=identifier_of(station.reference),
        number=station.reference,
        name=station.name,
        latitude=station.latitude,
        longitude=station.longitude,
        altitude=station.datum_offset,
        vertical_datum="ODN" if station.datum_offset is not None else None,
        timezone=TIMEZONE,
        agency=PREFIX,
        datum_name=station.datum_name,
    )


def reading_from(entry: pack.Reading, identifier: str) -> Reading:
    """One EA reading as the reading type the rest of the package carries.

    The measure name is used where a USGS reading carries a parameter code, because that
    is what this agency publishes in that position: there is no numeric vocabulary to map
    onto, and inventing one would be inventing a correspondence between two agencies'
    measurements that nobody has established.

    The qualifier travels as a qualifier. It names where on the structure the measurement
    is taken, as in `Stage` or `Downstream Stage`, and two measures at one station can
    differ by nothing else, so dropping it would merge two different things.
    """
    return Reading(
        location_id=identifier,
        parameter_code=entry.measure or "measurement",
        value=entry.value,
        observed_at=entry.observed_at,
        approval=entry.value.quality,
        qualifiers=(entry.qualifier,) if entry.qualifier else (),
        unit_published=None,
    )


class EnvironmentAgency:
    """The pack, bound to a fetch and returning what a session records.

    Thin on purpose. Everything about the service is in `quantity_guard.packs.ea`; what is
    here is the adaptation and the provenance, which are this package's concerns and not
    that one's.
    """

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch: Fetch = fetch or _http

    def station(self, reference: str) -> tuple[Location, list[Retrieval]]:
        recorder = _Recorder(self._fetch, [])
        try:
            record = pack.station(reference, fetch=recorder)
        except (KeyError, IndexError, ValueError) as exc:
            # The service answers a missing reference with a body the pack cannot read
            # rather than with a 404, so an unknown station arrives as a parse failure.
            # Named here so it reaches the caller as a missing station.
            raise StationNotFound(f"no EA station with the reference {reference!r}") from exc
        return location_from(record), [
            _retrieval(STATION_COLLECTION, url, status, body)
            for url, status, body in recorder.seen
        ]

    def search(
        self,
        *,
        river: str | None = None,
        town: str | None = None,
        text: str | None = None,
        limit: int = 10,
    ) -> tuple[list[StationSummary], list[Retrieval]]:
        """Stations matching a river, a town, or a name, with the record of asking.

        Every filter the service offers is matched in full and case-sensitively, which is
        why `text` exists: `riverName` wants `River Thames` and will not find `Thames`,
        and a question rarely arrives in the agency's spelling. The free-text filter reads
        the station label, so a place name reaches it.

        A search with no filter is refused rather than sent. The list is the whole network
        and returning the first ten of it would read as a result.
        """
        filters = {"riverName": river, "town": town, "search": text}
        given = {k: v for k, v in filters.items() if v}
        if not given:
            raise ValueError("a station search needs a river, a town, or a name to match")

        query = urllib.parse.urlencode({**given, "_limit": max(1, min(limit, MAX_RESULTS))})
        url = f"{pack.BASE}/id/stations?{query}"
        status, body = self._fetch(url)
        if status >= 400:
            raise ServiceUnavailable(
                f"the Environment Agency station list answered {status}"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                "the Environment Agency station list answered with a body that is not JSON"
            ) from exc

        items = payload.get("items") or []
        # One match comes back as an object rather than as a list of one, which read
        # without this is a station's fields taken for a list of stations.
        items = items if isinstance(items, list) else [items]
        found = [summary_from(i) for i in items if isinstance(i, Mapping)]
        return found, [_retrieval(SEARCH_COLLECTION, url, status, body)]

    def readings(
        self, reference: str, datum_name: str | None = None
    ) -> tuple[list[Reading], list[Retrieval]]:
        recorder = _Recorder(self._fetch, [])
        entries = pack.readings(reference, fetch=recorder, datum_name=datum_name)
        identifier = identifier_of(reference)
        return [reading_from(e, identifier) for e in entries], [
            _retrieval(MEASURES_COLLECTION, url, status, body)
            for url, status, body in recorder.seen
        ]


def now() -> datetime:
    """The current moment, in the frame the service stamps its readings in."""
    return datetime.now(timezone.utc)


__all__ = [
    "EnvironmentAgency",
    "PREFIX",
    "StationNotFound",
    "identifier_of",
    "is_ea",
    "location_from",
    "reading_from",
    "reference_of",
]
