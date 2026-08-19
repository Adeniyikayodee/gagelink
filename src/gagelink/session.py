"""A unit of agent work, and the record of what it did.

Everything a session retrieves passes through here, so the manifest is assembled from what
happened rather than reconstructed from what was supposed to have happened. That
distinction is the whole reason the session exists: a pipeline that reports its own
provenance from its source code reports the provenance it intended, and the two differ
exactly when something went wrong.

Opened as a context manager it also opens a quantity-guard ledger, so that quantities
crossing guarded tool boundaries are recorded alongside the requests that produced them and
a final answer can be audited against both.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from quantity_guard import __version__ as quantity_guard_version
from quantity_guard import session as quantity_ledger

from . import __version__
from .normalise import Location, location_from
from .nldi import Basin, Network, NetworkSite
from .nwps import Forecasts, Gauge
from quantity_guard import Q

from .service import Retrieval, Service


class Session:
    """State for one question, and the manifest that lets it be re-run.

    >>> with Session(question="freeboard at Little Falls?") as work:
    ...     station = work.location("USGS-01646500")
    ...     work.manifest()["retrievals"][0]["collection"]
    'monitoring-locations'
    """

    def __init__(
        self,
        service: Service | None = None,
        *,
        forecasts: Forecasts | None = None,
        network: Network | None = None,
        api_key: str | None = None,
        question: str = "",
    ) -> None:
        self.service = service if service is not None else Service(api_key=api_key)
        self.forecasts = forecasts if forecasts is not None else Forecasts()
        self.network = network if network is not None else Network()
        self.question = question
        self.started_at = datetime.now(timezone.utc)
        self.retrievals: list[Retrieval] = []
        #: Response bodies by hash, so a replay can recompute from what was actually seen.
        self.archive: dict[str, str] = {}
        self.locations: dict[str, Location] = {}
        self.series: dict[str, dict[str, Any]] = {}
        self.gauges: dict[str, Gauge] = {}
        self.basins: dict[str, Basin] = {}
        self.ledger = None
        self._stack: ExitStack | None = None

    def __enter__(self) -> "Session":
        self._stack = ExitStack()
        self.ledger = self._stack.enter_context(quantity_ledger(context=self.question))
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._stack is not None:
            self._stack.close()
        self._stack = None

    # Retrieval ---------------------------------------------------------------------

    def items(self, collection: str, **params: Any) -> dict[str, Any]:
        """Fetch a page and keep the record of having fetched it."""
        page, retrieval = self.service.items(collection, **params)
        self._keep(retrieval)
        return page

    def _keep(self, retrieval: Retrieval) -> None:
        """Record a retrieval and archive the body it returned.

        The archive is what separates a replay from a fresh run. Without the original
        bodies a re-run can only be compared against today's data, which cannot tell a
        changed answer caused by revised record from one caused by changed code.
        """
        self.retrievals.append(retrieval)
        if retrieval.body is not None:
            self.archive.setdefault(retrieval.sha256, retrieval.body)

    def location(self, identifier: str) -> Location | None:
        """A monitoring location, fetched once per session and registered on arrival.

        Registering on arrival rather than on demand is what makes a stage arriving later
        carry the station's frame. Doing it lazily would leave the first stage retrieved
        before the site record as a bare length, which is the shape the whole package is
        built to refuse.
        """
        if identifier in self.locations:
            return self.locations[identifier]

        page = self.items("monitoring-locations", id=identifier)
        features = page.get("features") or []
        if not features:
            return None

        station = location_from(features[0])
        station.register()
        self.locations[identifier] = station
        return station

    def gauge(self, identifier: str) -> Gauge:
        """A forecast point, fetched once per session.

        Accepts the USGS station number as readily as the NWS location id, since a
        question arrives naming the station and requiring the caller to translate first
        would be a step with no purpose.
        """
        if identifier in self.gauges:
            return self.gauges[identifier]

        number = identifier.split("-")[-1]
        gauge, retrieval = self.forecasts.gauge(number)
        self._keep(retrieval)
        self.gauges[identifier] = gauge
        return gauge

    def navigate(
        self, identifier: str, direction: str, distance_km: float, target: str = "nwissite"
    ) -> list[NetworkSite]:
        """Features along the river from a starting point."""
        sites, retrieval = self.network.navigate(
            identifier, direction=direction, target=target, distance_km=distance_km
        )
        self._keep(retrieval)
        return sites

    def basin(self, identifier: str) -> Basin | None:
        """The area draining to a point, delineated once per session."""
        if identifier in self.basins:
            return self.basins[identifier]

        basin, retrieval = self.network.basin(identifier)
        self._keep(retrieval)
        if basin is not None:
            self.basins[identifier] = basin
        return basin

    # Provenance --------------------------------------------------------------------

    def record(self, tool: str, field: str, quantity: Q) -> Q:
        """Enter a quantity a tool is returning into the ledger.

        Without this a retrieved value is indistinguishable from an invented one, because
        the answer audit checks a number against what tools recorded and nothing else. It
        is a no-op outside the context manager, where there is no ledger to write to.
        """
        if self.ledger is not None:
            # The role must be "output": the ledger's answer audit matches against
            # outputs and derived values only, so a quantity filed under any other role
            # is recorded for the manifest and then never matched.
            self.ledger.record(tool, "output", field, quantity)
        return quantity

    def record_derived(self, quantity: Q, note: str = "") -> Q:
        """Enter a value computed from retrieved ones, such as a series mean.

        Recorded separately because it is a different claim: the audit can then accept a
        summary statistic without accepting an arbitrary number.
        """
        if self.ledger is not None:
            self.ledger.record_derived(quantity, note)
        return quantity


    @property
    def quota_remaining(self) -> int | None:
        return self.service.quota.remaining

    def manifest(self) -> dict[str, Any]:
        """The record of this session, sufficient to re-run it.

        Three parts, and each answers a different question when a re-run disagrees with
        the original. The retrievals say what was asked of the service and what came
        back, so a changed body hash localises the difference to the data. The quantities
        say what crossed the tool boundaries, so a difference there with unchanged hashes
        localises it to the code. The versions say what was running, since a library
        change is the third possibility and is otherwise indistinguishable from the
        second.
        """
        manifest: dict[str, Any] = {
            "question": self.question,
            "started_at": self.started_at.isoformat(),
            "versions": {
                "gagelink": __version__,
                "quantity-guard": quantity_guard_version,
            },
            "retrievals": [r.record() for r in self.retrievals],
            "locations": sorted(self.locations),
        }
        if self.ledger is not None:
            manifest["quantities"] = self.ledger.manifest()["quantities"]
        return manifest

    def bundle(self) -> dict[str, Any]:
        """The manifest together with the bodies it refers to.

        Kept as one object because the two are useless apart: a manifest without the
        bodies cannot be replayed offline, and bodies without the manifest do not say
        what was asked for or what was made of them.
        """
        return {"manifest": self.manifest(), "archive": dict(self.archive)}

    def save(self, path: str | Path) -> Path:
        """Write the bundle where a later replay can find it."""
        destination = Path(path)
        destination.write_text(json.dumps(self.bundle(), indent=1))
        return destination

    def audit(self, answer: str) -> Any:
        """Classify every number in an answer against what was actually retrieved.

        Available only inside the context manager, since outside it nothing recorded the
        quantities to check the answer against.
        """
        if self.ledger is None:
            raise RuntimeError(
                "auditing needs the ledger, which is opened by using the session as a "
                "context manager: with Session(question=...) as work:"
            )
        return self.ledger.audit_answer(answer, context=self.question)
