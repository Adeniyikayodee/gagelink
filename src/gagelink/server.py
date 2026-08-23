"""An MCP server over the toolkit.

The tool descriptions here are part of the product rather than documentation of it. In the
quantity-guard evaluation, declaring physical metadata in the schema without enforcing it
still recovered a third of the runs that failed at baseline, so what a description says
about datums, units, and provisional record does work before any validation runs. They are
written for a model that will read them once and then act.

The session resets on `initialize`, so quantities and requests from one conversation cannot
appear in another's manifest or trip its checks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable

from . import __version__
from .results import DEFAULT_BUDGET_TOKENS, ErrorCode, Result
from .schema import validate
from .session import Session
from .tools import RAISE_INTERNAL, Toolkit

PROTOCOL_VERSION = "2025-06-18"

#: The revisions this server can speak, newest first. A client asking for one of these is
#: answered in it; a client asking for anything else is answered in the newest, which is
#: what the specification says to do and is why the list exists rather than a constant.
#: Nothing here differs between the three except the fields a client may send, which are
#: additive, so supporting the older two costs nothing and refusing them costs a client.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

#: Sent to the client at handshake and put in front of the model before it calls anything.
#: This is the one piece of text that reaches every conversation, so it carries the things
#: that decide whether an answer is right rather than a description of the software.
#:
#: Everything in it was measured or observed. The datum rule is the error seven of eleven
#: models made in the quantity-guard evaluation. Latest against current is a live payload
#: at Little Falls holding a discharge from this morning beside a turbidity from 2019. The
#: unit warning is four spellings of one quantity across two agencies.
INSTRUCTIONS = """Hydrology data for rivers, gages, forecasts, and basins, from USGS, NOAA, the UK Environment Agency, and the SWOT satellite. Use it for questions about streamflow, river level, flood risk, water quality, drainage areas, and what lies upstream or downstream of a point.

Identifiers are of the form USGS-01646500, with the agency prefix and the leading zero. Use find_locations to search by state, county, or bounding box if you have a place name rather than a number.

UK stations are EA-2604TH, using the Environment Agency's own reference. They answer describe_location and get_latest only: that service publishes no series, peaks, forecast, network, or basin, and no record grade at all. There is no search for them, so a UK question needs the reference. Everything else here is the United States.

Four things decide whether an answer is right.

A river level is not an elevation. Gage height is measured from the station's own datum, whose zero sits at some height on a national datum, and the two are both lengths in feet. Subtracting a stage from a surveyed elevation without adding that offset gives a number that looks like a freeboard and is wrong by tens of feet, in the direction of calling a levee safe. Call describe_location before any such comparison; it returns the offset. Flood thresholds from get_forecast are already on the gage datum and need no shift.

Latest is not current. Each parameter returns the last value held for it, independently, so one response can carry a discharge from this morning beside a turbidity from years ago. Pass max_age_hours, and read the age on every reading you quote.

Modelled is not measured. get_model_forecast returns National Water Model output, which covers reaches with no gage on them, so a value from it may have nothing observed behind it. get_satellite_passes returns elevations on a geoid, which cannot be compared to a stage or a survey at all.

Units are not interchangeable across these services. Discharge appears as cfs, kcfs, ft^3/s, and ft^3/s with a superscript, and every value comes back labelled with its unit, datum, and record quality. Use those labels; do not assume a unit from a magnitude.

Every value carries whether the record is provisional or approved. Say which when it matters. Never supply a number from memory: if a tool cannot provide it, report it as unavailable, and call export_manifest at the end when the answer needs to be reproducible.

