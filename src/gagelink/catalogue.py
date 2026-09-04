"""What the server offers besides its tools: prompts, resources, and argument completion.

A tool list says what can be called. It does not say what the server is for, and the
distinction matters because the datum rule this package exists to enforce is a procedure
rather than a call. `describe_location` returns an offset; knowing to fetch it before
differencing a stage against a levee crest is the part a model gets wrong. A prompt is
where that procedure can be stated once and invoked by name, so the sequence arrives in
the conversation already correct instead of being reconstructed from a paragraph of
server instructions.

Resources carry the tables that do not change between calls. The parameter codes, the
datums that can be converted onto, and which tools answer for which country are all facts
about this server rather than about a station, and a client that reads them as resources
spends no request against the hourly allowance to learn them.

Completion is scoped by the specification to prompt arguments and resource template
variables, not to tool arguments. That is the whole of what it can reach, so the closed
vocabularies worth completing are exposed here as templates as well as inside the tool
schemas: a parameter code and a datum name are both things a client can now offer before
a call rather than after a refusal.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .tools import COMMON_PARAMETERS
from .vdatum import DESCRIPTIONS, TARGET, tidal_datums

#: The scheme every resource here is named under. One authority, so a client can tell a
#: gagelink resource from any other server's without reading a list first.
SCHEME = "gagelink://"

#: What a caller may ask an elevation to be converted onto, in the order they are worth
#: offering: the modern national datum first, since that is what a survey is on and what
#: the majority of stations are not on, then the tidal datums for a question about the
#: level relative to the tide. Built from the converter's own table rather than typed out
#: again, so a datum cannot be offered to a model that the module would then refuse.
CONVERTIBLE_DATUMS: list[str] = [TARGET, *tidal_datums()]

#: Which tools answer for which network, and what each service leaves out. Held here
#: rather than in prose because a model choosing a tool for a UK station needs the gap to
#: be a field it can read, not a sentence it has to infer a negative from.
COVERAGE: dict[str, Any] = {
    "US": {
        "identifier": "USGS-01646500, with the agency prefix and the leading zero",
        "services": [
            "USGS Water Data",
            "NOAA National Water Prediction Service",
            "NOAA National Water Model",
            "NOAA VDatum",
            "USGS Hydro Network-Linked Data Index",
        ],
        "tools": [
            "find_locations",
            "describe_location",
            "get_latest",
            "get_series",
            "slice_series",
            "get_peaks",
            "get_forecast",
            "get_model_forecast",
            "navigate_network",
            "get_basin",
            "lookup_parameter",
        ],
        "notes": [
            "Datum conversion through on_datum covers the contiguous states only.",
            "58% of stations publish their gage datum offset on NGVD29 rather than "
            "NAVD88, and 72% of published offsets are known no better than a foot.",
        ],
    },
    "FR": {
        "identifier": "FR-F700000102, from Hub'Eau",
        "services": ["Hub'Eau"],
        "tools": ["find_locations", "describe_location", "get_latest", "get_series"],
        "notes": [
            "Hub'Eau publishes no unit on any value. A level is millimetres above the "
            "station's own zero and a discharge is litres per second, and both are "
            "labelled here from a recorded table.",
            "Search by river as the agency writes it, with the article: La Seine, Le "
            "Rhone.",
        ],
    },
    "GB": {
        "identifier": "EA-2604TH, the Environment Agency's own reference",
        "services": ["UK Environment Agency"],
        "tools": ["find_locations", "describe_location", "get_latest"],
        "notes": [
            "The agency publishes no record grade on live data, so age is the only "
            "staleness signal for a UK reading.",
            "River names match in full and in the agency's own spelling, so River Thames "
            "returns stations and Thames returns none. Free text against the station "
            "name is the filter to use when that spelling is unknown.",
        ],
    },
    "global": {
        "identifier": "A SWORD river reach id, which is not a station number",
        "services": ["SWOT via NASA Hydrocron"],
        "tools": ["get_satellite_passes"],
        "notes": [
            "Elevations are on the EGM2008 geoid and can be differenced against nothing "
            "until they are moved off it.",
            "SWOT observes the whole world; the datum service that would move an "
            "elevation off the geoid covers the contiguous United States.",
        ],
    },
}


# Prompts ----------------------------------------------------------------------------------


#: The workflows this server is for, as opposed to the calls it accepts.
#:
#: Each one is a sequence a model gets wrong when it assembles the sequence itself. The
#: freeboard prompt is the error seven of eleven models made in the quantity-guard
#: evaluation, written out as an order of operations. The station search prompt exists
#: because the three networks disagree about what a searchable name is, and a model that
#: guesses gets an empty result rather than a correction.
PROMPTS: list[dict[str, Any]] = [
    {
        "name": "freeboard_check",
        "title": "Freeboard against a surveyed elevation",
        "description": (
            "How much clearance there is between the water and a levee crest, floodwall, "
            "bridge soffit, or any other surveyed elevation. Fetches the gage datum "
            "offset and its accuracy before differencing anything, which is the step that "
            "decides whether the answer is right."
        ),
        "arguments": [
            {
                "name": "identifier",
                "description": "The station, as in USGS-01646500",
                "required": True,
            },
            {
                "name": "crest_elevation",
                "description": "The surveyed elevation, with its unit, as in 41 ft",
                "required": True,
            },
            {
                "name": "crest_datum",
                "description": (
                    "The datum the survey is on. NAVD88 unless the survey says otherwise."
                ),
                "required": False,
            },
        ],
    },
    {
        "name": "flood_status",
        "title": "Is this river in flood",
        "description": (
            "Where the river stands against its flood categories now and over the "
            "forecast, with the record quality and the age of every reading quoted."
        ),
        "arguments": [
            {
                "name": "identifier",
                "description": "The station, as in USGS-01646500",
                "required": True,
            },
        ],
    },
    {
        "name": "find_a_station",
        "title": "Find a station from a place name",
        "description": (
            "Turn a river or town into station identifiers, using the filter the relevant "
            "agency actually matches on. The three networks disagree about what a "
            "searchable name is, and the wrong filter returns an empty list rather than "
            "an error."
        ),
        "arguments": [
            {
                "name": "place",
                "description": "A river, town, county, or state",
                "required": True,
            },
            {
                "name": "country",
                "description": "US, FR, or GB. US if not given.",
                "required": False,
            },
        ],
    },
    {
        "name": "reproducible_answer",
        "title": "Answer with a manifest",
        "description": (
            "Answer a hydrology question and end with the record of how it was answered: "
            "every request, every value, and the library versions, enough to re-run the "
            "work and check the numbers."
        ),
        "arguments": [
            {
                "name": "question",
                "description": "The question to answer",
                "required": True,
            },
        ],
    },
]


def _freeboard(arguments: Mapping[str, str]) -> str:
    identifier = arguments.get("identifier", "")
    crest = arguments.get("crest_elevation", "")
    datum = arguments.get("crest_datum") or TARGET
    return f"""How much freeboard is there at {identifier} against a crest of {crest} on {datum}?

