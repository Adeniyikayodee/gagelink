"""Reading a sweep.

Separate from the runner so a result can be re-read without re-running, and so a change
to how a table is drawn cannot be mistaken for a change in what was measured. The runner
writes rows; nothing here writes anything.

    python -m bench.analyse bench/results_8x.jsonl
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .conditions import CONDITIONS
from .tasks import BY_NAME


def load(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _rate(rows: Iterable[dict], predicate) -> str:
    rows = list(rows)
    if not rows:
        return "-"
    hits = sum(1 for r in rows if predicate(r))
    return f"{hits}/{len(rows)}"


def outcomes(rows: list[dict]) -> str:
    """Counts by condition, over runs that produced an answer at all."""
    lines = [
        f"{'condition':<16}{'correct':>10}{'wrong':>8}{'refused':>9}{'error':>7}"
        f"{'trap':>7}{'silent':>8}{'no unit':>9}"
    ]
    for condition in CONDITIONS:
        runs = [r for r in rows if r["condition"] == condition]
        if not runs:
            continue
        counted = defaultdict(int)
        for run in runs:
            counted[run["outcome"]] += 1
        # Counted over every run rather than over those that produced an answer. A run
        # that ended without one is a run the condition did not answer, and on this suite
        # the failures are caused by the condition: seven of the eight belong to
        # http_only on the two tasks whose raw record runs to 42,000 tokens. Excluding
        # them would credit a condition for the runs its own payload size destroyed.
        share = f"{counted['correct']}/{len(runs)}"
        lines.append(
            f"{condition:<16}{share:>10}{counted['wrong']:>8}{counted['refused']:>9}"
            f"{counted['error']:>7}{sum(r['hit_trap'] for r in runs):>7}"
            f"{sum(1 for r in runs if r['outcome'] == 'wrong' and r.get('audit_flagged') is False):>8}"
            f"{sum(r['unit_omitted'] for r in runs):>9}"
        )
    return "\n".join(lines)


def by_task(rows: list[dict]) -> str:
    """Correct answers per task, which is where a pooled figure hides the differences."""
    lines = [f"{'task':<20}{'hazard':<16}" + "".join(f"{c:>15}" for c in CONDITIONS)]
    for name, task in BY_NAME.items():
        row = f"{name:<20}{task.hazard:<16}"
        for condition in CONDITIONS:
            runs = [
                r for r in rows if r["task"] == name and r["condition"] == condition
            ]
            row += _rate(runs, lambda r: r["outcome"] == "correct").rjust(15)
        lines.append(row)
    return "\n".join(lines)


def traps(rows: list[dict]) -> str:
    """Where a wrong answer was the specific wrong answer the hazard produces.

    A wrong answer that hit the recorded trap is attributable; one that did not is only
    wrong, and the two should not be pooled.
    """
    lines = [f"{'task':<20}" + "".join(f"{c:>15}" for c in CONDITIONS)]
    for name, task in BY_NAME.items():
        if task.trap is None:
            continue
        row = f"{name:<20}"
        for condition in CONDITIONS:
            runs = [r for r in rows if r["task"] == name and r["condition"] == condition]
            row += _rate(runs, lambda r: r["hit_trap"]).rjust(15)
        lines.append(row)
    return "\n".join(lines)


def cost(rows: list[dict]) -> str:
    """What each condition cost to run, which is the least noisy thing measured here."""
    lines = [f"{'condition':<16}{'median prompt':>15}{'max prompt':>13}{'median turns':>14}{'calls':>8}"]
    for condition in CONDITIONS:
        runs = [r for r in rows if r["condition"] == condition and r["turns"]]
        if not runs:
            continue
        prompts = [r["prompt_tokens"] for r in runs]
        lines.append(
            f"{condition:<16}{statistics.median(prompts):>15,.0f}{max(prompts):>13,}"
            f"{statistics.median(r['turns'] for r in runs):>14.0f}"
            f"{statistics.median(r['tool_calls'] for r in runs):>8.0f}"
        )
    return "\n".join(lines)


def stability(rows: list[dict]) -> str:
    """How often replicates of one cell disagreed.

    A cell whose replicates all agree is measuring something; one that splits is measuring
    the sampler. Reported because the first sweep at one replicate moved five of 27 cells
    between two runs of the same thing.
    """
    split = 0
    total = 0
    for name in BY_NAME:
        for condition in CONDITIONS:
            runs = [r for r in rows if r["task"] == name and r["condition"] == condition]
            if len(runs) < 2:
                continue
            total += 1
            if len({r["outcome"] for r in runs}) > 1:
                split += 1
    return f"{split} of {total} cells split across their replicates"


def report(rows: list[dict]) -> str:
    models = sorted({r["model"] for r in rows})
    replicates = max((r["replicate"] for r in rows), default=0) + 1
    prompt = sum(r["prompt_tokens"] for r in rows)
    completion = sum(r["completion_tokens"] for r in rows)
    return "\n\n".join(
        [
            f"{len(rows)} runs, {replicates} replicates, models: {', '.join(models)}",
            outcomes(rows),
            by_task(rows),
            "hit the recorded trap:\n" + traps(rows),
            cost(rows),
            stability(rows),
            f"{prompt:,} prompt and {completion:,} completion tokens",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    print(report(load(argv[0] if argv else "bench/results.jsonl")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
