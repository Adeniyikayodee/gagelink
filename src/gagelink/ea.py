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

The pack reads a station and its latest readings, and nothing else. There is no search, no
time series, no peak record, no forecast, and no river network, because the pack offers
none of those and inventing them here would be writing a second client rather than wiring
in the first. Tools that need what is missing say so against the identifier rather than
failing as though the station did not exist.

The service publishes no record grade on live data. Neither a measure nor a reading carries
one, and `qualifier` names the measurement position rather than the quality of the record,
so a UK reading arrives ungraded. That is a fact about the source, and it means the
provisional-against-approved distinction the USGS tools rest on has no counterpart here.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from quantity_guard.packs import ea as pack

from .normalise import Location, Reading
from .service import USER_AGENT, Quota, Retrieval, _trust_store

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
