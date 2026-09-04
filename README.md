# GageLink

Hydrology data for AI agents, with the reference frames kept attached.

River levels, streamflow, flood forecasts, drainage basins and satellite water levels from
USGS, NOAA, Hub'Eau, the UK Environment Agency and SWOT. Every value arrives carrying its
unit, the datum it was measured from, its timezone, and whether the record is provisional or
approved.

mcp-name: io.github.Adeniyikayodee/gagelink

**Pre-alpha. The API may change.**

## Install

`pip install gagelink` for the library. From an MCP client, with nothing installed:

```json
{
  "mcpServers": {
    "gagelink": {
      "command": "uvx",
      "args": ["--from", "gagelink", "gagelink-mcp"]
    }
  }
}
```

Or open the [`.mcpb` bundle](https://github.com/Adeniyikayodee/gagelink/releases/latest),
which carries the server and its dependencies in one file.

No account is needed. A [free key](https://api.waterdata.usgs.gov/signup) raises the
allowance from 50 requests an hour to 1,000; set it as `GAGELINK_API_KEY`.

## What can it answer?

* How high is the river, and how does that compare with flood stage?
* How much freeboard is there between the water and a surveyed levee crest?
* What is the flow now, and what fraction of the record peak is that?
* What is forecast over the next few days, and does it cross a flood category?
* What lies upstream or downstream along the river network?
* How large is the basin draining to this point?
* What did a station record over a date range, and has that record been revised?
* What is the water surface elevation of a river with no gage on it?
* Is a reading provisional or approved, and how old is it?

## Why the frames matter

At Little Falls on the Potomac, a river stage of `3.02 ft` is measured upward from the
gage's own zero. A surveyed levee crest of `41 ft` is measured upward from a national datum.
Both are lengths in feet, so subtracting one from the other produces a number that reads as
freeboard, and a units library will pass it.

The gage zero at this station sits 37.04 ft above NAVD88, so the stage is 40.06 ft on that
datum and the freeboard is 0.94 ft. Subtracting without the offset gives 37.98 ft, which
overstates the margin by a factor of 40 in the direction of calling a levee safe.

GageLink refuses that subtraction and returns the offset that makes it well defined. The
same applies to satellite elevations, which sit on a geoid, and to modelled flows, which may
have no measurement behind them.

`python demo/freeboard.py` runs the whole example offline from recorded responses.

## Converting a datum

The offset is available for most stations, so the refusal can become an answer. Pass
`on_datum` to `describe_location` and the station's offset is converted through NOAA's
VDatum, with the uncertainty of the conversion returned beside it:

```text
altitude_of_gage_datum        4860 ft (NGVD29)      Boulder Creek at mouth, CO
altitude_accuracy             10 ft, interpolated from a topographic map
altitude_on_requested_datum   4863.061 ft (NAVD88)
conversion_uncertainty        0.17 ft
offset_uncertainty            10 ft
```

Two things this surfaces are easy to miss.

**The offset has an accuracy of its own.** Across 7,361 USGS stream stations sampled in four
states, 3,397 publish an altitude for their gage datum. Of those, 72% are known no better
than a foot. The commonest published accuracy is 15 ft, a third were interpolated from a
topographic map, and about one in twenty is levelled to a hundredth. A freeboard is bounded
by that figure whatever precision the stage was read to, so `describe_location` returns it
alongside the method used to determine it.

**Most stations are on the older datum.** 58% of those altitudes are published on NGVD29
while a modern survey or lidar product is on NAVD88. Across the contiguous states the
difference runs to feet.

`on_datum` also takes the tidal datums (`MLLW`, `MLW`, `LMSL`, `MTL`, `DTL`, `MHW`, `MHHW`)
for questions about level relative to the tide, and `get_satellite_passes` takes it to move
SWOT elevations off the EGM2008 geoid they are measured against. Both cover the contiguous
United States. Outside that coverage the conversion is refused and the reason is stated.

## Tools

| Tool                   | What it does                          |
| ---------------------- | ------------------------------------- |
| `find_locations`       | Find monitoring stations              |
| `describe_location`    | Station metadata and reference frames |
| `get_latest`           | The latest reading for each parameter |
| `get_series`           | A time series over a date range       |
| `slice_series`         | Work with part of a retrieved series  |
| `get_peaks`            | Annual peak flows                     |
| `get_forecast`         | Forecasts and flood thresholds        |
| `get_model_forecast`   | Modelled flow for ungaged reaches     |
| `get_satellite_passes` | Water levels measured from orbit      |
| `navigate_network`     | Upstream and downstream stations      |
| `get_basin`            | The contributing drainage basin       |
| `lookup_parameter`     | Resolve a parameter code              |
| `export_manifest`      | Everything that answered the question |

All thirteen are read-only and annotated as such, so a client asks for consent once.

Results come back as structured data against each tool's output schema, so a unit, datum or
grade is a field the client can read directly.

A series is returned as a handle with a summary. A year of 15-minute record is 35,000
values, and no answer needs them in a context window.

## Prompts

A tool list says what can be called. It does not say what to call first, and the datum rule
above is an order of operations rather than a call. Four prompts state the ones that go
wrong when a model assembles them itself.

| Prompt                | What it walks through                                    |
| --------------------- | -------------------------------------------------------- |
| `freeboard_check`     | Fetch the offset, then difference, then bound the answer |
| `flood_status`        | Stage against flood category, now and over the forecast  |
| `find_a_station`      | The filter the relevant agency actually matches on       |
| `reproducible_answer` | Answer, then export the manifest                         |

## Resources

The tables that do not change between calls, readable without spending a request against
the hourly allowance.

| Resource                 | What is in it                                        |
| ------------------------ | ---------------------------------------------------- |
| `gagelink://instructions` | The four rules that decide whether an answer is right |
| `gagelink://parameters`   | The common parameter codes and what each measures     |
| `gagelink://datums`       | Every datum named here, and which can be converted onto |
| `gagelink://coverage`     | Which tools answer for which country, and what each service omits |
| `gagelink://manifest`     | This conversation's ledger, without a tool call       |

`gagelink://parameter/{code}` and `gagelink://datum/{name}` are templates, and their
variables complete: the server answers `completion/complete` for them and for the prompt
arguments with a closed set of values. The specification scopes completion to prompt
arguments and resource template variables, which is why those two vocabularies are exposed
as templates as well as inside the tool schemas.

## Coverage

| Region | Services | Available |
| ------ | -------- | --------- |
| United States | USGS, NOAA NWPS, NOAA National Water Model, NLDI, VDatum | All thirteen tools |
| France | Hub'Eau | Search, metadata, latest readings, time series |
| United Kingdom | Environment Agency | Search, metadata, latest readings |
| Global | SWOT | Satellite water surface elevation |

ERA5, GRACE, CAMELS and HydroBASINS are available to library callers.

Each service publishes a different amount, and the tools say which. Hub'Eau states no unit
on any value, so levels in millimetres and flows in litres per second are labelled here from
a recorded table. The Environment Agency publishes no record grade on live data, so age is
the only staleness signal for a UK reading.

To find a UK station, `find_locations` takes `country=GB`. The agency matches river and town
in full and in its own spelling, so `River Thames` returns stations and `Thames` returns
none. Free text matched against the station name is the filter to use when the agency's
spelling is unknown.

## Protocol support

GageLink serves MCP revision `2026-07-28` and the three handshake revisions before it
(`2025-06-18`, `2025-03-26`, `2024-11-05`). It declares tools, prompts, resources and
completions, and declares `listChanged` false on all of them: every list is built at
import, so a client that subscribed would be waiting on a notification that cannot come.

The 2026 revision removed the `initialize` handshake. Every request carries its own version
and capabilities, so a client calls a tool on its first message and learns what the server
is through `server/discover`. Clients on the earlier revisions continue to open a session
and keep it.

Because a connection no longer implies a conversation, a client that wants a ledger of its
own names one in `_meta`:

```json
{"_meta": {"io.github.adeniyikayodee.gagelink/conversation": "whatever-you-call-it"}}
```

Each name gets its own manifest, quantities and checks. A client that sends no name shares
the default.

For clients that cannot start a local process:

```bash
gagelink-mcp --http          # http://127.0.0.1:8765/mcp
```

This binds to loopback and checks the `Origin` header. It has no authentication, so
`--host` on a reachable interface gives away your hourly allowance.

## Reproducible answers

Every retrieval is recorded with its URL, the time it was made, and a hash of the response
body. `export_manifest` returns that record, and a session can be replayed later in three
modes:

* `offline` uses the archived bodies
* `strict` checks the live service returns identical data
* `revision_aware` separates a changed answer caused by an official record revision from one
  caused by changed code

The third mode exists because hydrology data is revised. A provisional measurement is often
approved or corrected months later, so an answer can change for reasons that have nothing to
do with the code. `revision_aware` tells the two causes apart.

Values are also checked against the ledger, so an answer can be audited:

```text
[ok]         3.02 ft        from get_latest.00065
[ok]         2960 ft3/s     from get_latest.00060
[UNSOURCED]  116000 ft3/s   no tool output produced this value
```

## Benchmark

`waterbench` measures whether the interface changes what a model gets right. It runs the
same nine tasks under three conditions: raw API responses, structured results with the
metadata stripped, and the full toolkit.

First results, gpt-oss-120b, eight replicates, 216 runs:

| Condition               | Correct |
| ----------------------- | ------: |
| Raw API                 |   61/72 |
| Structured, no metadata |   63/72 |
| GageLink                |   70/72 |

Six of the nine tasks sit at ceiling, which is a finding about the suite. Where it
separates, the causes are legible. Two long-record tasks sent 49,864 and 42,006 prompt
tokens through raw JSON against 5,462 and 2,384 through the toolkit. On the opaque-unit
task, stripping the reference frames sent seven of eight runs into the recorded trap,
answering with the USGS
discharge of 3010 ft³/s where the forecast service had published 2.95 kcfs.

One model and a small suite, so these numbers are an early signal about the interface. A
general claim would need more models and more tasks.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Requires Python 3.10 or later. The suite answers from recorded fixtures and needs no network
access. `mypy src/gagelink` is expected to be clean.

## License

MIT
