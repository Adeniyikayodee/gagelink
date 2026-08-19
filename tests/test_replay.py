"""Re-running a session, and telling apart the three reasons an answer can change.

The bundle fixture stands in for a session run before August 2024, when USGS revised
discharge at station 02344872 for the period May to October 2021. The current daily page
and the revision record are both recorded from the live service, so the whole
decomposition is exercised without the network.
"""

import hashlib
import json
from pathlib import Path

import pytest

from gagelink import Service
from gagelink.replay import (
    OFFLINE,
    REVISION_AWARE,
    STRICT,
    CorruptArchive,
    load,
    replay,
)

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE = json.loads((FIXTURES / "bundle_pre_revision.json").read_text())
CURRENT = json.loads((FIXTURES / "daily_02344872_revised.json").read_text())
REVISIONS = json.loads((FIXTURES / "revisions_02344872.json").read_text())
NO_REVISIONS = {"type": "FeatureCollection", "features": []}


def bundle():
    return json.loads(json.dumps(BUNDLE))


def serving(daily=CURRENT, revisions=REVISIONS):
    def fetch(url, headers):
        page = revisions if "time-series-revisions" in url else daily
        return 200, {}, json.dumps(page)

    return fetch


def service(**kwargs):
    return Service(fetch=serving(**kwargs))


# Modes --------------------------------------------------------------------------------


def test_offline_recomputes_without_fetching_anything():
    """Nothing is fetched, so the data cannot have moved and any difference is the code."""
    def refuse(url, headers):
        raise AssertionError("offline replay must not reach the network")

    report = replay(bundle(), mode=OFFLINE, service=Service(fetch=refuse))
    assert report.verdict == "reproduced"


def test_offline_catches_a_quantity_the_code_no_longer_produces():
    """The signature of a change in the library rather than in the data."""
    changed = bundle()
    changed["manifest"]["quantities"] = changed["manifest"]["quantities"] + [
        {"tool": "get_series", "role": "output", "field": "series",
         "value": 999999.0, "unit": "ft ** 3 / s"}
    ]
    report = replay(changed, mode=OFFLINE)

    assert report.verdict == "code_changed"
    assert report.missing_quantities[0]["value"] == 999999.0
    assert "no longer produced" in report.report()


def test_strict_reports_any_drift_at_all():
    """Against a live service the honest expectation is that it usually fails, which is
    why it is a mode rather than the default."""
    report = replay(bundle(), mode=STRICT, service=service())
    assert report.verdict == "changed"
    assert not report.reproduced
    assert [c.verdict for c in report.changes] == ["changed"] * 5


def test_revision_aware_attributes_the_same_differences_to_the_agency():
    """The same bundle, the same re-fetch, and a different verdict, because the pipeline
    did the same thing to data the agency had since corrected."""
    report = replay(bundle(), mode=REVISION_AWARE, service=service())

    assert report.verdict == "reproduced"
    assert report.reproduced
    assert {c.verdict for c in report.changes} == {"revised"}
    assert "revised on Aug. 16, 2024" in report.changes[0].revision_note


def test_the_two_modes_disagree_on_the_same_bundle():
    """Which is the whole point: a result that changed because 400 provisional values
    were later approved is a different fact from one that changed because the code did."""
    assert replay(bundle(), mode=STRICT, service=service()).verdict == "changed"
    assert (
        replay(bundle(), mode=REVISION_AWARE, service=service()).verdict == "reproduced"
    )


def test_a_change_with_no_published_revision_stays_unexplained():
    """The check has to be able to fail, or attributing everything to revision would be
    vacuous."""
    report = replay(bundle(), mode=REVISION_AWARE, service=service(revisions=NO_REVISIONS))
    assert report.verdict == "changed"
    assert {c.verdict for c in report.changes} == {"changed"}


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="unknown mode"):
        replay(bundle(), mode="whatever")


# The differences themselves --------------------------------------------------------------


