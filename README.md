# gagelink

Hydrology retrieval for AI agents. Values arrive carrying their unit, the datum they are
measured from, their timezone, and whether the record is provisional or approved, and every
request is recorded in a form that lets a session be re-run and its differences attributed.

**Pre-alpha.** The API will change.

```bash
pip install gagelink
```

## Why

Water services already publish everything needed to use their data correctly. A discharge
states its unit, a stage states the datum it is measured from, a reading states whether it
is provisional or approved, and a timestamp states its offset. Clients typically parse the
number and drop the rest, and the errors follow from that.

The failure is measurable. In a benchmark of 4,288 runs across eleven models,
[`quantity-guard`](https://github.com/Adeniyikayodee/quantity-guard) found that every model
reaching the computing tool sent a discharge published in cubic feet per second into a
parameter declared in cubic metres per second without converting it, on nearly every run,
giving an answer 35.3 times too large with nothing in the output to indicate it. Seven of
eleven differenced a stage on a local gage datum against an elevation on NAVD88 and reported
the result as freeboard.

`gagelink` retrieves the data with the metadata kept, and uses `quantity-guard` to enforce
it where the agent's tools are called.

## Current surface

```python
from gagelink import Service

service = Service(api_key="...")           # free key, see below
page, retrieval = service.items(
    "latest-continuous",
    monitoring_location_id="USGS-07374000",
    parameter_code="00060",
)

retrieval.record()      # what a replay needs: url, params, time, status, sha256
retrieval.quota         # Quota(limit=1000, remaining=999)
```

`items` returns the parsed page and the record of having fetched it together, rather than
the page alone, because a number that reaches an answer without the request that produced it
cannot be replayed, and pairing them at the only entry point is cheaper than remembering to
record it.

Payloads become quantities that carry their own reference frames:

```python
from gagelink import location_from, readings_from

page, _ = service.items("monitoring-locations", id="USGS-06730500")
station = location_from(page["features"][0])
station.register()                       # its datum, and the offset where one is published

observations, _ = service.items("latest-continuous", monitoring_location_id=station.id)
readings = {r.parameter_code: r for r in readings_from(observations, station)}

readings["00060"].value       # Q(1.35 ft³/s (provisional))
readings["00065"].value       # Q(9.11 ft (GAGE:06730500, provisional))
readings["00065"].value.to_datum("NGVD29")   # Q(4869.11 ft (NGVD29, provisional))
readings["00065"].value.to_datum("NAVD88")   # DatumConversionUnavailable
```

That last line is the point. Boulder Creek publishes its altitude on NGVD29, so a stage
there resolves onto NGVD29 and refuses NAVD88, since the offset between the two varies with
location and is not published here. Assuming the modern datum because it is the modern datum
is a freeboard error one step earlier than the one anybody looks for.

### What is not published, and what is done about it

`altitude` and `drainage_area` come back as bare numbers, and the collection schema states
no unit for either, so the USGS conventions of feet and square miles are applied in
`normalise.py` where they are visible rather than assumed further downstream.

A unit with no mapping is refused rather than guessed. A unit that pint can parse but that
this package has no entry for is allowed through with a warning, because parseable is not
the same as understood: `ppt` reads as parts per trillion to pint and means parts per
thousand to USGS, which is a factor of 10^9 between two dimensionally identical readings.

A missing value is `null` here rather than the -999999 that WaterServices published, and it
stays missing. The qualifier says why, `["EQUIP"]` for an equipment outage.

Approval arrives as `Provisional` or `Approved` rather than as `P` or `A`, and condition
codes grade below their review status, so approved record of an ice-affected measurement
grades as unverified rather than as approved.

The station timezone is resolved from the abbreviation together with the daylight saving
flag, since MST without daylight saving is Arizona and MST with it is Colorado, and they
differ by an hour for eight months of the year.

## Tools

A session holds the state for one question and the record of what answered it. Tools return
a result rather than raising, because a failure carrying a repair keeps a model in the
conversation where it can correct itself, and a raised exception ends the turn.

```python
from gagelink import Session, Toolkit

with Session(question="How high is the Potomac at Little Falls?") as work:
    kit = Toolkit(work)
    kit.describe_location("USGS-01646500")
    kit.get_latest("USGS-01646500", parameters=["00060", "00065"], max_age_hours=6)

    work.audit("The gage height is 3.02 ft and the discharge is 2960 ft3/s.")
    work.manifest()
```

Every value leaves with its frame attached, and every one is entered in a ledger, so an
answer can be checked against what was actually retrieved:

```
[ok]         3.02 ft        from get_latest.00065
[ok]         2960 ft3/s     from get_latest.00060
[UNSOURCED]  116000 ft3/s   no tool output produced this value
```

The third line is the check earning its place. The figure is a plausible discharge for that
river, it is wrong, and nothing about the sentence containing it indicates as much.

A series is returned as a handle with a summary and a twenty-point sample rather than as its
points, since a year of 15-minute record is 35,000 values. The handle is derived from the
query that produced it, so a replay of the same session produces the same handle. Results
are budgeted, and anything dropped to stay inside the budget is stated in the result, since
a silent truncation reads as coverage.

| tool | purpose |
|---|---|
| `find_locations` | search by state, county, hydrologic unit, site type, or bounding box |
| `describe_location` | metadata, datum, timezone, and the offset a stage needs |
| `get_latest` | most recent value per parameter, with age and quality |
| `get_series` | a date range, as a handle plus a summary |
| `slice_series` | narrow a stored series without fetching again |
| `get_peaks` | annual peak flow record |
| `get_forecast` | observed and forecast stage, with flood thresholds |
| `navigate_network` | monitoring locations upstream or downstream along the river |
| `get_basin` | the area draining to a point |
| `lookup_parameter` | resolve a parameter code, since readings carry no name |

## As an MCP server

```bash
export GAGELINK_API_KEY=...        # free, see below
gagelink-mcp
```

```json
{"mcpServers": {"gagelink": {"command": "gagelink-mcp"}}}
```

Eleven tools, no more. A model degrades as its tool list grows, so the surface is organised
by verb and the choice of which service answers is made by the server rather than put to the
caller.

The tool descriptions are part of the product rather than documentation of it. In the
quantity-guard evaluation, declaring physical metadata in the schema without enforcing it
still recovered a third of the runs that failed at baseline, so what a description says about
datums, units, and provisional record does work before any validation runs.

A tool failure comes back as content marked in error rather than as a protocol fault, which
keeps the repair in front of the model instead of ending the turn. The session resets on
`initialize`, so one conversation's quantities cannot appear in another's manifest.

## Freeboard, which is where the hazards meet

`python demo/freeboard.py` runs the whole thing offline from recorded responses:

```
stage      3.02 ft (GAGE:01646500)
crest      41 ft (NAVD88)

The two are both lengths, so nothing dimensional separates them:
  refused: cannot difference an elevation on NAVD88 against one on GAGE:01646500

The gage's zero is at 37.04 ft NAVD88, so the stage is 40.06 ft (NAVD88).
  freeboard = 0.94 ft

Ignoring the datum gives 37.98 ft of margin where 0.94 ft is correct,
overstating it by a factor of 40.
```

A stage and a surveyed elevation are both lengths in feet, and subtracting one from the
other returns a number that looks like a freeboard. The error is silent, it runs in the
direction of reporting a levee as safe, and no units library prevents it because nothing
about the units is wrong.

## Forecasts

Flood thresholds come from the NOAA National Water Prediction Service, since a stage means
nothing until it is set against the stage at which the river floods. Three things in that
payload need handling and none is signposted:

Flow appears as `cfs` in the flood categories and as `kcfs` in the status block of the same
response, so a caller reading both and treating them alike is out by a factor of a thousand.

Thresholds that were never set are published as `-9999` rather than omitted. That is
dimensionally valid, plausible in sign only, and passes every check downstream, so it is
read as the sentinel it is.

Stages are on the gage's own datum, not a national one. The observed stage published there
matches USGS parameter 00065 at the same station and time exactly, which is the evidence for
that reading, and it is why a flood stage differences against a gage height but not against
a surveyed elevation.

## The river network

Navigation runs along the river rather than within a radius, which is the distinction that
makes the answer useful: a gage two miles away on the next catchment is upstream of nothing
here. Directions are words rather than the index's two-letter codes, so `upstream` includes
tributaries and `upstream_main` follows the main stem alone.

A basin arrives as a polygon of a couple of thousand coordinate pairs. That is the answer to
a mapping question and the wrong answer to every question an agent asks, so the polygon is
kept and its area, extent, and vertex count are reported. The area is computed from the
polygon by the line-integral form of the spherical area, which needs no projection and so
has no zone to choose or to get wrong. It agrees with the drainage area USGS publishes for
the one station where both figures exist to 0.06%, and the result says it is computed rather
than published, so it is not quoted against a surveyed figure as though the two were the
same.

## Replay

Hydrology reproduces at 1.6% in the tested literature. The usual explanation is that data
and code were not published, and it hides the more interesting failure: a published pipeline
against a live service does not reproduce either, because the service revised the record
underneath it. Provisional discharge becomes approved discharge and the number moves.

A session saves a bundle of its manifest and the response bodies it saw. Replaying it runs
in three modes, and the distinction between them is the point.

| mode | procedure | isolates |
|---|---|---|
| `offline` | recompute from the archived bodies | code and library change |
| `strict` | re-fetch, require identical responses | any drift at all |
| `revision_aware` | re-fetch, diff, ask the service to account for each difference | data revision |

```python
with Session(question="what was the discharge in mid May 2021?") as work:
    Toolkit(work).get_series("USGS-02344872", "00060", "2021-05-16", "2021-05-20")
    work.save("bundle.json")
```

```bash
gagelink-replay bundle.json --mode strict
gagelink-replay bundle.json --mode revision_aware
```

The same bundle, the same re-fetch, and two different verdicts:

```
strict replay: changed
  changed      daily
      [changed] USGS-02344872 00060 at 2021-05-16: 702.1 -> 826.0 ft^3/s

revision_aware replay: reproduced
  changed      daily
      [revised] USGS-02344872 00060 at 2021-05-16: 702.1 -> 826.0 ft^3/s,
                Revisions: Discharge for the period May 16, 2021 to Oct. 27, 2021,
                was revised on Aug. 16, 2024, based on changes to the estimated discharge.
```

A result that changed because the agency revised 400 provisional values is a different fact
about the science than one that changed because the code changed, and the two are otherwise
indistinguishable. The revision record comes from the service's own `time-series-revisions`
collection, joined on the time series identifier the reading already carries, so the
attribution is a lookup rather than a guess. A difference with no published revision behind
it stays reported as unexplained, which is what keeps the check from being vacuous.

Bodies are verified against their hashes before anything is compared. A bundle whose archive
does not match is refused rather than replayed, since every verdict rests on the archive
being what the session actually saw.

## waterbench

`bench/` is a benchmark measuring what the toolkit is worth to a model, over nine tasks at
one station covering nine hazards, every one of them observed in a live service payload
while the package was built.

Three conditions are compared on identical data, differing only in the interface between
the model and the bytes:

| condition | what the model gets |
|---|---|
| `http_only` | one fetch tool returning the service's own JSON, which is what a developer has today |
| `toolkit_plain` | the eleven tools with results stripped to bare magnitudes and the notes removed |
| `toolkit` | the tools as they are, with units, datums, quality, staleness, and the notes |

The middle condition is what makes the measurement worth taking. Without it, a difference
between the first and the last would only show that structured retrieval beats raw JSON,
which nobody doubts. The difference between the last two is what the metadata is worth on
its own.

```bash
python -m bench --dry-run                                   # no provider, no spend
python -m bench --model anthropic/claude-opus-5 --replicates 4
```

Every expected answer is derived from the same recorded responses the tools serve, and
`tests/test_bench.py` solves each task from those responses and checks the result against
the declared answer. A task whose answer cannot be reached that way is a broken task, and
that is where it shows up. Each task also records a `basis` saying where its figure comes
from, so a reader can check it without taking this project's word for it.

The freeboard task is checkable against the agency's own arithmetic: USGS publishes the
water surface elevation as parameter 63160, at 40.07 ft NAVD88, which is the gage height of
3.03 ft plus the station's datum offset of 37.04 ft.

Scoring is written before any sweep runs and is in version control, so a rule cannot be
adjusted after seeing a result it disfavours.

### First results

gpt-oss-120b, nine tasks, three conditions, eight replicates, 216 runs, $0.09.

| condition | correct |
|---|---|
| `http_only` | 61/72 |
| `toolkit_plain` | 63/72 |
| `toolkit` | 70/72 |

Correctness counts every run, including the eight that ended without an answer at all. Seven
of those belong to `http_only` on the two tasks whose raw record runs to 42,000 and 50,000
prompt tokens, where the model degenerates into repeating a number instead of answering.
Those failures are caused by the condition, so excluding them would credit raw JSON for the
runs its own payload size destroyed.

The suite sits at ceiling on six tasks of nine, which is a finding about the suite. Where it
separates:

| task | `http_only` | `toolkit_plain` | `toolkit` |
|---|---|---|---|
| `peak_fraction`, a long record | 3/8 | 8/8 | 8/8 |
| `record_peak`, a long record | 6/8 | 8/8 | 8/8 |
| `forecast_flow`, an opaque unit | 8/8 | 1/8 | 7/8 |

On the two long-record tasks the median prompt was 49,864 and 42,006 tokens through raw JSON
against 5,462 and 2,384 through the toolkit. On the opaque-unit task, stripping the reference
frames sent seven of eight runs into the recorded trap, answering with the USGS discharge of
3010 ft³/s rather than the forecast service's 2.95 kcfs.

The toolkit does not beat raw JSON on accuracy in general. The gap is driven almost entirely
by the two tasks where the raw payload will not fit. Eight of 27 cells split across their
replicates, so differences under roughly two runs in eight are not separated by this design,
and one model is one model.

## API keys and rate limits

The service allows 50 requests per IP per hour unauthenticated and 1,000 per hour with a
key, which is free from
[api.waterdata.usgs.gov/signup](https://api.waterdata.usgs.gov/signup). A single agent
question comparing conditions at five sites costs roughly 15 to 25 requests, so caching is
load-bearing rather than an optimisation and responses are cached for the life of the
process by default.

The remaining allowance is read from `X-RateLimit-Remaining` on every response and carried
on the retrieval, so an agent can be told what it has left rather than discovering the limit
by failing.

The key travels in the `X-Api-Key` header and never appears in a recorded URL, since a
manifest is meant to be publishable.

## The service this targets

USGS is decommissioning the WaterServices API family, with decommission scheduled for the
first quarter of 2027 and degradation possible from the second half of 2026. `gagelink`
targets the replacement at `api.waterdata.usgs.gov/ogcapi/v0` only. One consequence worth
naming is the `time-series-revisions` collection, which publishes changes and deletions to
approved record, and which is what will let a replay separate an answer that changed because
the agency revised a measurement from one that changed because the code changed.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Network access goes through a replaceable `fetch`, so the suite runs against recorded
responses and no test needs the network.

## License

MIT