Without an API key the service allows 50 requests an hour, and each result tells you how many remain."""

#: Kept short deliberately. A model degrades as its tool list grows, and eleven tools
#: covering three services is the whole of what phase one offers.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_locations",
        "description": (
            "Search USGS monitoring locations by state, county, hydrologic unit, site "
            "type, or bounding box. At least one filter is required. Returns identifiers "
            "of the form USGS-01646500, which every other tool takes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "Full state name, as in Maryland"},
                "county": {"type": "string", "description": "Full county name"},
                "hydrologic_unit_code": {"type": "string", "description": "HUC, any level"},
                "site_type": {"type": "string", "description": "As in Stream, Lake, Well"},
                "bbox": {
                    "type": "string",
                    "description": "west,south,east,north in decimal degrees",
                },
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "describe_location",
        "description": (
            "Metadata for one monitoring location: its name, position, drainage area, "
            "timezone, and the vertical datum its stage readings are measured from. Call "
            "this before comparing any stage against an elevation, because the answer "
            "depends on the offset it returns. Takes a USGS identifier or an Environment "
            "Agency one, as in EA-2604TH."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "get_latest",
        "description": (
            "The most recent value the service holds for each parameter at a location. "
            "Latest is not the same as current: the service returns the last value it has "
            "for each parameter independently, so one response can carry a discharge from "
            "this morning beside a turbidity from years ago. Pass max_age_hours to drop "
            "the stale ones. Values arrive with their unit, datum, and whether the record "
            "is provisional or approved. Takes a USGS identifier or an Environment Agency "
            "one, as in EA-2604TH, whose measures are named rather than coded and whose "
            "readings carry no grade at all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "parameters": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Parameter codes, as in 00060 for discharge",
                },
                "max_age_hours": {"type": "number"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_series",
        "description": (
            "A date range of record, returned as a handle with a summary and a small "
            "sample rather than as its points, since a year of 15-minute record is 35,000 "
            "values. Use slice_series on the handle to narrow it. Resolution is daily or "
            "continuous."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "parameter": {"type": "string"},
                "start": {"type": "string", "description": "ISO date, as in 2026-08-01"},
                "end": {"type": "string"},
                "resolution": {
                    "type": "string",
                    "enum": ["daily", "continuous"],
                    "default": "daily",
                },
            },
            "required": ["identifier", "parameter", "start", "end"],
        },
    },
    {
        "name": "slice_series",
        "description": (
            "Narrow a series already fetched under a handle and summarise what remains. "
            "Costs no request against the hourly allowance."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["handle"],
        },
    },
    {
        "name": "get_peaks",
        "description": (
            "Annual peak flow record for a location, largest first. A water year can carry "
            "more than one peak, so the count of peaks is not the count of years."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_forecast",
        "description": (
            "Observed and forecast stage from the NOAA National Water Prediction Service, "
            "with the flood thresholds that give them meaning. Stages and thresholds here "
            "are on the gage's own datum, so they can be differenced against each other "
            "and against a gage height, but not against a surveyed elevation without the "
            "offset from describe_location. Takes the USGS identifier or the NWS location "
            "id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "get_model_forecast",
        "description": (
            "National Water Model streamflow for the reach a monitoring location sits on. "
            "These are modelled values, not measurements: the model covers reaches with "
            "no gauge on them, so a figure here may have nothing observed behind it, and "
            "it carries no record-quality grade. Series are analysis_assimilation, which "
            "looks back, and short_range, medium_range, medium_range_blend, and "
            "long_range, which look forward. Not every reach publishes every series."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "series": {
                    "type": "string",
                    "enum": [
                        "analysis_assimilation",
                        "short_range",
                        "medium_range",
                        "medium_range_blend",
                        "long_range",
                    ],
                    "default": "short_range",
                },
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_satellite_passes",
        "description": (
            "Water surface elevation measured from orbit by the SWOT mission, for a river "
            "reach. Covers reaches no gauge stands on. Elevations are referenced to the "
            "EGM2008 geoid, not to a national datum and not to any gage datum, so they "
            "cannot be differenced against a stage or a surveyed elevation. A reach "
            "identifier is a SWORD river reach id and is not a USGS station number."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "feature_id": {"type": "string", "description": "SWORD reach id"},
                "start": {"type": "string", "description": "ISO date"},
                "end": {"type": "string", "description": "ISO date"},
            },
            "required": ["feature_id", "start", "end"],
        },
    },
    {
        "name": "navigate_network",
        "description": (
            "Monitoring locations upstream or downstream of a point, following the river "
            "rather than a radius. Direction is upstream, upstream_main, downstream, or "
            "downstream_diversions, where upstream includes tributaries and upstream_main "
            "follows the main stem alone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": [
                        "upstream",
                        "upstream_main",
                        "downstream",
                        "downstream_main",
                        "downstream_diversions",
                    ],
                    "default": "upstream",
                },
                "distance_km": {"type": "number", "default": 50},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_basin",
        "description": (
            "The area draining to a point. The area is computed from the delineated "
            "boundary rather than published; where a site record also states a drainage "
            "area, that figure is surveyed and is the one to quote."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"identifier": {"type": "string"}},
            "required": ["identifier"],
        },
    },
    {
        "name": "lookup_parameter",
        "description": (
            "Resolve a parameter code to what it measures, or find a code by name. "
            "Readings carry a code and no name, so this is how 00065 becomes gage height."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "export_manifest",
        "description": (
            "The record of this session: every request made, every quantity returned, and "
            "the library versions. Enough to re-run the work and check the numbers. Call "
            "it last when the answer needs to be reproducible."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]
#: What a measured value looks like wherever one appears in a result. This is the package's
#: whole claim expressed in the protocol: a number arrives with the frame that makes it
#: mean something, and a client reads the frame as a field rather than parsing it back out
#: of prose. A length without its datum is the error this exists to prevent.
QUANTITY: dict[str, Any] = {
    "type": "object",
    "description": (
        "A value with the frame that makes it meaningful. Never use the number without the "
        "unit, and never compare a length without checking the datum."
    ),
    "properties": {
        "value": {"type": "number"},
        "unit": {
            "type": "string",
            "description": "As the service publishes it, as in ft^3/s, ft, or kcfs",
        },
        "datum": {
            "type": "string",
            "description": (
                "The surface a length is measured from, as in NAVD88, NGVD29, EGM2008, or "
                "the station's own gage datum. Two lengths on different datums cannot be "
                "differenced."
            ),
        },
        "quality": {
            "type": "string",
            "description": "provisional, approved, estimated, or unverified",
        },
        "crs": {"type": "string", "description": "For a coordinate, as in EPSG:4326"},
    },
    "required": ["value", "unit"],
}

#: A reference to the quantity definition, written once because it appears in most of the
#: output schemas below.
Q_REF: dict[str, Any] = {"$ref": "#/$defs/quantity"}


def _returns(data: dict[str, Any], describes: str) -> dict[str, Any]:
    """The result envelope around one tool's data.

    Every tool answers in the same envelope, so a client learns one shape rather than
    thirteen. Only `ok` is required: a failure carries `error`, `message`, and `repair`
    instead of `data`, and several tools include a key only when the service published
    the thing it names, which is a fact about the record rather than a defect. A schema
    that required those keys would be declaring something this package cannot promise.
    """
    return {
        "type": "object",
        "description": describes,
        "properties": {
            "ok": {
                "type": "boolean",
                "description": "False means the call failed and repair says how to fix it",
            },
            "data": {
                "type": "object",
                "description": "The answer, present when ok is true",
                "properties": data,
                "additionalProperties": True,
            },
            "error": {
                "type": "string",
                "enum": ErrorCode.all(),
                "description": "Present when ok is false",
            },
            "message": {"type": "string", "description": "What went wrong"},
            "repair": {
                "type": "string",
                "description": "What to do about it. Follow this rather than guessing.",
            },
            "requests_remaining_this_hour": {
                "type": "integer",
                "description": "The allowance left against the service, for planning",
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything true of the result that its numbers do not say",
            },
        },
        "required": ["ok"],
        "$defs": {"quantity": QUANTITY},
    }


#: A location as it appears in a list, which is the short form: enough to choose one and
#: call describe_location on it, not enough to answer from.
_LOCATION_SUMMARY: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "As in USGS-01646500"},
            "name": {"type": "string"},
            "site_type": {"type": "string"},
            "state": {"type": "string"},
        },
        "additionalProperties": True,
    },
}

#: The summary a series is returned as, in place of its points.
_SERIES_SUMMARY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "unit": {"type": "string"},
        "statistic": {"type": "string"},
        "first": {"type": "object"},
        "last": {"type": "object"},
        "minimum": {"type": "object"},
        "maximum": {"type": "object"},
        "mean": {"type": "number"},
        "quality": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every grade present in the span, not just the commonest",
        },
    },
    "additionalProperties": True,
}

#: One reading, which is where the unit, datum, and grade actually reach the model.
_READING: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "parameter_code": {"type": "string"},
            "parameter": {"type": "string"},
            "statistic": {"type": "string"},
            "value": Q_REF,
            "time": {"type": "string"},
            "age_hours": {
                "type": "number",
                "description": (
                    "How old this reading is. Latest is not current, and each parameter "
                    "ages independently."
                ),
            },
            "qualifiers": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
}

#: What each tool puts in `data`, taken from what the tools return rather than from what
#: they were meant to. Keys a service publishes only sometimes are declared and not
#: required, which is the honest reading of a record that is not uniform.
RETURNS: dict[str, dict[str, Any]] = {
    "find_locations": {
        "locations": _LOCATION_SUMMARY,
        "count": {"type": "integer"},
    },
    "describe_location": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "site_type": {"type": "string"},
        "state": {"type": "string"},
        "latitude": {"type": "number", "description": "Decimal degrees, WGS84"},
        "longitude": {"type": "number", "description": "Decimal degrees, WGS84"},
        "drainage_area": Q_REF,
        "altitude_of_gage_datum": Q_REF,
        "gage_datum": {
            "type": "string",
            "description": (
                "The datum the station's own zero sits on. This is the offset a stage has "
                "to be shifted by before it can be compared to a surveyed elevation."
            ),
        },
        "timezone": {"type": "string"},
        "hydrologic_unit_code": {"type": "string"},
    },
    "get_latest": {
        "location": {"type": "string"},
        "readings": _READING,
    },
    "get_series": {
        "handle": {
            "type": "string",
            "description": "Pass to slice_series. Lasts for the session.",
        },
        "summary": _SERIES_SUMMARY,
        "preview": {"type": "array", "items": {"type": "object"}},
    },
    "slice_series": {
        "handle": {"type": "string"},
        "summary": _SERIES_SUMMARY,
        "preview": {"type": "array", "items": {"type": "object"}},
    },
    "get_peaks": {
        "location": {"type": "string"},
        "peaks": {"type": "array", "items": {"type": "object"}},
        "peaks_in_record": {"type": "integer"},
    },
    "get_forecast": {
        "gauge": {"type": "string"},
        "usgs_id": {"type": "string"},
        "name": {"type": "string"},
        "timezone": {"type": "string"},
        "observed": {"type": "object"},
        "forecast": {"type": "object"},
        "thresholds": {
            "type": "object",
            "description": (
                "Flood categories on the gage's own datum, so they need no shift before "
                "being compared to a stage from this service or from USGS 00065."
            ),
        },
        "below_minor_flooding": Q_REF,
    },
    "get_model_forecast": {
        "reach": {"type": "string"},
        "series": {"type": "string"},
        "looks": {"type": "string"},
        "issued": {"type": "string"},
        "points": {"type": "integer"},
        "first": {"type": "object"},
        "last": {"type": "object"},
        "peak": {"type": "object"},
    },
    "get_satellite_passes": {
        "reach": {"type": "string"},
        "datum": {
            "type": "string",
            "description": (
                "EGM2008, a geoid. Not a national datum and not a gage datum, so an "
                "elevation here cannot be differenced against a stage or a survey."
            ),
        },
        "passes": {"type": "integer"},
        "with_an_elevation": {"type": "integer"},
        "observations": {"type": "array", "items": {"type": "object"}},
    },
    "navigate_network": {
        "from": {"type": "string"},
        "direction": {"type": "string"},
        "within_km": {"type": "number"},
        "locations": _LOCATION_SUMMARY,
        "count": {"type": "integer"},
    },
    "get_basin": {
        "location": {"type": "string"},
        "area": Q_REF,
        "bounding_box": {"type": "object"},
        "polygon_vertices": {
            "type": "integer",
            "description": "The count only. The geometry is never put in an answer.",
        },
    },
    "lookup_parameter": {
        "parameter_code": {"type": "string"},
        "name": {"type": "string"},
        "parameters": {"type": "array", "items": {"type": "object"}},
    },
    "export_manifest": {
        "question": {"type": "string"},
        "started_at": {"type": "string"},
        "versions": {
            "type": "object",
            "description": "The libraries the answer was computed with",
        },
        "retrievals": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Every request, with its URL, the time it was made, and the hash of what "
                "came back. This is what a replay checks against."
            ),
        },
        "locations": {"type": "array", "items": {"type": "object"}},
        "quantities": {
            "type": "array",
            "items": {"type": "object"},
            "description": "Every value the session produced, with its unit and datum",
        },
    },
}

#: What each tool is called in a client's interface, where a name is read by a person
#: rather than by a model.
TITLES: dict[str, str] = {
    "find_locations": "Find monitoring locations",
    "describe_location": "Describe a location",
    "get_latest": "Latest readings",
    "get_series": "Time series",
    "slice_series": "Slice a series",
    "get_peaks": "Annual peak flows",
    "get_forecast": "River forecast and flood thresholds",
    "get_model_forecast": "Modelled forecast",
    "get_satellite_passes": "Satellite elevations",
    "navigate_network": "Navigate the river network",
    "get_basin": "Contributing basin",
    "lookup_parameter": "Look up a parameter code",
    "export_manifest": "Export the session manifest",
}

#: Every tool here reads and none writes, so a client has no reason to ask a person before
#: any of them. Saying so is the difference between one consent and thirteen prompts.
#: `openWorldHint` separates the twelve that call a service from the one that reports what
#: this session already did.
_READS_A_SERVICE: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

def _annotate(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the parts of a tool declaration that are the same for all of them.

    Written as a pass over the list rather than repeated thirteen times, so that a new
    tool cannot be added without a title and an output schema: the lookups below raise
    rather than defaulting, and a test calls this at import.
    """
    for tool in tools:
        name = tool["name"]
        tool["title"] = TITLES[name]
        tool["annotations"] = {
            "title": TITLES[name],
            **_READS_A_SERVICE,
            **({"openWorldHint": False} if name == "export_manifest" else {}),
        }
        # An argument the schema does not name is a mistake worth reporting as one. Without
        # this the call reaches Python and comes back as a TypeError about keyword
        # arguments, which describes the implementation rather than the contract.
        tool["inputSchema"].setdefault("additionalProperties", False)
        tool["outputSchema"] = _returns(RETURNS[name], tool["description"].split(".")[0])
    return tools


