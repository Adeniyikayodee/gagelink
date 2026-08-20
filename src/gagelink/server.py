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
from .results import DEFAULT_BUDGET_TOKENS, Result
from .session import Session
from .tools import Toolkit

PROTOCOL_VERSION = "2025-06-18"

#: Sent to the client at handshake and put in front of the model before it calls anything.
#: This is the one piece of text that reaches every conversation, so it carries the things
#: that decide whether an answer is right rather than a description of the software.
#:
#: Everything in it was measured or observed. The datum rule is the error seven of eleven
#: models made in the quantity-guard evaluation. Latest against current is a live payload
#: at Little Falls holding a discharge from this morning beside a turbidity from 2019. The
#: unit warning is four spellings of one quantity across two agencies.
INSTRUCTIONS = """Hydrology data for rivers, gages, forecasts, and basins, from USGS, NOAA, and the SWOT satellite. Use it for questions about streamflow, river level, flood risk, water quality, drainage areas, and what lies upstream or downstream of a point.

Identifiers are of the form USGS-01646500, with the agency prefix and the leading zero. Use find_locations to search by state, county, or bounding box if you have a place name rather than a number.

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
            "depends on the offset it returns."
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
            "is provisional or approved."
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
        """
        assert self.toolkit is not None and self.session is not None

        if name == "export_manifest":
            payload = self.session.manifest()
            return {"content": [{"type": "text", "text": json.dumps(payload, indent=1)}]}

        method = getattr(self.toolkit, name, None)
        if method is None or name.startswith("_") or name not in {t["name"] for t in TOOLS}:
            return {
                "content": [{"type": "text", "text": f"no tool named {name!r}"}],
                "isError": True,
            }

        try:
            result: Result = method(**arguments)
        except TypeError as exc:
            # Wrong or missing arguments, which is the model's error to fix and so belongs
            # in the conversation rather than in a protocol fault.
            return {
                "content": [{"type": "text", "text": f"{name}: {exc}"}],
                "isError": True,
            }

        body = result.to_dict(self.budget_tokens)
        response: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(body, indent=1)}]
        }
        if not result.ok:
            response["isError"] = True
        return response


class MethodNotFound(Exception):
    """An unsupported JSON-RPC method, which is a -32601 rather than a server fault."""


def dispatch(server: Server, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        server.begin_session()
        return {
            "protocolVersion": PROTOCOL_VERSION,
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
    args = parser.parse_args(argv)

    serve_stdio(Server(api_key=args.api_key, budget_tokens=args.budget_tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
