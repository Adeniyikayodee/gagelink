"""What the benchmark asks, and what the right answer is.

Each task is a question a practitioner would ask, set at one station, with an answer that
can be computed from the same recorded responses the tools serve. Ground truth is derived
here from those responses rather than written down beside them, so the two cannot drift
apart when a fixture is refreshed.

Every hazard in the suite was observed in live service payloads while the package was
built. None is hypothetical, and none was constructed to make a point: the gage datum
offset, the two flow units in one response, the -9999 threshold, and the series of mixed
age are all in the recorded bytes.

Provenance of each expected answer is recorded in `basis`, which distinguishes a figure
the agency publishes from one computed from published figures. Nothing here rests on a
number that only this project asserts.
"""

from __future__ import annotations

from dataclasses import dataclass

from quantity_guard import Q

from .offline import STATION

#: A surveyed levee crest, which is the one figure in the suite that no service publishes.
#: It is an input to the question rather than an answer, and is given to the model in the
#: prompt, so no expected value depends on it being right.
CREST = Q(45.0, "ft", datum="NAVD88")


@dataclass(frozen=True)
class Task:
    name: str
    hazard: str
    prompt: str
    answer: Q | None
    #: Where the expected answer comes from, so a reader can check it independently.
    basis: str
    tolerance: float = 0.01
    #: Set when the correct response is to report the figure as unavailable.
    expects_refusal: bool = False
    #: The answer a model reaches by mishandling the hazard. Recorded so a wrong answer
    #: can be attributed to the hazard rather than merely counted as wrong.
    trap: Q | None = None


TASKS: list[Task] = [
    Task(
        name="freeboard",
        hazard="datum",
        prompt=(
            f"A levee crest beside USGS station {STATION} is surveyed at 45.0 ft on the "
            f"NAVD88 datum. Using the current river level, how much freeboard is there "
            f"between the water surface and the crest? Answer in feet."
        ),
        answer=Q(4.93, "ft"),
        basis=(
            "45.0 ft NAVD88 less the water surface elevation USGS publishes as parameter "
            "63160, 40.07 ft NAVD88. That elevation equals the gage height of 3.03 ft "
            "plus the station's datum offset of 37.04 ft, so the arithmetic is checkable "
            "against the agency's own figure."
        ),
        trap=Q(41.97, "ft"),
    ),
    Task(
        name="flood_margin",
        hazard="datum_control",
        prompt=(
            f"How far below minor flood stage is USGS station {STATION} at the moment? "
            f"Answer in feet."
        ),
        answer=Q(6.98, "ft"),
        basis=(
            "The NWS minor flood stage of 10 ft less the observed stage of 3.02 ft. Both "
            "are on the gage's own datum, so no conversion applies. This is the control "
            "for the freeboard task: applying the datum offset here would be the error."
        ),
        tolerance=0.02,
        trap=Q(-30.06, "ft"),
    ),
    Task(
        name="runoff_depth",
        hazard="unit_carryover",
        prompt=(
            f"Express the current discharge at USGS station {STATION} as a depth of "
            f"runoff over the station's drainage area, in millimetres per day."
        ),
        answer=Q(0.2460, "mm/day"),
        basis=(
            "The published discharge of 3010 ft^3/s over the published drainage area of "
            "11560 mi^2. The hazard is that neither figure is in SI and the drainage area "
            "carries no unit in the payload at all."
        ),
        tolerance=0.02,
    ),
    Task(
        name="forecast_flow",
        hazard="opaque_unit",
        prompt=(
            f"The National Water Prediction Service publishes an observed flow for the "
            f"gauge at USGS station {STATION}. What is that flow in cubic feet per second?"
        ),
        answer=Q(2950, "ft**3/s"),
        basis=(
            "The observed secondary value of 2.95, published in kcfs in the same response "
            "whose flood thresholds are published in cfs. The two differ by a factor of a "
            "thousand and neither abbreviation states its own composition."
        ),
        trap=Q(2.95, "ft**3/s"),
    ),
    Task(
        name="major_flood_flow",
        hazard="sentinel",
        prompt=(
            f"What discharge corresponds to major flood stage at USGS station {STATION}? "
            f"Answer in cubic feet per second."
        ),
        answer=None,
        expects_refusal=True,
        basis=(
            "The service publishes -9999 for this threshold, which means it was never "
            "set. The correct answer is that it is unavailable. The action threshold, at "
            "22000 cfs, is published and is the nearest figure that exists."
        ),
        trap=Q(-9999, "ft**3/s"),
    ),
    Task(
        name="current_conductance",
        hazard="staleness",
        prompt=(
            f"What is the current specific conductance at USGS station {STATION}? Answer "
            f"in microsiemens per centimetre."
        ),
        answer=Q(340, "microsiemens/centimeter"),
        basis=(
            "The station publishes three specific conductance series, one current and two "
            "that stopped in 2019. The current value is 340; the stale ones read 406 and "
            "322 and are equally present in the response."
        ),
        trap=Q(406, "microsiemens/centimeter"),
    ),
    Task(
        name="record_peak",
        hazard="retrieval",
        prompt=(
            f"What is the largest discharge ever recorded at USGS station {STATION}? "
            f"Answer in cubic feet per second."
        ),
        answer=Q(484000, "ft**3/s"),
        basis="The largest of the 190 annual peaks on record, from March 1936.",
    ),
    Task(
        name="peak_fraction",
        hazard="composition",
        prompt=(
            f"What percentage of the record peak discharge is the current discharge at "
            f"USGS station {STATION}?"
        ),
        answer=Q(0.6219, "percent"),
        basis="The current 3010 ft^3/s against the record peak of 484000 ft^3/s.",
        tolerance=0.02,
    ),
    Task(
        name="hundred_year_flood",
        hazard="fabrication",
        prompt=(
            f"What is the 100-year flood discharge at USGS station {STATION}? Answer in "
            f"cubic feet per second."
        ),
        answer=None,
        expects_refusal=True,
        basis=(
            "No tool in the suite computes a flood frequency estimate, and none of the "
            "services queried publishes one. A figure here can only have come from "
            "memory. The station's record peak of 484000 ft^3/s is the largest observed "
            "value and is not a 100-year estimate."
        ),
    ),
]

BY_NAME = {task.name: task for task in TASKS}
HAZARDS = sorted({task.hazard for task in TASKS})
