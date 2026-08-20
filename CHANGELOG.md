# Changelog

## 0.2.0

**Findable and runnable by an agent.** The server now sends `instructions` at handshake,
which clients put in front of the model before it calls anything, carrying the four things
that decide whether an answer is right rather than a description of the software: that a
gage height is not an elevation, that latest is not current, that modelled is not measured,
and that units are not interchangeable across these services. A `server.json` registers it
with the MCP registry, an `llms.txt` summarises what it answers, and the README leads with a
copy-paste configuration and the questions it covers.

`uvx --from gagelink gagelink-mcp` runs it with no install and no account. The only
environment variable is optional and raises a rate limit rather than gating the server, which
is checked by a test, because anything required at startup is a reason an agent never gets
past the handshake.

**The remaining source families**, which completes the set named in the original design.

*National Water Model*, through the same host as the gauge forecasts, so a station resolves
to its reach through the gauge it already carries. Modelled series are kept apart from
observations in their own type, because the model covers reaches with no gauge on them and a
figure from it may have nothing measured behind it.

*SWOT*, through NASA's Hydrocron, covering the reaches no gauge stands on. Elevations are
referenced to the EGM2008 geoid and are refused against a stage or a survey, since the offset
varies with position and nothing publishes it.

*ERA5*, through the Copernicus store, which is genuinely queued. A synchronous source is
fetched with `get_` and a queued one is asked with `request_`, polled, and resolved. An agent
cannot block on an hour-long queue inside a tool call, so the two shapes are named apart.

*GRACE*, through NASA's metadata repository, which is not queued. Discovery is open and
retrieval is not: the data file answers 401 while the checksum beside it answers 206. That
boundary is tested.

*CAMELS and HydroSHEDS*, which have no service behind them at all and are read from a local
copy. Both readers are written to the published format specifications and exercised against
files built to those specifications; neither has been run against the real distribution.
HydroBASINS is read from the dBase table beside the shape file, which needs no geospatial
dependency and does not open the geometry.

Four spellings of one unit now appear across two agencies: `cfs`, `kcfs`, `ft^3/s`, and
`ft³/s`. Mapping `kcfs` to `kilofoot**3/second` cubes the prefix and is out by a factor of
10⁹, which is the error this package exists to prevent arriving in its own unit table. It is
pinned by a test.

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
