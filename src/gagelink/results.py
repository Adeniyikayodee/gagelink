"""What a tool hands back to a model.

Three properties separate a result meant for a model from one meant for a program, and
each of them costs something if it is got wrong.

Size is a cost. A year of 15-minute record is 35,000 points and no answer needs them in
context, so results are budgeted and a series is summarised rather than returned. Where
something is dropped the result says so, because a silent truncation reads as coverage.

Failure is a normal return value. A raised exception ends the turn, whereas a failure
carrying a repair keeps the model in the conversation where it can correct itself, which is
what the guarded_repair condition in the quantity-guard benchmark measured.

Quantities keep their metadata on the way out. A number rendered as `8.09` has lost the
frame that made it meaningful, so every quantity is serialised with its unit, datum, and
grade beside it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quantity_guard import Q

#: Rough characters per token. Used only to keep a result inside a budget, where being
#: approximately right is sufficient and being exactly right would need a tokeniser per
#: model family.
CHARS_PER_TOKEN = 4

#: Default ceiling on one tool result. Twelve of these fit in a small context window
#: alongside a conversation, which is the working assumption.
DEFAULT_BUDGET_TOKENS = 1500


class ErrorCode:
    """The failures a caller has to be able to tell apart.

    Each names a different repair, which is the test for whether a code earns its place:
    two failures fixed the same way do not need two codes.
    """

    NO_DATA = "NO_DATA"
    LOCATION_UNKNOWN = "LOCATION_UNKNOWN"
    LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"
    PARAMETER_NOT_MEASURED = "PARAMETER_NOT_MEASURED"
    RECORD_STALE = "RECORD_STALE"
    DATUM_UNKNOWN = "DATUM_UNKNOWN"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    UNKNOWN_HANDLE = "UNKNOWN_HANDLE"


def unit_text(units: Any) -> str:
    """A unit written the way the service writes it.

    pint's short form spaces its operators, giving `ft ** 3 / s`, and a model reading that
    has to reassemble it before it can match it against anything. Closing the spaces gives
    `ft^3/s`, which is the spelling the service itself publishes.
    """
    return f"{units:~}".replace(" ** ", "^").replace(" / ", "/").replace(" * ", "*")


def render(value: Any) -> Any:
    """A value in the form a model should see it.

    A quantity becomes an object rather than a string, so that the unit is a field the
    model can read and act on rather than prose it has to parse back out.
    """
    if isinstance(value, Q):
        out: dict[str, Any] = {"value": value.magnitude, "unit": unit_text(value.units)}
        if value.datum:
            out["datum"] = value.datum
        if value.quality:
            out["quality"] = value.quality
        if value.crs:
            out["crs"] = value.crs
        return out
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: render(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [render(v) for v in value]
    return value


def size_in_tokens(payload: Any) -> int:
    """An estimate of what a payload costs to put in a context window."""
    return len(json.dumps(payload, default=str)) // CHARS_PER_TOKEN


@dataclass
class Result:
    """One tool's answer, successful or not."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    message: str | None = None
    repair: str | None = None
    quota_remaining: int | None = None

    @classmethod
    def failure(
        cls, code: str, message: str, repair: str, **data: Any
    ) -> "Result":
        return cls(ok=False, error=code, message=message, repair=repair, data=data)

    def note(self, text: str) -> "Result":
        """Record something the caller should know but that did not stop the call."""
        self.notes.append(text)
        return self

    def to_dict(self, budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> dict[str, Any]:
        """The payload, trimmed to fit, with any trimming stated."""
        body: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            body["data"] = render(self.data)
        else:
            body["error"] = self.error
            body["message"] = self.message
            body["repair"] = self.repair
            if self.data:
                body["data"] = render(self.data)
        if self.quota_remaining is not None:
            body["requests_remaining_this_hour"] = self.quota_remaining
        if self.notes:
            body["notes"] = list(self.notes)
        return fit(body, budget_tokens)


def fit(body: dict[str, Any], budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> dict[str, Any]:
    """Trim a payload to a token budget, saying what was dropped.

    Trimming takes from the longest list first, since that is where the bulk is, and never
    silently: a result that dropped 900 of 1,000 items and does not say so is worse than
    one that failed, because it reads as complete.
    """
    if size_in_tokens(body) <= budget_tokens:
        return body

    trimmed = json.loads(json.dumps(body, default=str))
    dropped: dict[str, int] = {}
    while size_in_tokens(trimmed) > budget_tokens:
        target = _longest_list(trimmed)
        if target is None:
            break
        path, items = target
        if len(items) <= 1:
            break
        keep = max(1, len(items) // 2)
        dropped[path] = dropped.get(path, 0) + (len(items) - keep)
        del items[keep:]

    if dropped:
        said = ", ".join(f"{n} from {path}" for path, n in dropped.items())
        trimmed.setdefault("notes", []).append(
            f"the result exceeded the {budget_tokens} token budget, so items were "
            f"dropped: {said}. Narrow the request, or slice the series, to see them."
        )
    return trimmed


def _longest_list(node: Any, path: str = "") -> tuple[str, list] | None:
    """The longest list anywhere in a payload, with the path that reaches it."""
    best: tuple[str, list] | None = None
    if isinstance(node, dict):
        children = node.items()
    elif isinstance(node, list):
        children = enumerate(node)  # type: ignore[assignment]
        if len(node) > 1:
            best = (path or "result", node)
    else:
        return None
    for key, value in children:
        found = _longest_list(value, f"{path}.{key}" if path else str(key))
        if found and (best is None or len(found[1]) > len(best[1])):
            best = found
    return best