def test_a_difference_names_the_reading_and_both_values():
    change = replay(bundle(), mode=STRICT, service=service()).changes[0]
    assert change.location == "USGS-02344872"
    assert change.parameter == "00060"
    assert change.time == "2021-05-16"
    assert change.before == pytest.approx(702.1)
    assert change.after == pytest.approx(826.0)
    assert change.unit == "ft^3/s"


def test_a_daily_value_is_matched_against_a_revision_by_overlap_not_by_instant():
    """A daily value describes a day. Read as the instant of midnight it would fall
    outside a window that opened at 22:00 the same day, which would leave the first day of
    every revision unattributed."""
    revision = REVISIONS["features"][0]["properties"]
    assert revision["begin"].startswith("2021-05-16T22:00")

    changes = replay(bundle(), mode=REVISION_AWARE, service=service()).changes
    first = next(c for c in changes if c.time == "2021-05-16")
    assert first.verdict == "revised"


def test_a_withdrawn_reading_is_distinguished_from_a_changed_one():
    current = json.loads(json.dumps(CURRENT))
    current["features"][0]["properties"]["value"] = None
    report = replay(bundle(), mode=STRICT, service=service(daily=current))
    assert "withdrawn" in {c.verdict for c in report.changes}


def test_an_appeared_reading_is_distinguished_too():
    earlier = bundle()
    body = earlier["archive"].popitem()[1]
    page = json.loads(body)
    page["features"][0]["properties"]["value"] = None
    body = json.dumps(page)
    digest = hashlib.sha256(body.encode()).hexdigest()
    earlier["manifest"]["retrievals"][0]["sha256"] = digest
    earlier["archive"] = {digest: body}

    report = replay(earlier, mode=STRICT, service=service())
    assert "appeared" in {c.verdict for c in report.changes}


# Integrity ------------------------------------------------------------------------------


def test_an_archive_that_does_not_hash_to_its_keys_is_refused():
    """Every verdict rests on the archive being what the session saw, so a bundle failing
    this makes a difference unattributable rather than merely unexplained."""
    tampered = bundle()
    key = next(iter(tampered["archive"]))
    tampered["archive"][key] = tampered["archive"][key].replace("702.1", "999.9")
    assert tampered["archive"][key] != BUNDLE["archive"][key]

    with pytest.raises(CorruptArchive, match="cannot be replayed"):
        replay(tampered, mode=STRICT, service=service())


def test_a_service_that_will_not_answer_is_reported_rather_than_raised():
    def failing(url, headers):
        return 503, {}, "down"

    report = replay(bundle(), mode=STRICT, service=Service(fetch=failing))
    assert report.verdict == "incomplete"
    assert report.diffs[0].status == "unavailable"


def test_a_bundle_round_trips_through_a_file(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(BUNDLE))
    assert load(path)["manifest"]["retrievals"]


def test_the_other_services_are_not_re_fetched_and_the_report_says_why():
    """Only the USGS collections publish revisions, so a difference elsewhere could not be
    attributed, and pretending otherwise would put an unexplained change beside an
    explained one with nothing to separate them."""
    mixed = bundle()
    mixed["manifest"]["retrievals"].append(
        {"collection": "nwps/gauges/01646500", "url": "https://x", "params": {},
         "sha256": "0" * 64, "status": 200, "size": 1, "retrieved_at": "2026-01-01T00:00:00+00:00"}
    )
    report = replay(mixed, mode=REVISION_AWARE, service=service())
    assert any("not re-fetched" in note for note in report.notes)


def test_the_command_line_separates_a_finding_from_a_fault(tmp_path, capsys):
    """A replay that did not reproduce is a result, not an error, so it exits 1 rather
    than raising, and a CI job can act on it."""
    from gagelink.replay import main

    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(BUNDLE))
    assert main([str(path), "--mode", OFFLINE]) == 0
    assert "reproduced" in capsys.readouterr().out