TOOLS = _annotate(TOOLS)


class Server:
    """The toolkit behind the MCP protocol.

    Holds one session at a time, replaced whenever a client initialises, so that a
    long-lived server process does not accumulate one conversation's quantities into the
    next one's manifest.
    """

    def __init__(
        self,
        api_key: str | None = None,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.api_key = api_key
        self.budget_tokens = budget_tokens
        self._factory = session_factory or (lambda: Session(api_key=self.api_key))
        self.session: Session | None = None
        self.toolkit: Toolkit | None = None
        self.begin_session()

    def begin_session(self, question: str = "") -> None:
        """Start a fresh session, closing any the previous client left open."""
        if self.session is not None:
            self.session.__exit__(None, None, None)
        session = self._factory()
        session.question = question
        session.__enter__()
        self.session = session
        self.toolkit = Toolkit(session, budget_tokens=self.budget_tokens)

    def list_tools(self) -> list[dict[str, Any]]:
        return TOOLS

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool and return its result as MCP content.

        A failure comes back as content marked in error rather than as a protocol fault,
        which is what keeps the repair in front of the model instead of ending the turn.
        Nothing raised in here reaches the transport: the last clause below is the seam
        that guarantees it, because a protocol fault loses the repair, the quota count,
        and the model's chance of saying what went wrong.
        """
        try:
            return self._call(name, arguments)
        except Exception as exc:
            if os.environ.get(RAISE_INTERNAL):
                raise
            return self._respond(
                Result.failure(
                    ErrorCode.INTERNAL_ERROR,
                    f"{name} failed inside gagelink: {type(exc).__name__}: {exc}",
                    "This is a fault in the tool rather than in the request. Report the "
                    "data as unavailable rather than supplying a value from memory.",
                ).to_dict(self.budget_tokens),
                ok=False,
            )

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self.toolkit is not None and self.session is not None

        declared = next((t for t in TOOLS if t["name"] == name), None)
        if declared is None:
            known = ", ".join(sorted(t["name"] for t in TOOLS))
            return self._failed(
                ErrorCode.INVALID_ARGUMENTS,
                f"no tool named {name!r}",
                f"The tools are {known}.",
            )

        wrong = validate(arguments, declared["inputSchema"])
        if wrong:
            # Checked against the schema the model was given rather than against the Python
            # signature, so the correction names the contract and not the implementation.
            return self._failed(
                ErrorCode.INVALID_ARGUMENTS,
                f"{name}: " + "; ".join(wrong),
                "Correct the arguments against the tool's schema and call it again.",
            )

        if name == "export_manifest":
            return self._respond(
                Result(ok=True, data=self.session.manifest()).to_dict(self.budget_tokens),
                ok=True,
            )

        method = getattr(self.toolkit, name)
        result: Result = method(**arguments)
        return self._respond(result.to_dict(self.budget_tokens), ok=result.ok)

    def _failed(self, code: str, message: str, repair: str) -> dict[str, Any]:
        failure = Result.failure(code, message, repair)
        failure.quota_remaining = self.session.quota_remaining if self.session else None
        return self._respond(failure.to_dict(self.budget_tokens), ok=False)

    def _respond(self, body: dict[str, Any], ok: bool) -> dict[str, Any]:
        """One result in both the forms a client may read.

        `structuredContent` is the payload as data, which is what lets a client read a
        unit or a datum as a field instead of parsing it back out of a string. The same
        payload is repeated as text, which the protocol asks for so that a client written
        before structured output still sees the answer.
        """
        response: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(body, indent=1)}],
            "structuredContent": body,
        }
        if not ok:
            response["isError"] = True
        return response


class MethodNotFound(Exception):
    """An unsupported JSON-RPC method, which is a -32601 rather than a server fault."""


def dispatch(server: Server, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        server.begin_session()
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": (
                asked if asked in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
            ),
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "gagelink",
                "title": "Hydrology data: rivers, gages, forecasts, basins",
                "version": __version__,
            },
            # Clients surface this to the model before it calls anything, which makes it
            # the highest-leverage text in the package.
            "instructions": INSTRUCTIONS,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": server.list_tools()}
    if method == "tools/call":
        return server.call_tool(params.get("name", ""), params.get("arguments") or {})
    raise MethodNotFound(method)


def serve_stdio(server: Server, stdin=None, stdout=None) -> None:
    """Answer JSON-RPC on stdin."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            continue  # a notification; nothing to answer

        try:
            result = dispatch(server, method, message.get("params") or {})
        except MethodNotFound as exc:
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {exc}"},
            }
        except Exception as exc:  # a protocol-level failure, not a tool failure
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": str(exc)},
            }
        else:
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        stdout.write(json.dumps(reply) + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gagelink-mcp",
        description="Serve hydrology tools over MCP.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GAGELINK_API_KEY") or os.environ.get("USGS_API_KEY"),
        help=(
            "USGS Water Data API key, free from https://api.waterdata.usgs.gov/signup. "
            "Without one the allowance is 50 requests per hour, which is two or three "
            "questions. Read from GAGELINK_API_KEY if not given."
        ),
    )
    parser.add_argument(
        "--budget-tokens",
        type=int,
        default=DEFAULT_BUDGET_TOKENS,
        help="ceiling on one tool result, in estimated tokens",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "serve over Streamable HTTP instead of stdio, for a client that cannot spawn "
            "a process. Binds to the loopback address unless --host says otherwise."
        ),
    )
    parser.add_argument("--host", default=None, help="interface to bind, with --http")
    parser.add_argument("--port", type=int, default=None, help="port to bind, with --http")
    parser.add_argument("--path", default=None, help="endpoint path, with --http")
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        metavar="HOST",
        help=(
            "an additional browser origin that may reach the server, by host. Repeatable. "
            "Loopback is always allowed; anything else has to be named here, because a "
            "page the user did not write can otherwise drive a server on their machine."
        ),
    )
    args = parser.parse_args(argv)

    if args.http:
        return _serve_http(args)

    for unusable in ("host", "port", "path", "allow_origin"):
        if getattr(args, unusable) is not None:
            parser.error(f"--{unusable.replace('_', '-')} applies only with --http")

    serve_stdio(Server(api_key=args.api_key, budget_tokens=args.budget_tokens))
    return 0


