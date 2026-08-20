"""Queued retrievals, and the two archives that need an account.

Nothing here can be driven end to end without credentials, which is itself a fact worth
encoding: the boundary between what is open and what is not is tested, so a change that
moves it fails rather than surprising someone mid-question.
"""

import json

import pytest

from gagelink.era5 import UNITS, VARIABLES, Climate
from gagelink.grace import Granule, Gravimetry, granule_from
from gagelink.jobs import (
    FINISHED,
    PENDING,
    CredentialsMissing,
    Job,
    JobFailed,
    JobNotReady,
)
from gagelink.service import ServiceUnavailable


def sending(status=200, payload=None):
    calls = []

    def send(url, method, headers, body):
        calls.append((url, method, dict(headers), body))
        return status, {}, json.dumps(payload if payload is not None else {})

    send.calls = calls
    return send


# The shape of the surface ------------------------------------------------------------------


def test_a_queued_source_is_named_apart_from_a_synchronous_one():
    """An agent cannot block on an hour-long queue inside a tool call, and pretending the
    two shapes share an interface is the mistake that would make the surface unusable."""
    assert set(PENDING) == {"accepted", "running"}
    assert set(FINISHED) == {"successful", "failed", "dismissed"}


def test_a_job_carries_the_request_that_produced_it():
    """A queued retrieval is the kind whose provenance is easiest to lose, since the
    answer arrives an hour later, in another conversation, as a file."""
    job = Job(id="j1", service="cds", dataset="era5", request={"variable": ["runoff"]})
    record = job.record()
    assert record["job"] == "j1"
    assert record["request"] == {"variable": ["runoff"]}
    assert record["status"] == "accepted"


def test_a_pending_job_is_not_ready_and_says_how_long_it_has_waited():
    job = Job(id="j1", service="cds", dataset="era5", status="running")
    assert job.is_pending and not job.is_ready
    assert job.waited() >= 0


def test_a_result_asked_for_too_early_is_refused_with_the_status():
    job = Job(id="j1", service="cds", dataset="era5", status="running")
    with pytest.raises(JobNotReady, match="running"):
        Climate("key").download(job, "/tmp/nothing")


def test_a_failed_job_is_told_apart_from_an_unfinished_one():
    job = Job(id="j1", service="cds", dataset="era5", status="failed", message="bad area")
    with pytest.raises(JobFailed, match="bad area"):
        Climate("key").download(job, "/tmp/nothing")


# ERA5 ---------------------------------------------------------------------------------------


def test_submitting_without_an_account_is_refused_before_the_request_is_made():
    """So the failure names the account rather than arriving as a 401 from the archive."""
    send = sending()
    with pytest.raises(CredentialsMissing, match="requires an account"):
        Climate(None, send=send).submit("reanalysis-era5-land", {"variable": ["runoff"]})
    assert send.calls == []


def test_a_submission_posts_the_request_as_inputs():
    send = sending(payload={"jobID": "abc", "status": "accepted"})
    job, retrieval = Climate("key", send=send).submit(
        "reanalysis-era5-land", {"variable": ["total_precipitation"]}
    )

    url, method, headers, body = send.calls[0]
    assert method == "POST"
    assert url.endswith("/processes/reanalysis-era5-land/execution")
    assert json.loads(body) == {"inputs": {"variable": ["total_precipitation"]}}
    assert headers["PRIVATE-TOKEN"] == "key"
    assert job.id == "abc" and job.status == "accepted"
    assert retrieval.record()["collection"].startswith("cds/submit/")


def test_a_successful_job_without_a_result_link_reads_as_unfinished():
    """The safer of the two wrong answers: a caller polls again rather than downloading
    nothing and calling it data."""
    send = sending(payload={"status": "successful"})
    job = Job(id="abc", service="cds", dataset="era5")
    updated, _ = Climate("key", send=send).check(job)
    assert updated.status == "successful"
    assert not updated.is_ready


def test_a_result_link_makes_a_job_ready():
    send = sending(
        payload={"status": "successful", "links": [{"rel": "results", "href": "https://x/f.nc"}]}
    )
    updated, _ = Climate("key", send=send).check(Job(id="abc", service="cds", dataset="era5"))
    assert updated.is_ready and updated.href == "https://x/f.nc"


def test_rejected_credentials_are_told_apart_from_a_broken_archive():
    with pytest.raises(CredentialsMissing):
        Climate("wrong", send=sending(status=401)).check(
            Job(id="a", service="cds", dataset="e")
        )
    with pytest.raises(ServiceUnavailable):
        Climate("key", send=sending(status=503)).check(
            Job(id="a", service="cds", dataset="e")
        )


def test_the_store_publishes_precipitation_as_a_depth_in_metres():
    """Which reads as a plausible figure in millimetres and is a thousand times wrong.
    The responses state no unit, which is the whole reason this table exists."""
    assert UNITS[VARIABLES["precipitation"]] == "meter"
    assert UNITS[VARIABLES["temperature"]] == "kelvin"


# GRACE ---------------------------------------------------------------------------------------


def test_searching_needs_no_account_and_downloading_does():
    """The boundary is tested so that a change moving it fails here rather than mid
    question."""
    entry = {
        "id": "G1",
        "title": "GRCTellus",
        "time_start": "2002-04-16T00:00:00.000Z",
        "time_end": "2026-05-16T23:59:59.000Z",
        "granule_size": "43.5",
        "links": [
            {"href": "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-protected/a.nc"},
            {"href": "https://archive.podaac.earthdata.nasa.gov/podaac-ops-cumulus-public/a.nc.md5"},
        ],
    }
    granule = granule_from("TELLUS", entry)
    assert granule.needs_credentials
    with pytest.raises(CredentialsMissing, match="Searching needs no account"):
        Gravimetry(token=None).download(granule, "/tmp/nothing")


def test_the_protected_link_is_the_data_and_the_public_one_is_the_checksum():
    """Returning the public sibling as though it were the file would be the most
    misleading thing this module could do."""
    entry = {
        "id": "G1",
        "links": [
            {"href": "https://x/podaac-ops-cumulus-public/a.nc.md5"},
            {"href": "https://x/podaac-ops-cumulus-protected/a.nc"},
        ],
    }
    assert granule_from("T", entry).href.endswith("protected/a.nc")


def test_a_granule_with_no_link_is_refused_rather_than_downloaded():
    with pytest.raises(Exception):
        Gravimetry("token").download(Granule(id="G", title="t", collection="c"), "/tmp/x")


def test_water_storage_is_a_depth_and_not_a_volume():
    """Reading an equivalent water thickness as a volume is a plausible mistake and a
    large one, and the file header this package does not open is where the unit lives."""
    from gagelink.grace import UNITS as GRACE_UNITS

    assert GRACE_UNITS["lwe_thickness"] == "centimeter"


def test_an_empty_search_is_its_own_failure():
    from gagelink.grace import GranuleNotFound

    send = sending(payload={"feed": {"entry": []}})
    with pytest.raises(GranuleNotFound):
        Gravimetry(send=send).granules("NOTHING", "2024-01-01", "2024-02-01")
