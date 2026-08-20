"""That every benchmark answer is reachable from the data the benchmark serves.

A suite whose expected answers were written down beside the tasks rather than derived from
them is a suite that measures whoever wrote them. Each task here is solved from the same
recorded responses the model will be given, through the same tools, and the result is
checked against the declared answer. A task that cannot be solved this way is a broken
task, and this is where that shows up.
"""

import pytest
from quantity_guard import Q

from bench.offline import STATION, session
from bench.tasks import BY_NAME, CREST, TASKS
from gagelink import Toolkit


@pytest.fixture
def kit():
    with session("benchmark ground truth") as work:
        yield Toolkit(work)


def latest(kit, code):
    """The current reading for a parameter, which is the newest of its series."""
    readings = [
        r
        for r in kit.get_latest(STATION).data["readings"]
        if r["parameter_code"] == code and r["value"] is not None
    ]
    return max(readings, key=lambda r: r["time"])["value"]


def check(name, computed):
    task = BY_NAME[name]
    expected = task.answer
    assert expected is not None, f"{name} expects a refusal, not a value"
    assert computed.to(expected.units).magnitude == pytest.approx(
        expected.magnitude, rel=task.tolerance
    ), f"{name}: {task.basis}"


def test_every_task_is_distinct_in_name_and_hazard():
    assert len({t.name for t in TASKS}) == len(TASKS)
    assert len({t.hazard for t in TASKS}) == len(TASKS)


def test_every_task_records_where_its_answer_comes_from():
    """So a reader can check a figure without taking this project's word for it."""
    for task in TASKS:
        assert len(task.basis) > 40, task.name


def test_freeboard(kit):
    """The datum arithmetic is checkable against the agency's own published elevation:
    63160 is the water surface on NAVD88 and equals the gage height plus the offset."""
    surface = latest(kit, "63160")
    stage = latest(kit, "00065")
    offset = kit.describe_location(STATION).data["altitude_of_gage_datum"]

    assert surface.magnitude == pytest.approx(
        stage.to_datum("NAVD88").magnitude, abs=0.01
    )
    assert surface.magnitude == pytest.approx(stage.magnitude + offset.magnitude, abs=0.01)
    check("freeboard", CREST - surface)


def test_the_freeboard_trap_is_what_ignoring_the_datum_gives(kit):
    """Recorded so a wrong answer can be attributed to the hazard rather than counted as
    merely wrong."""
    stage = latest(kit, "00065")
    naive = CREST.magnitude - stage.magnitude
    assert naive == pytest.approx(BY_NAME["freeboard"].trap.magnitude, abs=0.01)


def test_flood_margin(kit):
    """The control: both figures are on the gage datum, so converting would be the error."""
    forecast = kit.get_forecast(STATION).data
    margin = forecast["thresholds"]["minor"]["stage"] - forecast["observed"]["stage"]
    assert margin.datum is None
    check("flood_margin", margin)


def test_runoff_depth(kit):
    discharge = latest(kit, "00060")
    area = kit.describe_location(STATION).data["drainage_area"]
    check("runoff_depth", discharge / area)


def test_forecast_flow(kit):
    flow = kit.get_forecast(STATION).data["observed"]["flow"]
    assert f"{flow.units:~}" == "kcfs"
    check("forecast_flow", flow)


def test_current_conductance(kit):
    """Three series, one current and two that stopped in 2019, all in one response."""
    series = [
        r
        for r in kit.get_latest(STATION).data["readings"]
        if r["parameter_code"] == "00095" and r["value"] is not None
    ]
    assert len(series) == 3
    check("current_conductance", latest(kit, "00095"))


def test_the_staleness_trap_is_present_in_the_same_response(kit):
    values = {
        r["value"].magnitude
        for r in kit.get_latest(STATION).data["readings"]
        if r["parameter_code"] == "00095" and r["value"] is not None
    }
    assert BY_NAME["current_conductance"].trap.magnitude in values


def test_record_peak(kit):
    peaks = kit.get_peaks(STATION, limit=1).data
    assert peaks["peaks_in_record"] == 190
    check("record_peak", peaks["peaks"][0]["value"])


def test_peak_fraction(kit):
    current = latest(kit, "00060")
    peak = kit.get_peaks(STATION, limit=1).data["peaks"][0]["value"]
    check("peak_fraction", Q(current.magnitude / peak.magnitude * 100, "percent"))


