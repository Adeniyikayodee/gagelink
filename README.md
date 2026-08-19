# gagelink

Hydrology retrieval for AI agents. Values arrive carrying their unit, the datum they are
measured from, their timezone, and whether the record is provisional or approved, and every
request is recorded in a form that lets a session be re-run and its differences attributed.

**Pre-alpha.** Transport, normalisation into typed quantities, and the USGS and forecast
tools are present. Network navigation, the MCP server, and replay are not. The API will
change.

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
| `lookup_parameter` | resolve a parameter code, since readings carry no name |

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
