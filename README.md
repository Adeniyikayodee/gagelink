# GageLink

**Hydrology data AI can trust**

River levels, flows, forecasts, and flood data, with the context AI needs to use them correctly.

mcp-name: io.github.Adeniyikayodee/gagelink

**Pre-alpha. The API may change.**

## What it does

GageLink gives AI agents access to hydrology data from USGS, NOAA, Hub'Eau, the UK Environment Agency, and SWOT.

Every value keeps its:

* Unit
* Datum
* Timezone
* Data quality
* Provisional or approved status

This helps AI use water data without silently mixing incompatible numbers.

## What can it answer?

* How high is the river?
* Is it above flood stage?
* How much freeboard is left?
* What is the current flow?
* How does it compare with the record peak?
* What is the forecast?
* Will the river reach a flood level?
* What is upstream or downstream?
* How large is the drainage basin?
* Has a historical measurement been revised?
* Is a reading provisional or approved?

## The problem

Water data often looks simple:

`3.02 ft`

But that number is not enough.

A river stage may be measured from a local gage datum. A surveyed elevation may use NAVD88. Both are measured in feet, so a normal unit checker will think they can be subtracted.

They cannot.

GageLink refuses unsafe calculations instead of giving a plausible-looking answer. It provides the information needed to make the calculation correctly.

## Why this matters

AI agents can make simple mistakes with physical data.

For example, a model can use:

`3010 ft³/s`

when the tool expects:

`2.95 kcfs`

Or it can subtract a gage height from an elevation that uses a different datum.

These errors can produce answers that look reasonable but are wrong.

GageLink keeps the physical context attached to the number and checks it when the agent uses the data.

## How it works

A normal API gives an agent a number.

GageLink gives the agent a number **with its meaning**.

For example:

```text
1.35 ft³/s
provisional

9.11 ft
GAGE:06730500
provisional
```

A stage can also be converted to another datum when the required information is available.

If it is not available, GageLink refuses the conversion instead of guessing.

## Tools

GageLink provides tools for:

| Tool                   | What it does                          |
| ---------------------- | ------------------------------------- |
| `find_locations`       | Find monitoring stations              |
| `describe_location`    | Get station metadata                  |
| `get_latest`           | Get the latest readings               |
| `get_series`           | Get historical data                   |
| `slice_series`         | Work with part of a series            |
| `get_peaks`            | Get annual peak flows                 |
| `get_forecast`         | Get forecasts and flood thresholds    |
| `get_model_forecast`   | Get modelled flow for ungaged reaches |
| `get_satellite_passes` | Get water levels measured from orbit  |
| `navigate_network`     | Find upstream or downstream stations  |
| `get_basin`            | Get the drainage basin                |
| `lookup_parameter`     | Understand parameter codes            |
| `export_manifest`      | Export what answered a question       |

All tools are read-only.

## Built for AI agents

GageLink can run as an MCP server.

```bash
gagelink-mcp
```

To use it from an MCP client, with nothing installed:

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

It can also run over HTTP for clients that cannot start a local process.

The tools return structured data, so units, datums, quality, and other metadata are fields rather than text an agent has to guess from.

## Beyond the US

GageLink currently supports:

* **US:** USGS, NOAA and NLDI
* **France:** Hub'Eau
* **UK:** Environment Agency
* **Global:** SWOT and selected datasets such as ERA5, GRACE and HydroBASINS

Support differs by service. For example, the UK Environment Agency currently provides location and reading tools, while the US services provide the wider set of forecasting, basin and network tools.

## A simple example

Suppose an agent wants to calculate flood protection:

```text
River stage: 3.02 ft
Levee crest: 41 ft NAVD88
```

GageLink does not simply subtract the two.

It first checks the reference frames.

The gage zero is 37.04 ft NAVD88, so:

```text
Stage = 3.02 + 37.04
      = 40.06 ft NAVD88

Freeboard = 41 - 40.06
          = 0.94 ft
```

Without the datum conversion, the answer would be **37.98 ft**, which is wrong by a factor of 40.

## Replayable results

GageLink records the data used to produce an answer.

A session can be saved and replayed later.

There are three modes:

* `offline` — use the saved data
* `strict` — check whether the live data is identical
* `revision_aware` — check whether the difference came from an official data revision

This matters because hydrology data can change. A provisional measurement may later be revised or approved.

## Benchmark

GageLink includes **waterbench**, a benchmark for testing whether better data interfaces help AI models work with hydrology data.

It compares:

* Raw API data
* Structured data without metadata
* Structured data with units, datums, quality and other context

In the first test with gpt-oss-120b:

| Condition               | Correct |
| ----------------------- | ------: |
| Raw API                 |   61/72 |
| Structured, no metadata |   63/72 |
| GageLink                |   70/72 |

The benchmark is small and uses one model, so these results are an early signal rather than a general claim about model performance.

## Getting started

Install it:

```bash
pip install gagelink
```

Or run the MCP server:

```bash
gagelink-mcp
```

No account is needed to start. A free USGS key increases the limit from 50 to 1,000 requests per hour.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests use recorded responses, so they do not need a live network connection.

## License

MIT
