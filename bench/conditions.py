"""The three conditions, which differ only in what the model is given to work with.

The data is identical throughout: the same recorded responses, the same station, the same
questions. What varies is the interface between the model and those bytes.

`http_only` gives a single fetch tool returning the service's own JSON, which is what an
agent developer has today and what the existing hydrology MCP servers amount to.

`toolkit_plain` gives the eleven tools with their results stripped to bare magnitudes and
their notes removed, which is what a competent wrapper over the same endpoints would
return: the right values, correctly retrieved, with the reference frames dropped.

`toolkit` gives the tools as they are, with units, datums, record quality, staleness, and
the notes that state what the payload does not.

The middle condition is the one that makes the measurement worth taking. Without it a
difference between the first and the last would only show that structured retrieval beats
raw JSON, which nobody doubts. With it, the difference between the last two is what the
metadata is worth on its own.
"""

from __future__ import annotations

import json
from typing import Any

from gagelink.server import TOOLS

HTTP_ONLY = "http_only"
TOOLKIT_PLAIN = "toolkit_plain"
TOOLKIT = "toolkit"
CONDITIONS = (HTTP_ONLY, TOOLKIT_PLAIN, TOOLKIT)

#: The endpoints the fetch tool will answer, described as the services describe them. A
#: developer writing this tool today would read the same documentation.
ENDPOINTS = """Available paths:
  usgs/monitoring-locations/items?id=USGS-01646500
      Site record: name, position, altitude, vertical_datum, drainage_area,
      time_zone_abbreviation, uses_daylight_savings.
  usgs/latest-continuous/items?monitoring_location_id=USGS-01646500
      Most recent value per time series: parameter_code, unit_of_measure, value, time,
      qualifier, approval_status.
  usgs/peaks/items?monitoring_location_id=USGS-01646500
      Annual peak flows: value, unit_of_measure, water_year, time.
  nwps/gauges/01646500
      Forecast gauge: flood.categories with stage and flow per category, flood.stageUnits,
      flood.flowUnits, status.observed and status.forecast with primary, primaryUnit,
      secondary, secondaryUnit.

Parameter codes: 00060 discharge, 00065 gage height, 00010 water temperature,
00095 specific conductance, 00300 dissolved oxygen, 00400 pH, 63160 water surface
elevation above NAVD88, 63680 turbidity, 99133 nitrate."""

FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "http_get",
        "description": (
            "Fetch a URL from the USGS Water Data API or the NOAA National Water "
            "Prediction Service and return its JSON response verbatim.\n\n" + ENDPOINTS
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "One of the paths listed above.",
                }
            },
            "required": ["path"],
        },
    },
}


def schemas(condition: str) -> list[dict[str, Any]]:
    """The tool definitions a model is shown under one condition."""
    if condition == HTTP_ONLY:
        return [FETCH_TOOL]
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
        }
        for tool in TOOLS
    ]


def plain(payload: Any) -> Any:
    """Strip a result to what a wrapper without reference frames would return.

    A quantity becomes its magnitude, and the notes go. Nothing else changes: the same
    values, retrieved the same way, from the same responses. What is removed is only the
    metadata, which is what the condition is measuring the worth of.
    """
    if isinstance(payload, dict):
        if set(payload) >= {"value", "unit"} and isinstance(
            payload.get("value"), (int, float)
        ):
            return payload["value"]
        return {
            key: plain(value)
            for key, value in payload.items()
            if key not in {"notes", "quality", "datum", "crs"}
        }
    if isinstance(payload, list):
        return [plain(item) for item in payload]
    return payload


def render(condition: str, body: dict[str, Any]) -> str:
    """One tool result, as the model will see it under this condition."""
    return json.dumps(plain(body) if condition == TOOLKIT_PLAIN else body, indent=1)
