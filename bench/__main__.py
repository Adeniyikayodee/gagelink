"""Run the suite.

    python -m bench --model anthropic/claude-opus-5 --replicates 4
    python -m bench --dry-run

The dry run drives the whole harness with a scripted client that calls every tool and
states the right answer, so the plumbing can be exercised without a provider, a key, or
any spend. It is what CI runs.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from pathlib import Path

from .conditions import CONDITIONS
from .runner import OpenRouter, RunResult, run_one
from .tasks import TASKS, BY_NAME


class Scripted:
    """A client that answers each task correctly after one tool call.

    Not a model and not a baseline. It exists so the harness can be run end to end
    without a provider, and so a change that breaks the plumbing fails in CI rather than
    halfway through a paid sweep.
    """

    model = "scripted"

    def complete(self, messages, tools):
        # Decided from the conversation rather than from a counter, so several runs in
        # flight at once cannot interleave into each other's state.
        question = next(m["content"] for m in messages if m["role"] == "user")
        task = next(t for t in TASKS if t.prompt == question)
        called = any(m["role"] == "tool" for m in messages)

        if not called and tools:
            name = tools[0]["function"]["name"]
            arguments = (
                {"path": "usgs/latest-continuous/items?monitoring_location_id=USGS-01646500"}
                if name == "http_get"
                else {"identifier": "USGS-01646500"}
            )
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {},
            }

        answer = (
            "unavailable"
            if task.expects_refusal
            else f"{task.answer.magnitude:g} {task.answer.units:~}"
        )
        return {
            "choices": [{"message": {"content": f"ANSWER: {answer}"}}],
            "usage": {},
        }


def summarise(results: list[RunResult]) -> str:
    """Outcomes by condition, then by hazard, which is where the differences live."""
    lines = []
    by_condition: dict[str, list[RunResult]] = defaultdict(list)
    for result in results:
        by_condition[result.condition].append(result)

    lines.append(f"{'condition':<16}{'correct':>9}{'wrong':>7}{'refused':>9}"
                 f"{'error':>7}{'trap':>6}{'silent':>8}")
    for condition in CONDITIONS:
        runs = by_condition.get(condition) or []
        if not runs:
            continue
        counted = defaultdict(int)
        for run in runs:
            counted[run.outcome] += 1
        lines.append(
            f"{condition:<16}{counted['correct']:>9}{counted['wrong']:>7}"
            f"{counted['refused']:>9}{counted['error']:>7}"
            f"{sum(r.hit_trap for r in runs):>6}{sum(bool(r.silent) for r in runs):>8}"
        )

    lines.append("")
    lines.append(f"{'hazard':<18}" + "".join(f"{c:>16}" for c in CONDITIONS))
    for hazard in sorted({t.hazard for t in TASKS}):
        row = f"{hazard:<18}"
        for condition in CONDITIONS:
            runs = [r for r in results if r.hazard == hazard and r.condition == condition]
            usable = [r for r in runs if r.outcome != "error"]
            row += (
                f"{sum(r.correct for r in usable)}/{len(usable)}".rjust(16)
                if usable
                else "-".rjust(16)
            )
        lines.append(row)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description="Run waterbench.")
    parser.add_argument("--model", default="anthropic/claude-opus-5")
    parser.add_argument("--replicates", type=int, default=4)
    parser.add_argument("--conditions", nargs="*", default=list(CONDITIONS))
    parser.add_argument("--tasks", nargs="*", default=None)
    parser.add_argument("--out", default="bench/results.jsonl")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "runs in flight at once. The tasks share no state, so this only trades "
            "against the provider's rate limit"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the harness with a scripted client, spending nothing",
    )
    args = parser.parse_args(argv)

    tasks = [BY_NAME[name] for name in args.tasks] if args.tasks else TASKS
    client = Scripted() if args.dry_run else OpenRouter(args.model)
    replicates = 1 if args.dry_run else args.replicates
    plan = [
        (task, condition, replicate)
        for task in tasks
        for condition in args.conditions
        for replicate in range(replicates)
    ]

    path = Path(args.out)
    if not args.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    results: list[RunResult] = []

    def execute(job):
        task, condition, replicate = job
        result = run_one(task, condition, client, replicate)
        with lock:
            results.append(result)
            # Written as it completes rather than at the end. A sweep of this length that
            # dies at ninety percent would otherwise leave nothing, and a partial sweep
            # looks like a completed one in the summary tables.
            if not args.dry_run:
                with path.open("a") as handle:
                    handle.write(json.dumps(result.__dict__, default=str) + "\n")
            print(
                f"{len(results):>4}/{len(plan)} {task.name:<20} {condition:<15} "
                f"{result.outcome:<9} {result.stated[:32]}",
                file=sys.stderr,
                flush=True,
            )
        return result

    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(execute, plan))
    else:
        for job in plan:
            execute(job)

    if not args.dry_run:
        print(f"\nwrote {len(results)} rows to {path}", file=sys.stderr)

    print(summarise(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
