"""Re-running a session, and saying why the answer changed.

Hydrology reproduces at 1.6% in the tested literature. The usual explanation is that data
and code were not published, and that explanation hides the more interesting failure, which
is that a published pipeline against a live service does not reproduce either, because the
service revised the record underneath it. Provisional discharge becomes approved discharge
and the number moves. Nothing in the existing tooling separates that from a code change.

Three modes, and the distinction between them is the whole point.

`offline` recomputes from the archived bodies with today's code and checks that every
quantity the manifest recorded still appears. Nothing is fetched, so the data cannot have
moved, and any difference is the code or a library beneath it.

`strict` re-fetches every request and requires the responses to be identical. It answers
whether anything at all has drifted, and against a live service the honest expectation is
that it usually fails.

`revision_aware` re-fetches, compares value by value, and asks the service whether each
difference falls inside a revision it has published. A result that changed because 400
provisional values were later approved is a different fact about the science than a result
that changed because the code changed, and that decomposition is what has not previously
been available.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .normalise import readings_from
from .service import Service, ServiceUnavailable

class CorruptArchive(Exception):
    """An archived body that does not hash to the key it is filed under.

    Raised rather than reported, because every verdict a replay reaches rests on the
    archive being what the session saw, and a bundle that fails this makes a difference
    unattributable rather than merely unexplained.
    """


OFFLINE = "offline"
STRICT = "strict"
REVISION_AWARE = "revision_aware"
MODES = (OFFLINE, STRICT, REVISION_AWARE)

#: How close two magnitudes must be to count as the same measurement. Tight, because the
#: question is whether the number changed rather than whether it changed much.
TOLERANCE = 1e-9


@dataclass(frozen=True)
class ValueChange:
    """One measurement that differs between the original session and the re-run."""

    location: str
    parameter: str
    time: str
    before: float | None
    after: float | None
    unit: str | None = None
    time_series_id: str | None = None
    #: `revised` where the service published a revision covering this reading, `changed`
    #: where it did not, and `appeared` or `withdrawn` where one side has no value.
    verdict: str = "changed"
    revision_note: str | None = None

    def line(self) -> str:
        moved = f"{self.before} -> {self.after}"
        if self.before is None:
            moved = f"appeared as {self.after}"
        elif self.after is None:
            moved = f"withdrawn, was {self.before}"
        note = f", {self.revision_note}" if self.revision_note else ""
        return (
            f"[{self.verdict}] {self.location} {self.parameter} at {self.time}: "
            f"{moved} {self.unit or ''}{note}".rstrip()
        )


@dataclass(frozen=True)
class RetrievalDiff:
    """What became of one recorded request when it was made again."""

    collection: str
    url: str
    #: `unchanged`, `changed`, `unavailable`, or `unarchived`.
    status: str
    before_sha: str | None = None
    after_sha: str | None = None
    changes: tuple[ValueChange, ...] = ()
    detail: str | None = None


@dataclass
class ReplayReport:
    """The outcome of a replay, and the account of why it came out that way."""

    mode: str
    ran_at: str
    diffs: list[RetrievalDiff] = field(default_factory=list)
    missing_quantities: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[ValueChange]:
        return [change for diff in self.diffs for change in diff.changes]

    @property
    def reproduced(self) -> bool:
        """Whether the session came out the same way, on this mode's definition of same.

        A revision-aware replay counts a difference the service has published a revision
        for as reproduced, because the pipeline did the same thing to different data. That
        is a deliberate choice and it is why the mode is named rather than being the
        default.
        """
        if self.missing_quantities:
            return False
        if self.mode == REVISION_AWARE:
            return all(c.verdict == "revised" for c in self.changes) and not any(
                d.status in {"unavailable", "unarchived"} for d in self.diffs
            )
        return all(d.status == "unchanged" for d in self.diffs)

    @property
    def verdict(self) -> str:
        """One word for what happened, chosen so the three causes stay distinguishable."""
        if self.missing_quantities:
            return "code_changed"
        if any(d.status in {"unavailable", "unarchived"} for d in self.diffs):
            return "incomplete"
        if self.reproduced:
            return "reproduced"
        if self.mode == REVISION_AWARE and all(
            c.verdict == "revised" for c in self.changes if c.verdict != "unchanged"
        ):
            return "revised"
        return "changed"

    def report(self) -> str:
        lines = [f"{self.mode} replay at {self.ran_at}: {self.verdict}"]
        for diff in self.diffs:
            lines.append(f"  {diff.status:<12} {diff.collection}" + (
                f" ({diff.detail})" if diff.detail else ""
            ))
            for change in diff.changes[:10]:
                lines.append(f"      {change.line()}")
            if len(diff.changes) > 10:
                lines.append(f"      and {len(diff.changes) - 10} further differences")
        for quantity in self.missing_quantities:
            lines.append(
                f"  no longer produced: {quantity.get('value')} "
                f"{quantity.get('unit')} from {quantity.get('tool')}."
                f"{quantity.get('field')}"
            )
        lines.extend(f"  note: {note}" for note in self.notes)
        return "\n".join(lines)


def load(path: str | Path) -> dict[str, Any]:
    """Read a bundle written by `Session.save`."""
    return json.loads(Path(path).read_text())


def _key(reading: Any) -> tuple[str, str, str]:
    moment = reading.observed_at or reading.observed_on
    return (
        reading.location_id,
        reading.parameter_code,
        moment.isoformat() if moment else "",
    )


def _readings(body: str) -> dict[tuple[str, str, str], Any]:
    """Every reading in a body, keyed so two bodies can be compared position by position.

    Only observation collections yield readings. A site record or a basin polygon returns
    nothing here, which is correct: those are compared by hash and have no values to
    diff.
    """
    try:
        page = json.loads(body)
    except json.JSONDecodeError:
        return {}
    if not isinstance(page, dict) or "features" not in page:
        return {}
    found = {}
    for reading in readings_from(page):
        if reading.parameter_code:
            found[_key(reading)] = reading
    return found


def _compare(before: str, after: str) -> list[ValueChange]:
    """Value-level differences between two bodies for the same request."""
    old, new = _readings(before), _readings(after)
    changes = []
    for key in sorted(set(old) | set(new)):
        was, now = old.get(key), new.get(key)
        before_value = None if was is None or was.value is None else was.value.magnitude
        after_value = None if now is None or now.value is None else now.value.magnitude
        if before_value is not None and after_value is not None:
            if abs(before_value - after_value) <= TOLERANCE * max(
                1.0, abs(before_value)
            ):
                continue
        elif before_value is None and after_value is None:
            continue

        source = now or was
        changes.append(
            ValueChange(
                location=key[0],
                parameter=key[1],
                time=key[2],
                before=before_value,
                after=after_value,
                unit=source.unit_published,
                time_series_id=source.time_series_id,
                verdict="appeared"
                if before_value is None
                else "withdrawn"
                if after_value is None
                else "changed",
            )
        )
    return changes


class Revisions:
    """Whether the service has published a revision covering a reading.

    Queried by time series where the reading names one, since that is an exact join, and
    falling back to the monitoring location otherwise. Answers are cached for the life of
    the replay because one changed series produces many changed readings and they all ask
    the same question.
    """

    def __init__(self, service: Service) -> None:
        self.service = service
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def covering(self, change: ValueChange) -> dict[str, Any] | None:
        for key, params in (
            (change.time_series_id, {"time_series_id": change.time_series_id}),
            (change.location, {"monitoring_location_id": change.location}),
        ):
            if not key:
                continue
            for revision in self._revisions(key, params):
                if _overlaps(change.time, revision.get("begin"), revision.get("end")):
                    return revision
        return None

    def _revisions(self, key: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if key not in self._cache:
            try:
                page = self.service.items(
                    "time-series-revisions", limit=200, **params
                )[0]
                self._cache[key] = [
                    f.get("properties") or {} for f in page.get("features") or []
                ]
            except ServiceUnavailable:
                self._cache[key] = []
        return self._cache[key]


def _span(moment: str) -> tuple[datetime, datetime] | None:
    """The interval a reading covers.

    An instant covers itself. A daily value covers its day, and treating it as the instant
    of midnight would put it outside a revision window that opened later the same day,
    which is how the first day of every revision would otherwise go unattributed.
    """
    if not moment:
        return None
    try:
        at = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        # Date-only, which the service publishes for daily and peak record. Read as a
        # whole UTC day, which is the frame the revision windows are published in.
        start = at.replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=1)
    return at, at


def _overlaps(moment: str, begin: str | None, end: str | None) -> bool:
    """Whether a reading falls inside a revision's window, or overlaps it."""
    span = _span(moment)
    if span is None or not begin or not end:
        return False
    try:
        start = datetime.fromisoformat(str(begin).replace("Z", "+00:00"))
        finish = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None or finish.tzinfo is None:
        return False
    return span[0] <= finish and span[1] >= start