Work it in this order, because the subtraction is not defined until the third step.

1. Call get_latest on {identifier} for gage height, 00065. Note its unit, its record
   quality, and its age. A stage is measured upward from the station's own zero and is
   not an elevation.
2. Call describe_location on {identifier} with on_datum={datum}. Read the gage datum
   offset, the datum it is published on, the altitude accuracy, and the method. If the
   published datum is not {datum}, use altitude_on_requested_datum and the
   offset_uncertainty beside it.
3. Add the offset to the stage. That puts the water surface on {datum}, which is the
   frame {crest} is already in. Subtract to get the freeboard.
4. State the freeboard with the uncertainty from step 2 beside it. A freeboard cannot be
   tighter than how well the offset is known, whatever precision the stage was read to,
   and at most stations that is worse than a foot.
5. Say whether the stage is provisional or approved, and how old it is.

If the offset is missing or the conversion is refused for want of coverage, report the
freeboard as undetermined and say which step failed. Do not difference the two numbers
without the offset: at Little Falls on the Potomac that overstates the margin by 37 ft,
in the direction of calling a levee safe."""


def _flood_status(arguments: Mapping[str, str]) -> str:
    identifier = arguments.get("identifier", "")
    return f"""What is the flood status at {identifier}, now and over the forecast?

