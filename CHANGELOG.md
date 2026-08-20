# Changelog

## 0.1.0

Everything above the transport layer, which is to say everything the package is for.

**Quantities carry their own frames.** Service payloads become values labelled with their
unit, the datum they are measured from, their timezone, and whether the record is
provisional or approved. Two fields are published with no unit at all, `altitude` and
`drainage_area`, and the collection schema states none for either, so the USGS conventions
of feet and square miles are applied where they can be seen rather than assumed downstream.

**Eleven tools, organised by verb rather than by agency**, over USGS, the NOAA National
Water Prediction Service, and the river network index. A series returns a handle with a
summary rather than its points, because a year of 15-minute record is 35,000 values.
Results are budgeted, and anything dropped to stay inside the budget is stated, since a
silent truncation reads as coverage. Every failure carries a repair string.

**An MCP server**, `gagelink-mcp`, and a replay command, `gagelink-replay`.

**Replay in three modes.** `offline` recomputes from archived bodies and isolates code
change. `strict` re-fetches and requires identical responses. `revision_aware` re-fetches,
diffs value by value, and asks the service whether each difference falls inside a revision
it has published, joining on the time series identifier the reading already carries.
Demonstrated against record USGS revised in August 2024, where the same bundle and the same
re-fetch give `changed` under one mode and `reproduced` under the other.

**A benchmark**, `bench/`, measuring what the toolkit is worth to a model over nine hazards
in three conditions. It is not in the wheel.

### Things the services do that this handles

Flow is published as `cfs` in a flood category and as `kcfs` in the status block of the same
response. Thresholds never set are published as `-9999`. Stages are on the gage's own datum
and not a national one. A daily value carries a date and not an instant. `latest-continuous`
pages at ten and publishes no match count, so a station's discharge can be missing from a
listing that reads as complete. A text query on `parameter-codes` is ignored silently,
answering 200 with the wrong rows.

**TLS works without setup.** A Python installed outside the system keychain, which is the
default for a python.org build on macOS, ships with no trust store, and every HTTPS call
fails verification. The certificates now come from certifi rather than from whatever the
interpreter happened to be installed with.

### Known limits

The suite of nine benchmark tasks sits at ceiling on six of them for the one model tested,
so it does not yet discriminate. Coverage is instantaneous, daily, peak, and site record
from USGS, gauges from NWPS, and navigation and basins from the network index. Nothing is
asynchronous, and no gridded or global source is included.

## 0.0.1

Transport against the USGS Water Data APIs, with caching, quota accounting, and a record of
every request sufficient to replay it.