def replay(
    bundle: Mapping[str, Any],
    mode: str = REVISION_AWARE,
    service: Service | None = None,
    now: str | None = None,
) -> ReplayReport:
    """Re-run a recorded session and account for whatever came out differently."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")

    manifest = bundle.get("manifest") or {}
    archive = bundle.get("archive") or {}
    report = ReplayReport(
        mode=mode,
        ran_at=now or datetime.now().astimezone().isoformat(timespec="seconds"),
    )

    corrupt = [
        digest
        for digest, body in archive.items()
        if hashlib.sha256(body.encode()).hexdigest() != digest
    ]
    if corrupt:
        # Every verdict below rests on the archive being what the session actually saw. A
        # body that does not hash to its own key makes a difference unattributable rather
        # than merely unexplained, so it is refused instead of being reported.
        raise CorruptArchive(
            f"{len(corrupt)} archived bodies do not hash to their keys, so this bundle "
            f"cannot be replayed: {', '.join(d[:12] for d in corrupt[:3])}"
        )

    if mode == OFFLINE:
        return _offline(manifest, archive, report)

    client = service if service is not None else Service()
    revisions = Revisions(client) if mode == REVISION_AWARE else None

    for record in manifest.get("retrievals") or []:
        before = archive.get(record.get("sha256") or "")
        if record.get("collection", "").startswith(("nwps/", "nldi/")):
            report.notes.append(
                f"{record['collection']} was not re-fetched: only the USGS collections "
                f"publish revisions, so a difference there could not be attributed"
            )
            continue

        try:
            page, retrieval = client.items(
                record.get("collection", ""), **(record.get("params") or {})
            )
        except (ServiceUnavailable, Exception) as exc:  # noqa: BLE001 - reported, not raised
            report.diffs.append(
                RetrievalDiff(
                    collection=record.get("collection", ""),
                    url=record.get("url", ""),
                    status="unavailable",
                    before_sha=record.get("sha256"),
                    detail=str(exc)[:120],
                )
            )
            continue

        after_sha = retrieval.sha256
        if after_sha == record.get("sha256"):
            report.diffs.append(
                RetrievalDiff(
                    collection=record.get("collection", ""),
                    url=record.get("url", ""),
                    status="unchanged",
                    before_sha=record.get("sha256"),
                    after_sha=after_sha,
                )
            )
            continue

        if before is None:
            report.diffs.append(
                RetrievalDiff(
                    collection=record.get("collection", ""),
                    url=record.get("url", ""),
                    status="unarchived",
                    before_sha=record.get("sha256"),
                    after_sha=after_sha,
                    detail="the response differs and the original was not archived, so "
                    "the difference cannot be examined",
                )
            )
            continue

        changes = _compare(before, retrieval.body or "")
        if revisions is not None:
            changes = [_attribute(change, revisions) for change in changes]

        report.diffs.append(
            RetrievalDiff(
                collection=record.get("collection", ""),
                url=record.get("url", ""),
                status="changed",
                before_sha=record.get("sha256"),
                after_sha=after_sha,
                changes=tuple(changes),
            )
        )

    return report


def _attribute(change: ValueChange, revisions: Revisions) -> ValueChange:
    """Ask the service whether it published a revision covering this reading."""
    revision = revisions.covering(change)
    if revision is None:
        return change
    note = str(revision.get("revision_note") or "").strip()
    return ValueChange(
        location=change.location,
        parameter=change.parameter,
        time=change.time,
        before=change.before,
        after=change.after,
        unit=change.unit,
        time_series_id=change.time_series_id,
        verdict="revised",
        revision_note=note[:160] or "a revision covers this period",
    )


def _offline(
    manifest: Mapping[str, Any],
    archive: Mapping[str, str],
    report: ReplayReport,
) -> ReplayReport:
    """Recompute from the archived bodies and check the recorded numbers still appear.

    Nothing is fetched, so the data cannot have moved and any difference is the code or a
    library beneath it. Derived values are excluded, since they were computed outside the
    retrieval path and there is nothing here to recompute them from.
    """
    produced: list[tuple[float, str]] = []
    for record in manifest.get("retrievals") or []:
        body = archive.get(record.get("sha256") or "")
        if body is None:
            report.diffs.append(
                RetrievalDiff(
                    collection=record.get("collection", ""),
                    url=record.get("url", ""),
                    status="unarchived",
                    before_sha=record.get("sha256"),
                    detail="no archived body, so nothing can be recomputed from it",
                )
            )
            continue
        for reading in _readings(body).values():
            if reading.value is not None:
                produced.append(
                    (reading.value.magnitude, format(reading.value.units, "~"))
                )
        report.diffs.append(
            RetrievalDiff(
                collection=record.get("collection", ""),
                url=record.get("url", ""),
                status="unchanged",
                before_sha=record.get("sha256"),
                after_sha=record.get("sha256"),
            )
        )

    derived = 0
    for quantity in manifest.get("quantities") or []:
        if quantity.get("tool") == "(derived)":
            derived += 1
            continue
        # The ledger writes these as `value` and `unit`, in the short pint spelling, so
        # both sides of the comparison are rendered the same way.
        magnitude = quantity.get("value")
        units = str(quantity.get("unit"))
        if not isinstance(magnitude, (int, float)):
            continue  # a series-valued output, which is recorded but never matched
        if not any(
            abs(magnitude - value) <= TOLERANCE * max(1.0, abs(magnitude))
            and units == unit
            for value, unit in produced
        ):
            report.missing_quantities.append(quantity)

    if derived:
        report.notes.append(
            f"{derived} derived quantities were not checked, having been computed outside "
            f"the retrieval path with nothing archived to recompute them from"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    """Replay a saved bundle from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="gagelink-replay",
        description="Re-run a saved session and account for what came out differently.",
    )
    parser.add_argument("bundle", help="a file written by Session.save")
    parser.add_argument(
        "--mode",
        default=REVISION_AWARE,
        choices=MODES,
        help=(
            "offline recomputes from the archived bodies and isolates code change; "
            "strict re-fetches and requires identical responses; revision_aware "
            "re-fetches and asks the service whether each difference is one it published"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GAGELINK_API_KEY") or os.environ.get("USGS_API_KEY"),
    )
    args = parser.parse_args(argv)

    report = replay(
        load(args.bundle),
        mode=args.mode,
        service=Service(api_key=args.api_key),
    )
    print(report.report())
    # A replay that did not reproduce is a finding rather than a fault, so the exit code
    # separates the two: 1 says the answer moved, and anything else would have raised.
    return 0 if report.reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
