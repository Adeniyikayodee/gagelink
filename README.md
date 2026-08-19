# gagelink

Hydrology retrieval for AI agents. Values arrive carrying their unit, the datum they are
measured from, their timezone, and whether the record is provisional or approved, and every
request is recorded in a form that lets a session be re-run and its differences attributed.

**Pre-alpha.** The transport layer against the USGS Water Data APIs is present. The tool
surface, normalisation into typed quantities, and replay are not. The API will change.

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
