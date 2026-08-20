"""Retrievals that do not answer within a tool call.

Everything else in this package answers a request while the caller waits. The reanalysis
and gravimetry archives do not: a request is queued, runs for anywhere between a minute and
an hour, and produces a file measured in megabytes or gigabytes. An agent cannot block on
that inside a tool call, and pretending the two shapes share an interface is the mistake
that would make the whole surface unusable.

So they do not share one. A synchronous source is fetched with `get_*` and returns its
values. A queued source is asked with `request_*`, which returns a handle immediately, and
the handle is polled with `check_job` and resolved with `open_dataset`. No tool mixes the
two, and the difference is visible in the name before a model calls anything.

The status vocabulary is the one the OGC API Processes standard defines, which is what the
Copernicus data store speaks, so nothing here invents its own words for states that already
have them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .service import (
    USER_AGENT,
    GagelinkError,
    Quota,
    Retrieval,
    ServiceUnavailable,
    _explain,
    _trust_store,
)

#: A job has been accepted and has not finished. Both are live states and neither is a
#: reason to give up.
PENDING = ("accepted", "running")

#: A job has finished, one way or another. `successful` is the only one with a result.
FINISHED = ("successful", "failed", "dismissed")

STATUSES = PENDING + FINISHED


class JobFailed(GagelinkError):
    """The archive ran the request and it did not produce a result."""


class JobNotReady(GagelinkError):
    """A result was asked for before the job finished."""


class CredentialsMissing(GagelinkError):
    """The archive requires an account and none is configured.

    Raised at submission rather than at import, so the rest of the package works for
    someone who has no account with either archive and never needs one.
    """


#: Submitting takes a body and a method, which the read-only seam elsewhere does not, so
#: it gets a seam of its own rather than contorting that one.
Send = Callable[
    [str, str, Mapping[str, str], bytes | None],
    "tuple[int, Mapping[str, str], str]",
]


def _send(
    url: str, method: str, headers: Mapping[str, str], body: bytes | None = None
) -> tuple[int, Mapping[str, str], str]:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=_trust_store()) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:  # pragma: no cover - needs the live archive
        return exc.code, dict(exc.headers or {}), exc.read().decode(errors="replace")
    except urllib.error.URLError as exc:  # pragma: no cover - needs the network
        raise ServiceUnavailable(f"could not reach {url}: {exc.reason}") from exc


@dataclass
class Job:
    """A queued retrieval, and everything needed to find it again.

    Carries the request that produced it, because a job whose result arrives without the
    question it answers cannot go in a manifest, and a queued retrieval is exactly the
    kind whose provenance is easiest to lose: the answer comes back an hour later, in a
    different conversation, as a file.
    """

    id: str
    service: str
    dataset: str
    request: dict[str, Any] = field(default_factory=dict)
    status: str = "accepted"
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    href: str | None = None
    message: str | None = None
    size: int | None = None

    @property
    def is_pending(self) -> bool:
        return self.status in PENDING

    @property
    def is_ready(self) -> bool:
        return self.status == "successful" and bool(self.href)

    def waited(self, now: datetime | None = None) -> float:
        """Seconds since submission, which is what a caller needs to pace polling."""
        return ((now or datetime.now(timezone.utc)) - self.submitted_at).total_seconds()

    def record(self) -> dict[str, Any]:
        """The manifest fragment for this job."""
        return {
            "service": self.service,
            "dataset": self.dataset,
            "job": self.id,
            "request": dict(self.request),
            "status": self.status,
            "submitted_at": self.submitted_at.isoformat(),
            "href": self.href,
            "size": self.size,
        }


class Archive:
    """Base for a queued source.

    Subclasses say how to submit, how to read a status, and where the result lives. The
    polling, the vocabulary, and the record keeping are here so two archives cannot drift
    into two different notions of what "running" means.
    """

    name = "archive"
    base_url = ""

    def __init__(self, api_key: str | None = None, *, send: Send | None = None) -> None:
        self.api_key = api_key
        self._send: Send = send or _send

    # Subclasses implement these three.

    def _submit_request(self, dataset: str, request: Mapping[str, Any]) -> tuple[int, str]:
        raise NotImplementedError

    def _status_request(self, job: Job) -> tuple[int, str]:
        raise NotImplementedError

    def _read_status(self, job: Job, payload: Mapping[str, Any]) -> Job:
        raise NotImplementedError

    # Shared behaviour.

    def _require_key(self) -> None:
        if not self.api_key:
            raise CredentialsMissing(
                f"{self.name} requires an account. Register for one, then pass the key "
                f"to the client or set it in the environment. Nothing else in this "
                f"package needs it."
            )

    def _decode(self, status: int, body: str, what: str) -> dict[str, Any]:
        if status == 401 or status == 403:
            raise CredentialsMissing(
                f"{self.name} refused the credentials supplied for {what}"
            )
        if status >= 400:
            raise ServiceUnavailable(f"{self.name} answered {status} for {what}{_explain(body)}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ServiceUnavailable(
                f"{self.name} answered {status} for {what} with a body that is not JSON"
            ) from exc

    def submit(self, dataset: str, request: Mapping[str, Any]) -> tuple[Job, Retrieval]:
        """Queue a retrieval and return a handle to it."""
        self._require_key()
        status, body = self._submit_request(dataset, request)
        payload = self._decode(status, body, f"a request against {dataset}")
        job = Job(
            id=str(payload.get("jobID") or payload.get("id") or ""),
            service=self.name,
            dataset=dataset,
            request=dict(request),
        )
        job = self._read_status(job, payload)
        return job, Retrieval.of(
            f"{self.name}/submit/{dataset}", self.base_url, request, status, body, Quota()
        )

    def check(self, job: Job) -> tuple[Job, Retrieval]:
        """Ask the archive where a job has got to."""
        self._require_key()
        status, body = self._status_request(job)
        payload = self._decode(status, body, f"job {job.id}")
        updated = self._read_status(job, payload)
        return updated, Retrieval.of(
            f"{self.name}/status/{job.id}", self.base_url, {}, status, body, Quota()
        )

    def download(self, job: Job, destination: str) -> str:
        """Fetch a finished job's file to a local path.

        The file is not read here. These archives publish array data in formats that need
        a reader this package does not carry, and pretending to interpret one would be
        worse than handing over the path and saying what it holds.
        """
        if job.status == "failed":
            raise JobFailed(f"job {job.id} failed: {job.message or 'no reason given'}")
        if not job.is_ready:
            raise JobNotReady(
                f"job {job.id} is {job.status} after {job.waited():.0f} seconds; "
                f"check_job again before asking for the result"
            )
        request = urllib.request.Request(job.href, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=300, context=_trust_store()) as response:
            data = response.read()
        with open(destination, "wb") as handle:
            handle.write(data)
        job.size = len(data)
        return destination
