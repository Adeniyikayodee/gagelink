"""Freeboard at a levee, which is where the two hazards meet.

Runs offline from the recorded responses in tests/fixtures, so it needs no network and no
API key.

The question is how much room is left between the river and the top of a levee. Answering
it needs a stage from one service, a threshold or a survey from another, and the knowledge
that the two are measured from different places. The last of those is the part that is
never written down in the data, and it is the part that decides the answer.
"""

from __future__ import annotations

import json
from pathlib import Path

from quantity_guard import Q
from quantity_guard.errors import DatumMismatch

import gagelink as g

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
STATION = json.loads((FIXTURES / "monitoring_location_07374000.json").read_text())
GAUGE = json.loads((FIXTURES / "nwps_gauge_01646500.json").read_text())

#: A crest elevation from a survey, on a national datum, as such a figure always arrives.
CREST = Q(41.0, "ft", datum="NAVD88")


def recorded(payload):
    def fetch(url, headers):
        return 200, {}, json.dumps(payload)

    return fetch


def main() -> None:
    station = json.loads(json.dumps(STATION))
    # The recorded station is Baton Rouge; the forecast gauge is Little Falls. Renumbering
    # the site record makes the two describe one place, which is all the demo needs and is
    # cheaper than recording a second pair.
    station["features"][0]["properties"]["id"] = "USGS-01646500"
    station["features"][0]["properties"]["monitoring_location_number"] = "01646500"
    station["features"][0]["properties"]["altitude"] = 37.04

    question = (
        "A levee crest is surveyed at 41.0 ft NAVD88. How much freeboard is there now?"
    )
    with g.Session(
        service=g.Service(fetch=recorded(station)),
        forecasts=g.Forecasts(fetch=recorded(GAUGE)),
        question=question,
    ) as work:
        kit = g.Toolkit(work)
        kit.describe_location("USGS-01646500")
        forecast = kit.get_forecast("USGS-01646500")
        stage = work.gauges["USGS-01646500"].observed

        print(f"question   {question}")
        print(f"stage      {stage}")
        print(f"crest      {CREST}")
        print()

        print("The two are both lengths, so nothing dimensional separates them:")
        try:
            print(f"  crest - stage = {CREST - stage}")
        except DatumMismatch as exc:
            print(f"  refused: {str(exc).splitlines()[0]}")
        print()

        on_navd = stage.to_datum("NAVD88")
        freeboard = CREST - on_navd
        print(f"The gage's zero is at 37.04 ft NAVD88, so the stage is {on_navd}.")
        print(f"  freeboard = {freeboard}")
        print()

        naive = CREST.magnitude - stage.magnitude
        print(
            f"Ignoring the datum gives {naive:.2f} ft of margin where {freeboard.magnitude:.2f} ft "
            f"is correct, overstating it by a factor of {naive / freeboard.magnitude:.0f}."
        )
        print()

        # Computed here rather than inside a tool, so it is entered in the ledger
        # explicitly. Without this the audit reports it as unsourced, which is the
        # correct verdict on a number no tool returned.
        work.record_derived(freeboard, "freeboard below the surveyed crest")

        below = forecast.data.get("below_minor_flooding")
        print(f"Against the gage's own flood thresholds, which need no shift: {below}")
        print()

        audit = work.audit(
            f"There is {freeboard.magnitude:.2f} ft of freeboard, and the river is "
            f"{below.magnitude:.2f} ft below minor flooding."
        )
        print("answer audit:")
        print("  " + audit.report().replace("\n", "\n  "))


if __name__ == "__main__":
    main()
