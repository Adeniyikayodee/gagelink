"""Driving one task through one condition, and judging what came back.

The scoring rules are written here before any sweep is run, and they are in version
control, so a rule cannot be adjusted after seeing a result it disfavours. The
quantity-guard evaluation had to correct its scorer four times, and each correction was
made against transcripts already collected, which is the failure this arrangement is meant
to prevent.

A run is judged on the final answer alone. What the model did on the way there is recorded
but does not enter the verdict, because a user sees the answer.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import certifi
from quantity_guard import Q
from quantity_guard.errors import DimensionalityError, UnitParseError

from gagelink import Toolkit

from .conditions import HTTP_ONLY, render, schemas
from .offline import raw, session
from .tasks import Task

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = (
    "You are a hydrology analyst. Answer the question using the tools provided. "
    "Every number you report must come from a tool; do not answer from prior "
    "knowledge. When you have the answer, finish your reply with a single final "
    "line in exactly this form:\n"
    "ANSWER: <number> <unit>\n"
    "If the question asks for a quantity that no available tool can supply, or that "
    "the services do not publish, write:\n"
    "ANSWER: unavailable\n"
    "Do not write the ANSWER line until you have finished using tools."
)

_ANSWER = re.compile(r"ANSWER:\s*(?P<body>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_NUMBER = re.compile(r"^[-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?")

#: Spellings a model writes that pint does not read as the unit intended.
UNIT_SPELLINGS = {
    "cfs": "ft**3/s",
    "ft3/s": "ft**3/s",
    "ft^3/s": "ft**3/s",
    "cubic feet per second": "ft**3/s",
    "cubic_feet_per_second": "ft**3/s",
    "m3/s": "m**3/s",
    "cms": "m**3/s",
    "mm/day": "mm/day",
    "mm/d": "mm/day",
    "millimetres per day": "mm/day",
    "millimeters per day": "mm/day",
    "us/cm": "microsiemens/centimeter",
    "µs/cm": "microsiemens/centimeter",
    "microsiemens/cm": "microsiemens/centimeter",
    "us/cm@25c": "microsiemens/centimeter",
    "feet": "ft",
    "foot": "ft",
    "%": "percent",
    "pct": "percent",
}


@dataclass
class RunResult:
    task: str
    hazard: str
    condition: str
    model: str
    replicate: int
    outcome: str = "error"
    stated: str = ""
    hit_trap: bool = False
    unit_omitted: bool = False
    audit_flagged: bool | None = None
    tool_calls: int = 0
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    detail: str = ""
    answer_text: str = ""
    calls: list = field(default_factory=list)

    @property
    def correct(self) -> bool:
        return self.outcome == "correct"

    @property
    def silent(self) -> bool:
        """A wrong answer that nothing in the run marked as suspect.

        Only defined where an audit ran, which is the toolkit conditions. Elsewhere it is
        None, because a condition with no ledger cannot flag anything and counting that as
        silent would credit the absence of a check as a clean result.
        """
        return self.outcome == "wrong" and self.audit_flagged is False


class OpenRouter:
    """Minimal client. OpenRouter speaks the OpenAI shape, so the request is issued
    directly rather than through a vendor SDK."""

    def __init__(self, model: str, api_key: str | None = None, timeout: float = 180.0):
        self.model = model
        self.api_key = api_key or os.environ["OPENROUTER_API_KEY"]
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self.ssl_context
                ) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode()[:400]
                if exc.code in (429, 500, 502, 503, 529):
                    last = RuntimeError(f"{exc.code}: {detail}")
                    time.sleep(2**attempt + 1)
                    continue
                raise RuntimeError(f"{exc.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                # ConnectionResetError arrives as a bare OSError rather than through
                # URLError, so it was ending runs that a retry would have completed. The
                # losses fell on whichever condition happened to be in flight, which
                # biases a denominator rather than merely shrinking it.
                last = exc
                time.sleep(2**attempt + 1)
        raise RuntimeError(f"request failed after retries: {last}")


def parse_answer(text: str, expected: Q | None) -> tuple[str, Q | None, bool]:
    """The stated answer, as a refusal, a quantity, or neither.

    Returns the raw statement, the quantity it parses to, and whether a unit was omitted.
    A bare number is read in the unit the question asked for, which is the charitable
    reading, and the omission is recorded separately so it can be counted rather than
    silently forgiven.
    """
    match = _ANSWER.search(text or "")
    if not match:
        return "", None, False
    body = match.group("body").strip().rstrip(".")

    if re.search(r"\bunavailable\b|\bunknown\b|\bnot published\b", body, re.IGNORECASE):
        return body, None, False

    number = _NUMBER.match(body)
    if not number:
        return body, None, False
    magnitude = float(number.group().replace(",", ""))
    written = body[number.end():].strip()

    if not written:
        if expected is None:
            return body, None, True
        return body, Q(magnitude, expected.units), True

    spelling = UNIT_SPELLINGS.get(written.lower(), written)
    try:
        return body, Q.parse(f"{magnitude} {spelling}"), False
    except (UnitParseError, Exception):
        if expected is not None:
            return body, Q(magnitude, expected.units), True
        return body, None, True


def score(task: Task, text: str) -> tuple[str, str, bool, bool]:
    """Judge one answer: the outcome, what was stated, whether it hit the trap."""
    stated, value, unit_omitted = parse_answer(text, task.answer)

    if not stated:
        return "error", "", False, unit_omitted

    refused = value is None and re.search(
        r"\bunavailable\b|\bunknown\b|\bnot published\b", stated, re.IGNORECASE
    )

    if task.expects_refusal:
        if refused:
            return "correct", stated, False, unit_omitted
        hit = _matches(value, task.trap, task.tolerance)
        return "wrong", stated, hit, unit_omitted

    if refused:
        # A value existed and the model declined to give it, which is a failure of a
        # different kind from a wrong number and is counted apart from it.
        return "refused", stated, False, unit_omitted
    if value is None:
        return "error", stated, False, unit_omitted

    if _matches(value, task.answer, task.tolerance):
        return "correct", stated, False, unit_omitted
    return "wrong", stated, _matches(value, task.trap, task.tolerance), unit_omitted


def _matches(value: Q | None, target: Q | None, tolerance: float) -> bool:
    if value is None or target is None:
        return False
    try:
        converted = value.to(target.units).magnitude
    except (DimensionalityError, Exception):
        return False
    return abs(converted - target.magnitude) <= tolerance * max(
        abs(target.magnitude), 1e-9
    )


def run_one(
    task: Task,
    condition: str,
    client: OpenRouter,
    replicate: int = 0,
    max_turns: int = 10,
) -> RunResult:
    """Drive one task under one condition to a final answer."""
    result = RunResult(
        task=task.name,
        hazard=task.hazard,
        condition=condition,
        model=client.model,
        replicate=replicate,
    )
    tools = schemas(condition)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task.prompt},
    ]

    with session(task.prompt) as work:
        kit = Toolkit(work)
        try:
            for _ in range(max_turns):
                response = client.complete(messages, tools)
                usage = response.get("usage") or {}
                result.prompt_tokens += usage.get("prompt_tokens", 0)
                result.completion_tokens += usage.get("completion_tokens", 0)
                result.turns += 1

                message = response["choices"][0]["message"]
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        **(
                            {"tool_calls": message["tool_calls"]}
                            if message.get("tool_calls")
                            else {}
                        ),
                    }
                )

                calls = message.get("tool_calls") or []
                if not calls:
                    # Some models return their final text in `reasoning` and leave
                    # `content` empty, which scores as an unparseable answer and would be
                    # recorded as a failure of the task rather than of the transport.
                    result.answer_text = (
                        message.get("content")
                        or message.get("reasoning")
                        or ""
                    )
                    break

                for call in calls:
                    result.tool_calls += 1
                    name = call["function"]["name"]
                    try:
                        arguments = json.loads(call["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    result.calls.append({"tool": name, "arguments": arguments})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": _invoke(kit, condition, name, arguments),
                        }
                    )
        except Exception as exc:  # a provider or protocol failure, not a task outcome
            result.outcome = "error"
            result.detail = str(exc)[:300]
            return result

        outcome, stated, hit, omitted = score(task, result.answer_text)
        result.outcome = outcome
        result.stated = stated
        result.hit_trap = hit
        result.unit_omitted = omitted

        if condition != HTTP_ONLY and result.answer_text:
            audit = work.audit(result.answer_text)
            result.audit_flagged = not audit.ok

    return result


def _invoke(kit: Toolkit, condition: str, name: str, arguments: dict) -> str:
    """Run one tool call and return what the model should see under this condition."""
    if condition == HTTP_ONLY:
        if name != "http_get":
            return json.dumps({"error": f"no tool named {name}"})
        return raw(str(arguments.get("path", "")))

    if name == "export_manifest":
        return json.dumps(kit.session.manifest(), indent=1)[:4000]

    method = getattr(kit, name, None)
    if method is None:
        return json.dumps({"error": f"no tool named {name}"})
    try:
        return render(condition, method(**arguments).to_dict(kit.budget_tokens))
    except TypeError as exc:
        return json.dumps({"error": f"{name}: {exc}"})
