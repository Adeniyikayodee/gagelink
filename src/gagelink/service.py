"""Transport against the USGS Water Data APIs.

Every request the toolkit makes passes through here, because the manifest that makes a
session re-runnable is assembled from what this layer records rather than reconstructed
afterwards. A retrieval therefore carries the resolved URL, the parameters, the moment it
was made, the HTTP status, and a hash of the body, which is what a later replay compares
against.

The service is an OGC API, so every collection shares one query grammar. That is the
property this layer relies on: one request function covers observations, metadata,
reference codes, and revisions, and the differences between them belong to normalisation
rather than to transport.

Two constraints from the service shape the design. Requests are limited to 50 per IP per
hour unauthenticated and 1,000 per hour with a free key, which makes caching load-bearing
rather than an optimisation, and the remaining allowance is published on every response,
which makes it something an agent can be told rather than something it discovers by
failing.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

import certifi

BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v0"

#: Collections the service publishes, read from its own index rather than assumed. An
#: unknown name is refused here so that a typo fails at the call rather than as an opaque
#: 404 several layers away.
COLLECTIONS = frozenset(
    {
        # Observations.
        "continuous",
        "latest-continuous",
        "daily",
        "latest-daily",
        "field-measurements",
        "latest-field-measurements",
        "channel-measurements",
        "peaks",
        # Metadata.
        "monitoring-locations",
        "time-series-metadata",
        "field-measurements-metadata",
        "combined-metadata",
        # Changes and deletions to approved record. This is what makes a replay able to
        # separate a revised measurement from a changed calculation.
        "time-series-revisions",
        # Reference vocabularies, which is where the datum, parameter, statistic, and
        # timezone codes come from instead of being guessed.
        "agency-codes",
        "altitude-datums",
        "aquifer-codes",
        "aquifer-types",
        "coordinate-accuracy-codes",
        "coordinate-datum-codes",
        "coordinate-method-codes",
        "counties",
        "countries",
        "hydrologic-unit-codes",
        "medium-codes",
        "national-aquifer-codes",
        "parameter-codes",
        "reliability-codes",
        "site-types",
        "states",
        "statistic-codes",
        "time-zone-codes",
        "topographic-codes",
    }
)

USER_AGENT = "gagelink (+https://github.com/Adeniyikayodee/gagelink)"


class GagelinkError(Exception):
    """Base for every error this package raises."""


class UnknownCollection(GagelinkError):
    """A collection the service does not publish."""


class QuotaExhausted(GagelinkError):
    """The hourly request allowance is spent.

    Carried as its own type because the repair differs from every other failure: waiting
    or configuring a key fixes it, and retrying immediately does not.
    """


class ServiceUnavailable(GagelinkError):
    """The service answered with an error, or did not answer."""


@dataclass(frozen=True)
class Quota:
    """What the service says is left of the hourly allowance.

    Both fields are optional because the headers are not guaranteed, and an absent
    allowance is reported as unknown rather than as unlimited.
    """

    limit: int | None = None
    remaining: int | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "Quota":
        lowered = {k.lower(): v for k, v in headers.items()}

        def read(name: str) -> int | None:
            raw = lowered.get(name)
            try:
                return int(raw) if raw is not None else None
            except ValueError:
                return None

        return cls(limit=read("x-ratelimit-limit"), remaining=read("x-ratelimit-remaining"))

    @property
    def is_known(self) -> bool:
        return self.remaining is not None


@dataclass(frozen=True)
class Retrieval:
    """One request, recorded in the form a manifest needs.

    ``url`` never carries the API key, since a manifest is meant to be published and a key
    pasted into a query string would travel with it. The key goes in a header instead, and
    is not recorded at all.
    """

    collection: str
    url: str
    params: Mapping[str, Any]
    retrieved_at: datetime
    status: int
    sha256: str
    size: int
    quota: Quota = field(default=Quota())
    from_cache: bool = False
    #: The response body, kept so a session can be replayed against what it actually saw
    #: rather than against what the service holds today. Deliberately absent from
    #: ``record()``, since a manifest carrying every body would be unreadable and the hash
    #: is what a comparison needs.
    body: str | None = None

    @classmethod
    def of(
        cls,
        collection: str,
        url: str,
        params: Mapping[str, Any],
        status: int,
        body: str,
        quota: "Quota",
        *,
        from_cache: bool = False,
    ) -> "Retrieval":
        """Build a record from a response, stamping it with the moment it arrived.

        Shared by every client rather than written once per service, so that a manifest
        holds one shape of record regardless of which service produced it.
        """
        return cls(
            collection=collection,
            url=url,
            params=dict(params),
            retrieved_at=datetime.now(timezone.utc),
            status=status,
            sha256=hashlib.sha256(body.encode()).hexdigest(),
            size=len(body),
            quota=quota,
            from_cache=from_cache,
            body=body,
        )

    def record(self) -> dict[str, Any]:
        """The manifest fragment for this request."""
        return {
            "collection": self.collection,
            "url": self.url,
            "params": dict(self.params),
            "retrieved_at": self.retrieved_at.isoformat(),
            "status": self.status,
            "sha256": self.sha256,
            "size": self.size,
            "from_cache": self.from_cache,
        }


def more_pages(page: Mapping[str, Any]) -> bool:
    """Whether the service is holding back rows behind a next link.

    `numberMatched` is published as null, so the only signal that a response is partial is
    the presence of that link. Without checking it a default page of ten reads as the whole
    of what a station measures, which is how a station's discharge can be absent from a
    listing that appears complete.
    """
    return any(
        (link or {}).get("rel") == "next" for link in page.get("links") or []
    )


class Cache(Protocol):
    """Anything that can hold a response body against its resolved URL."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, body: str) -> None: ...


