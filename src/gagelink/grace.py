"""GRACE and GRACE-FO gravimetry, through NASA's common metadata repository.

The gravity missions measure change in total water storage, which is the only observation
of groundwater at a scale a catchment cares about, and the only one that includes the water
nobody can see. It is what answers whether a drought is depleting storage rather than only
lowering a river.

The access shape here is neither synchronous retrieval nor a queue, and it is worth saying
which, because the plan reserved a queue for it and that turned out to be the wrong guess.
Discovery is open: collections and granules can be searched, and their coverage and file
locations read, with no account at all. Retrieval is not: the data file itself answers 401
without Earthdata credentials, while the checksum beside it answers 206.

So search is free and download is credentialed, and this module keeps that boundary
visible rather than failing at whichever step the caller reaches first.

The files are NetCDF on a global grid. This package does not read them, and says so rather
than pretending to: a reader for gridded arrays is a dependency and a design this has not
taken on. What it gives back is the granule, its coverage, and the path it was saved to.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .jobs import CredentialsMissing, Send, _send
from .service import (
    USER_AGENT,
    GagelinkError,
    Quota,
    Retrieval,
    ServiceUnavailable,
    _explain,
    _trust_store,
)

SEARCH_URL = "https://cmr.earthdata.nasa.gov/search"

#: The products a water question usually wants, named as the repository names them.
#: Anything else it knows is accepted; these are a starting point rather than a limit.
COLLECTIONS = {
    "TELLUS_GRAC-GRFO_MASCON_CRI_GRID_RL06.3_V4": (
        "JPL mascon solution: monthly liquid water equivalent thickness on a global "
        "half-degree grid, 2002 to present."
    ),
    "GRACEDADM_CLSM025GL_7D": (
        "Drought indicators assimilating GRACE into a land surface model: groundwater "
        "and soil moisture percentiles, weekly, global."
    ),
    "GRACEDADM_CLSM0125US_7D": "The same drought indicators over the United States, finer.",
}

#: Water storage anomalies are published as an equivalent depth of liquid water, which is
#: a length and not a volume. Reading one as a volume is a plausible mistake and a large
#: one, so the unit is stated here rather than left to a file header this does not open.
UNITS = {
    "lwe_thickness": "centimeter",
    "uncertainty": "centimeter",
}


class GranuleNotFound(GagelinkError):
    """The repository holds no granule matching that request."""


@dataclass(frozen=True)
class Granule:
    """One file, with the window it covers and where it lives."""

    id: str
    title: str
    collection: str
    start: datetime | None = None
    end: datetime | None = None
    href: str | None = None
    size_mb: float | None = None

    @property
    def needs_credentials(self) -> bool:
        """Whether the file sits behind Earthdata login, which the protected path means."""
        return bool(self.href) and "protected" in self.href


def granule_from(collection: str, entry: Mapping[str, Any]) -> Granule:
    """Build a granule from one repository entry.

    The download link is the protected one where it exists, since the public sibling is
    the checksum rather than the data, and returning that as though it were the file
    would be the most misleading thing this module could do.
    """
    href = None
    for link in entry.get("links") or []:
        target = str((link or {}).get("href") or "")
        if target.startswith("https://") and "protected" in target:
            href = target
            break

    def moment(key: str) -> datetime | None:
        raw = entry.get(key)
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) if raw else None
        except ValueError:
            return None

    size = entry.get("granule_size")
    return Granule(
        id=str(entry.get("id") or ""),
        title=str(entry.get("title") or ""),
        collection=collection,
        start=moment("time_start"),
        end=moment("time_end"),
        href=href,
        size_mb=round(float(size), 1) if size else None,
    )


class Gravimetry:
    """A client for the gravity missions.

    Searching needs nothing. Downloading needs an Earthdata token, which is free and which
    nothing else in this package requires.
    """

    name = "grace"

    def __init__(
        self,
        token: str | None = None,
        *,
        send: Send | None = None,
        search_url: str = SEARCH_URL,
    ) -> None:
        self.token = token or os.environ.get("EARTHDATA_TOKEN")
        self.search_url = search_url.rstrip("/")
        self._send: Send = send or _send

    def _get(self, path: str, **params: Any) -> tuple[dict[str, Any], Retrieval]:
        query = urllib.parse.urlencode(
            {k: v for k, v in sorted(params.items()) if v is not None}
        )
        url = f"{self.search_url}/{path}?{query}"
        status, _, body = self._send(
            url, "GET", {"Accept": "application/json", "User-Agent": USER_AGENT}, None
        )
        if status >= 400:
            raise ServiceUnavailable(
                f"the metadata repository answered {status}{_explain(body)}"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                "the metadata repository answered with a body that is not JSON"
            ) from exc
        return payload, Retrieval.of(
            f"grace/{path.split('.')[0]}", url, params, status, body, Quota()
        )

    def collections(self, keyword: str = "GRACE mascon", limit: int = 10):
        """Search for products, which needs no account."""
        payload, retrieval = self._get(
            "collections.json", keyword=keyword, page_size=min(limit, 100)
        )
        found = [
            {
                "short_name": entry.get("short_name"),
                "title": entry.get("title"),
                "start": entry.get("time_start"),
            }
            for entry in (payload.get("feed") or {}).get("entry") or []
        ]
        return found, retrieval

    def granules(
        self,
        collection: str,
        start: str | None = None,
        end: str | None = None,
        limit: int = 10,
    ) -> tuple[list[Granule], Retrieval]:
        """Files within a product, optionally within a window. Needs no account."""
        window = f"{start}T00:00:00Z,{end}T00:00:00Z" if start and end else None
        payload, retrieval = self._get(
            "granules.json",
            short_name=collection,
            temporal=window,
            page_size=min(limit, 100),
        )
        entries = (payload.get("feed") or {}).get("entry") or []
        if not entries:
            raise GranuleNotFound(
                f"no granule in {collection}"
                + (f" between {start} and {end}" if window else "")
            )
        return [granule_from(collection, entry) for entry in entries], retrieval

    def download(self, granule: Granule, destination: str) -> str:
        """Fetch a granule's file, which needs an Earthdata token.

        The file is not read. It is a NetCDF grid, and a reader for gridded arrays is a
        dependency and a design this package has not taken on, so what comes back is the
        path and the knowledge of what is in it.
        """
        if not granule.href:
            raise GranuleNotFound(f"granule {granule.id} publishes no download link")
        if granule.needs_credentials and not self.token:
            raise CredentialsMissing(
                "this file sits behind Earthdata login. Register at "
                "urs.earthdata.nasa.gov, generate a token, and pass it to the client or "
                "set EARTHDATA_TOKEN. Searching needs no account; only the file does."
            )

        headers = {"User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(granule.href, headers=headers)
        with urllib.request.urlopen(request, timeout=600, context=_trust_store()) as response:
            data = response.read()
        with open(destination, "wb") as handle:
            handle.write(data)
        return destination