def test_the_sentinel_threshold_is_absent_rather_than_negative(kit):
    """The correct answer is that it is unavailable, and the trap is -9999 read as a
    discharge."""
    thresholds = kit.get_forecast(STATION).data["thresholds"]
    assert "flow" not in thresholds["major"] or thresholds["major"].get("flow") is None
    assert thresholds["action"]["flow"] == Q(22000, "cfs")
    assert BY_NAME["major_flood_flow"].expects_refusal


def test_no_tool_supplies_a_flood_frequency_estimate(kit):
    """Which is what makes a figure for it necessarily fabricated."""
    assert BY_NAME["hundred_year_flood"].expects_refusal
    names = {t["name"] for t in __import__("gagelink").server.TOOLS}
    assert not any("frequency" in n or "recurrence" in n for n in names)


def test_the_harness_runs_end_to_end_without_a_provider(capsys):
    """So a change that breaks the plumbing fails here rather than halfway through a paid
    sweep, which is how the previous benchmark lost three partial sweeps."""
    from bench.__main__ import main

    assert main(["--dry-run", "--tasks", "freeboard", "flood_margin"]) == 0
    printed = capsys.readouterr().out
    assert "http_only" in printed and "toolkit" in printed


def test_the_conditions_differ_only_in_the_interface():
    """The data is identical throughout, so a difference between conditions cannot come
    from one condition having been given better data."""
    from bench.conditions import HTTP_ONLY, TOOLKIT, TOOLKIT_PLAIN, plain, schemas

    assert len(schemas(HTTP_ONLY)) == 1
    assert len(schemas(TOOLKIT)) == len(schemas(TOOLKIT_PLAIN)) == 11

    framed = {"value": 3.03, "unit": "ft", "datum": "GAGE:01646500", "quality": "provisional"}
    assert plain(framed) == 3.03
    assert plain({"notes": ["x"], "data": framed}) == {"data": 3.03}


def test_scoring_reads_the_answer_rather_than_the_working():
    from bench.runner import score

    assert score(BY_NAME["freeboard"], "ANSWER: 4.93 ft")[0] == "correct"
    assert score(BY_NAME["freeboard"], "ANSWER: 41.97 ft")[:3] == ("wrong", "41.97 ft", True)
    assert score(BY_NAME["major_flood_flow"], "ANSWER: unavailable")[0] == "correct"
    assert score(BY_NAME["major_flood_flow"], "ANSWER: -9999 cfs")[2] is True
    assert score(BY_NAME["record_peak"], "ANSWER: unavailable")[0] == "refused"


def test_an_answer_in_another_unit_of_the_right_dimension_is_correct():
    """The question is whether the model reached the quantity, not whether it formatted
    it as asked. The trap catches the failure that matters, which is dropping the k."""
    from bench.runner import score

    assert score(BY_NAME["forecast_flow"], "ANSWER: 2.95 kcfs")[0] == "correct"
    assert score(BY_NAME["forecast_flow"], "ANSWER: 2.95 cfs")[:1] == ("wrong",)


def test_the_analysis_reads_a_sweep_without_rerunning_it(tmp_path):
    """Kept apart from the runner so a change to how a table is drawn cannot be mistaken
    for a change in what was measured."""
    import json as _json

    from bench.analyse import load, report, stability

    rows = [
        {"task": "freeboard", "hazard": "datum", "condition": c, "model": "m",
         "replicate": r, "outcome": o, "stated": "", "hit_trap": False,
         "unit_omitted": False, "audit_flagged": None, "tool_calls": 1, "turns": 2,
         "prompt_tokens": 1000, "completion_tokens": 10, "detail": "", "answer_text": "",
         "calls": []}
        for c in ("http_only", "toolkit")
        for r, o in enumerate(["correct", "wrong"])
    ]
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(_json.dumps(row) for row in rows))

    assert len(load(path)) == 4
    printed = report(load(path))
    assert "http_only" in printed and "toolkit" in printed
    assert "2 of 2 cells split" in stability(load(path))


def test_a_cell_whose_replicates_agree_is_not_counted_as_split():
    from bench.analyse import stability

    agreed = [
        {"task": "freeboard", "condition": "toolkit", "outcome": "correct"}
        for _ in range(4)
    ]
    assert "0 of 1 cells split" in stability(agreed)