class MemoryCache(dict):
    """A cache for the life of one process.

    Sufficient for a single agent session, which is where the repeated requests are: a
    conversation asks about the same three sites a dozen times.
    """

    def get(self, key: str) -> str | None:  # type: ignore[override]
        return dict.get(self, key)

    def set(self, key: str, body: str) -> None:
        self[key] = body


#: A fetch takes a URL and headers, and returns status, response headers, and body. It is
#: replaceable so that tests run against recorded responses and no test needs the network.
Fetch = Callable[[str, Mapping[str, str]], tuple[int, Mapping[str, str], str]]


def _trust_store() -> ssl.SSLContext:
    """A context that can verify the services this package talks to.

    A Python installed outside the system keychain ships without a trust store, which is
    the default on macOS for a python.org build, and every HTTPS call then fails
    verification. Since fetching over HTTPS is the whole of what this package does, the
    certificates come from certifi rather than from whatever the interpreter happened to
    be installed with.
    """
    return ssl.create_default_context(cafile=certifi.where())


def _http(url: str, headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], str]:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30, context=_trust_store()) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:  # pragma: no cover - needs the live service
        return exc.code, dict(exc.headers or {}), exc.read().decode(errors="replace")
    except urllib.error.URLError as exc:  # pragma: no cover - needs the network
        reason = str(exc.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            # Reported separately because it is not a service fault and the fix is local.
            # A python.org build on macOS ships without a trust store until
            # "Install Certificates.command" is run, so every HTTPS call fails this way.
            raise ServiceUnavailable(
                "TLS verification failed locally rather than at the service. On macOS "
                "run the Install Certificates.command in your Python installation, or "
                "pass a fetch built on a context using certifi."
            ) from exc
        raise ServiceUnavailable(f"could not reach {url}: {reason}") from exc


def _explain(body: str) -> str:
    """The service's own account of a failure, when it gave one.

    Errors come back as JSON carrying a code and a description, and "At least one
    requested property wasn't found" identifies a wrong query parameter immediately,
    whereas a bare 400 does not.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    description = payload.get("description") or payload.get("detail")
    code = payload.get("code")
    if description and code:
        return f": {code}, {description}"
    return f": {description or code}" if (description or code) else ""


class Service:
    """A client for one OGC API deployment.

    >>> service = Service(fetch=recorded)
    >>> page, retrieval = service.items("monitoring-locations", id="USGS-07374000")
    >>> retrieval.quota.remaining
    999
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        fetch: Fetch | None = None,
        cache: Cache | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._fetch: Fetch = fetch or _http
        self._cache: Cache = cache if cache is not None else MemoryCache()
        self.quota = Quota()

    def url_for(self, collection: str, **params: Any) -> str:
        """The resolved URL for a query, with no key in it."""
        if collection not in COLLECTIONS:
            raise UnknownCollection(
                f"{collection!r} is not a collection this service publishes; "
                f"the observation collections are continuous, daily, "
                f"latest-continuous, latest-daily, field-measurements, and peaks"
            )
        query = {k: v for k, v in sorted(params.items()) if v is not None}
        encoded = urllib.parse.urlencode(query, doseq=True)
        stem = f"{self.base_url}/collections/{collection}/items"
        return f"{stem}?{encoded}" if encoded else stem

    def items(self, collection: str, **params: Any) -> tuple[dict[str, Any], Retrieval]:
        """Fetch one page from a collection, with the record of having done so.

        The parsed body and the retrieval are returned together rather than the body
        alone, because a number that reaches an answer without the request that produced
        it cannot be replayed, and making that pairing the only shape the API offers is
        cheaper than remembering to record it.
        """
        url = self.url_for(collection, **params)
        cached = self._cache.get(url)
        if cached is not None:
            return json.loads(cached), Retrieval.of(
                collection, url, params, 200, cached, self.quota, from_cache=True
            )

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key

        status, response_headers, body = self._fetch(url, headers)
        quota = Quota.from_headers(response_headers)
        if quota.is_known:
            self.quota = quota

        if status == 429:
            raise QuotaExhausted(
                "the hourly request allowance is spent"
                + (
                    f", and the limit is {quota.limit} requests per hour"
                    if quota.limit
                    else ""
                )
                + (
                    "; an API key from https://api.waterdata.usgs.gov/signup raises the "
                    "limit from 50 to 1000 requests per hour"
                    if not self.api_key
                    else "; the allowance resets hourly"
                )
            )
        if status >= 400:
            raise ServiceUnavailable(
                f"{collection} answered {status} for {url}{_explain(body)}"
            )

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"{collection} answered {status} with a body that is not JSON"
            ) from exc

        self._cache.set(url, body)
        return parsed, Retrieval.of(collection, url, params, status, body, quota)

