# gagelink

Hydrology retrieval for AI agents. Values arrive carrying their unit, the datum they are
measured from, their timezone, and whether the record is provisional or approved, and every
request is recorded in a form that lets a session be re-run and its differences attributed.

**Pre-alpha.** Transport against the USGS Water Data APIs and normalisation into typed
quantities are present. The tool surface and replay are not. The API will change.

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