1. Call get_forecast on {identifier}. It returns observed and forecast stage with the
   flood thresholds that give them meaning. Those thresholds are on the gage's own datum
   and need no shift before being compared against a stage from this service or against
   USGS 00065.
2. Say which category the current stage sits in, and whether the forecast crosses into a
   higher one and when.
3. Call get_latest on {identifier} as well if a discharge is wanted. Read the age on
   every reading you quote: each parameter returns its last held value independently, so
   one response can carry a discharge from this morning beside a turbidity from years
   ago.
4. Say whether each value is provisional or approved.

If the station publishes no forecast, say so rather than substituting a modelled one.
get_model_forecast is National Water Model output and may have nothing measured behind
it, so name it as modelled if you use it at all."""


def _find_a_station(arguments: Mapping[str, str]) -> str:
    place = arguments.get("place", "")
    country = (arguments.get("country") or "US").upper()
    guidance = {
        "US": (
            "Use find_locations with state as the full state name, county as the full "
            "county name, or bbox. The USGS collection has no river-name filter, so a "
            "river has to be reached through a state or a bounding box."
        ),
        "FR": (
            "Use find_locations with country=FR. river takes the watercourse with its "
            "article, as in La Seine or Le Rhone. state is the region, county the "
            "commune, and hydrologic_unit_code the department number."
        ),
        "GB": (
            "Use find_locations with country=GB. river is the watercourse as the agency "
            "writes it, River Thames rather than Thames, matched in full. county is the "
            "town. state is free text matched against the station name, which is the "
            "filter to reach for when the agency's spelling is not known."
        ),
    }.get(country, "")
    return f"""Find the monitoring stations for {place}.

{guidance}

If the first filter returns nothing, the spelling is the likeliest cause rather than the
absence of a station. Try the free-text or name filter before concluding the network has
no coverage there.

Return the identifiers with the station names, and say which one you would use for a
question about the main river as opposed to a tributary. Every other tool takes the
identifier exactly as find_locations returns it."""


def _reproducible_answer(arguments: Mapping[str, str]) -> str:
    question = arguments.get("question", "")
    return f"""{question}

Answer this using the gagelink tools only. Never supply a number from memory: if a tool
cannot provide it, report it as unavailable.

Quote every value with its unit, its datum where it is a length, and whether the record
is provisional or approved. Where two lengths are differenced, say what datum both are on
and where the offset came from.

Call export_manifest last. It returns every request made, every value returned, and the
library versions, which is what lets the answer be re-run and checked. Say in your answer
that the manifest is there and what it covers."""


#: How each prompt turns its arguments into the message a client sends. Kept beside the
#: declarations rather than inside them because a declaration is data a client caches and
#: this is code, and the lookup below raises rather than defaulting, so a prompt cannot be
#: declared without being renderable.
RENDERERS: dict[str, Callable[[Mapping[str, str]], str]] = {
    "freeboard_check": _freeboard,
    "flood_status": _flood_status,
    "find_a_station": _find_a_station,
    "reproducible_answer": _reproducible_answer,
}


class UnknownPrompt(KeyError):
    """A prompt name this server does not declare."""


class UnknownResource(KeyError):
    """A resource URI this server does not serve."""


def prompt(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One prompt rendered into the messages a client will send.

    Missing optional arguments are filled by the renderer. A missing required one is left
    to render as an empty span rather than refused, because a client that offers a prompt
    with a blank field and gets an error has no way to show the model what was wanted;
    the text still names what it is missing.
    """
    declared = next((p for p in PROMPTS if p["name"] == name), None)
    if declared is None:
        raise UnknownPrompt(name)
    given = {k: str(v) for k, v in (arguments or {}).items() if v is not None}
    return {
        "description": declared["description"],
        "messages": [
            {
                "role": "user",
                "content": {"type": "text", "text": RENDERERS[name](given)},
            }
        ],
    }