def _serve_http(args: argparse.Namespace) -> int:
    """Run the HTTP endpoint until interrupted.

    Imported here rather than at the top because stdio is the default and nothing about it
    should depend on the HTTP stack being importable.
    """
    from .streamable import DEFAULT_HOST, DEFAULT_PATH, DEFAULT_PORT, LOCAL_HOSTS, serve_http

    host = args.host or DEFAULT_HOST
    origins = frozenset(LOCAL_HOSTS | {o.lower() for o in (args.allow_origin or [])})
    httpd = serve_http(
        lambda: Server(api_key=args.api_key, budget_tokens=args.budget_tokens),
        host=host,
        port=args.port or DEFAULT_PORT,
        path=args.path or DEFAULT_PATH,
        allowed_origins=origins,
    )
    where = f"http://{host}:{httpd.server_address[1]}{args.path or DEFAULT_PATH}"
    print(f"gagelink {__version__} serving MCP over Streamable HTTP at {where}", file=sys.stderr)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        # Worth saying out loud. The server needs no account and enforces no authorisation,
        # so binding it to a reachable interface hands anyone who can route to it this
        # machine's share of the hourly allowance.
        print(
            f"warning: bound to {host}, which is not loopback. This server has no "
            "authentication, so anyone who can reach it can spend the API allowance.",
            file=sys.stderr,
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        getattr(httpd, "sessions").close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
