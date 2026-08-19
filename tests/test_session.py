"""What a session records, and what that record is for."""

import json
from pathlib import Path

import pytest

from gagelink import Service, Session, Toolkit

FIXTURES = Path(__file__).parent / "fixtures"
LOCATION = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())
LATEST = json.loads((FIXTURES / "latest_continuous_07374000.json").read_text())


def fetch(url, headers):
    page = LOCATION if "monitoring-locations" in url else LATEST
    return 200, {"X-RateLimit-Remaining": "997"}, json.dumps(page)


def test_the_manifest_records_every_request_that_was_made():
    """Assembled from what happened rather than from what was supposed to happen, which
    is the distinction that matters when a re-run disagrees."""
    with Session(service=Service(fetch=fetch), question="how high?") as work:
        Toolkit(work).get_latest("USGS-07374000")
        manifest = work.manifest()

    collections = [r["collection"] for r in manifest["retrievals"]]
    assert collections == ["monitoring-locations", "latest-continuous"]
    assert all(len(r["sha256"]) == 64 for r in manifest["retrievals"])
    assert manifest["question"] == "how high?"


def test_the_manifest_names_the_versions_that_produced_it():
    """A library change is the third way a replay can disagree, and it is otherwise
    indistinguishable from a code change."""
    with Session(service=Service(fetch=fetch)) as work:
        versions = work.manifest()["versions"]
    assert set(versions) == {"gagelink", "quantity-guard"}


def test_a_location_is_fetched_once_per_session():
    """The allowance is 50 requests an hour without a key, and a conversation asks about
    the same station repeatedly."""
    with Session(service=Service(fetch=fetch)) as work:
        kit = Toolkit(work)
        kit.describe_location("USGS-07374000")
        kit.describe_location("USGS-07374000")
        assert [r["collection"] for r in work.manifest()["retrievals"]] == [
            "monitoring-locations"
        ]


def test_a_station_is_registered_when_it_is_fetched_rather_than_when_it_is_needed():
    """A stage retrieved before the site record would otherwise arrive as a bare length,
    which is the shape the package exists to refuse."""
    with Session(service=Service(fetch=fetch)) as work:
        stage = next(
            r
            for r in Toolkit(work).get_latest("USGS-07374000").data["readings"]
            if r["parameter_code"] == "00065"
        )
        assert stage["value"].datum == "GAGE:07374000"


def test_an_answer_can_be_audited_against_what_was_retrieved():
    """The check that separates a number a tool returned from one the model produced."""
    with Session(service=Service(fetch=fetch), question="how high is the river?") as work:
        Toolkit(work).get_latest("USGS-07374000")
        good = work.audit("The gage height is 8.09 ft.")
        invented = work.audit("The gage height is 41.2 ft.")

    assert good.ok
    assert not invented.ok


def test_auditing_outside_the_context_manager_says_why_it_cannot():
    work = Session(service=Service(fetch=fetch))
    with pytest.raises(RuntimeError, match="context manager"):
        work.audit("anything")


def test_a_session_still_records_retrievals_outside_the_context_manager():
    """The ledger needs the scope; the request record does not."""
    work = Session(service=Service(fetch=fetch))
    Toolkit(work).describe_location("USGS-07374000")
    manifest = work.manifest()
    assert manifest["retrievals"]
    assert "quantities" not in manifest