# Resources --------------------------------------------------------------------------------


#: The tables that do not change between calls, and the one that changes with the ledger.
#:
#: `instructions` is here because a client is not obliged to surface the text a server
#: sends at discovery, and the four rules in it are what decide whether an answer is
#: right. A resource is the one form a model can ask for by name when its client did not
#: put them in front of it.
RESOURCES: list[dict[str, Any]] = [
    {
        "uri": f"{SCHEME}instructions",
        "name": "instructions",
        "title": "What decides whether an answer is right",
        "description": (
            "The four rules the server sends at discovery: a river level is not an "
            "elevation, latest is not current, modelled is not measured, and units are "
            "not interchangeable across these services."
        ),
        "mimeType": "text/markdown",
    },
    {
        "uri": f"{SCHEME}parameters",
        "name": "parameters",
        "title": "Common parameter codes",
        "description": (
            "The codes readings arrive labelled with, and what each one measures. "
            "Readings carry a code and no name, so this is how 00065 becomes gage height."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": f"{SCHEME}datums",
        "name": "datums",
        "title": "Vertical datums and what converts between them",
        "description": (
            "Every datum this server names, what it is, and whether an elevation can be "
            "converted onto it. Two lengths on different datums cannot be differenced."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": f"{SCHEME}coverage",
        "name": "coverage",
        "title": "Which tools answer for which country",
        "description": (
            "Per network: the identifier form, the services behind it, the tools it "
            "answers, and what that service does not publish."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": f"{SCHEME}manifest",
        "name": "manifest",
        "title": "This conversation's manifest",
        "description": (
            "The same record export_manifest returns, readable without spending a tool "
            "call: every request made, every value returned, and the library versions."
        ),
        "mimeType": "application/json",
    },
]

#: The two closed vocabularies a client can complete against, exposed as templates so that
#: completion can reach them. The specification scopes `completion/complete` to prompt
#: arguments and resource template variables, so a vocabulary that lives only inside a
#: tool's input schema cannot be offered before the call.
RESOURCE_TEMPLATES: list[dict[str, Any]] = [
    {
        "uriTemplate": f"{SCHEME}parameter/{{code}}",
        "name": "parameter",
        "title": "One parameter code",
        "description": "What a five-digit parameter code measures.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": f"{SCHEME}datum/{{name}}",
        "name": "datum",
        "title": "One vertical datum",
        "description": "What a datum is, and whether an elevation can be converted onto it.",
        "mimeType": "application/json",
    },
]


def _datums() -> dict[str, Any]:
    """Every datum this server names, and what can be done with each.

    Convertibility is read from the converter's own table rather than restated, so a datum
    cannot be described here as reachable that the module would then refuse.
    """
    return {
        "convertible_onto": CONVERTIBLE_DATUMS,
        "coverage": (
            "Conversion runs through NOAA's VDatum and covers the contiguous United "
            "States. Elsewhere it is refused rather than approximated."
        ),
        "datums": {
            name: {
                "description": description,
                "convertible_onto": name in CONVERTIBLE_DATUMS,
                "tidal": name in tidal_datums(),
            }
            for name, description in DESCRIPTIONS.items()
        },
        "note": (
            "A gage height is measured from the station's own zero, which is not in this "
            "table: it is a station-specific surface whose height on a national datum is "
            "what describe_location returns."
        ),
    }


def _parameters() -> dict[str, Any]:
    return {
        "parameters": [
            {"parameter_code": code, "name": name} for code, name in COMMON_PARAMETERS.items()
        ],
        "note": (
            "The commonest codes, held locally. lookup_parameter reaches the full USGS "
            "collection for anything not here."
        ),
    }


def resource(
    uri: str,
    instructions: str,
    manifest: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """One resource read, in the contents shape the protocol asks for.

    The manifest is passed as a callable rather than as data because reading it is the one
    thing here that depends on which conversation asked, and building it for every read of
    an unrelated resource would be work done to be thrown away.
    """
    if uri == f"{SCHEME}instructions":
        return _contents(uri, "text/markdown", instructions)
    if uri == f"{SCHEME}parameters":
        return _json(uri, _parameters())
    if uri == f"{SCHEME}datums":
        return _json(uri, _datums())
    if uri == f"{SCHEME}coverage":
        return _json(uri, {"networks": COVERAGE})
    if uri == f"{SCHEME}manifest":
        return _json(uri, manifest())

    if uri.startswith(f"{SCHEME}parameter/"):
        code = uri[len(f"{SCHEME}parameter/") :]
        if code in COMMON_PARAMETERS:
            return _json(uri, {"parameter_code": code, "name": COMMON_PARAMETERS[code]})
        raise UnknownResource(uri)

    if uri.startswith(f"{SCHEME}datum/"):
        name = uri[len(f"{SCHEME}datum/") :].upper()
        if name in DESCRIPTIONS:
            return _json(
                uri,
                {
                    "datum": name,
                    "description": DESCRIPTIONS[name],
                    "convertible_onto": name in CONVERTIBLE_DATUMS,
                    "tidal": name in tidal_datums(),
                },
            )
        raise UnknownResource(uri)

    raise UnknownResource(uri)


def _contents(uri: str, mime: str, text: str) -> dict[str, Any]:
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


def _json(uri: str, payload: Any) -> dict[str, Any]:
    return _contents(uri, "application/json", json.dumps(payload, indent=1))


# Completion -------------------------------------------------------------------------------


#: The maximum a completion result may carry, fixed by the specification. Nothing here
#: reaches it, but the count is reported honestly against it rather than assumed.
COMPLETION_LIMIT = 100

def _vocabulary(ref: Mapping[str, Any], argument_name: str) -> list[str]:
    """What one argument may be, or nothing where it is free text.

    Keyed off the reference the specification sends: a prompt by name, a resource template
    by its URI template. A station identifier and a place name are absent deliberately;
    neither is a closed set, and offering a prefix of one would suggest the rest of it
    exists.
    """
    kind = ref.get("type")
    if kind == "ref/prompt":
        name = ref.get("name")
        if name == "freeboard_check" and argument_name == "crest_datum":
            return CONVERTIBLE_DATUMS
        if name == "find_a_station" and argument_name == "country":
            return ["US", "FR", "GB"]
        return []
    if kind == "ref/resource":
        uri = ref.get("uri") or ref.get("uriTemplate")
        if uri == f"{SCHEME}parameter/{{code}}" and argument_name == "code":
            return list(COMMON_PARAMETERS)
        if uri == f"{SCHEME}datum/{{name}}" and argument_name == "name":
            return list(DESCRIPTIONS)
        return []
    return []


def complete(ref: Mapping[str, Any], argument: Mapping[str, Any]) -> dict[str, Any]:
    """What an argument may still be, given what has been typed of it.

    Matched case-insensitively on the prefix, because every vocabulary here is upper case
    or numeric and a person typing a datum types navd88. An unknown reference completes to
    nothing rather than failing: the specification has a client call this speculatively,
    and a server that raised would make offering completion a risk.
    """
    typed = str(argument.get("value") or "")
    values = [v for v in _vocabulary(ref, str(argument.get("name") or "")) if _starts(v, typed)]
    return {
        "completion": {
            "values": values[:COMPLETION_LIMIT],
            "total": len(values),
            "hasMore": len(values) > COMPLETION_LIMIT,
        }
    }


def _starts(value: str, typed: str) -> bool:
    return value.lower().startswith(typed.lower())


__all__ = [
    "COMPLETION_LIMIT",
    "CONVERTIBLE_DATUMS",
    "COVERAGE",
    "PROMPTS",
    "RESOURCES",
    "RESOURCE_TEMPLATES",
    "SCHEME",
    "UnknownPrompt",
    "UnknownResource",
    "complete",
    "prompt",
    "resource",
]
