# Design history

Why this package is shaped the way it is, and what forced each decision. Written for
someone deciding whether to depend on it, or to change it, and needing to know which parts
are load-bearing and which are arbitrary.

The short version: almost nothing here was decided in the abstract. The interesting
decisions were made by reading what the services actually publish, discovering that it
differs from what their documentation says, and then designing around the difference.
Where a decision was reversed by evidence, the reversal is recorded rather than tidied
away, because a design history that shows only the surviving choices is a sales document.

## The problem

Water services publish everything needed to use their data correctly. A discharge states
its unit. A stage states the datum it is measured from. A reading states whether the record
is provisional or approved. A timestamp states its offset. Clients parse the number and
drop the rest, and the errors follow from that.

The failure is measurable rather than theoretical. In a benchmark of 4,288 runs across
eleven models, [`quantity-guard`](https://github.com/Adeniyikayodee/quantity-guard) found
that every model reaching the computing tool sent a discharge published in cubic feet per
second into a parameter declared in cubic metres per second without converting it, on
nearly every run. The answer is 35.3 times too large and nothing in the output indicates
it. Seven of eleven differenced a stage on a local gage datum against an elevation on
NAVD88 and called the result freeboard.

This package is the retrieval half of that: get the data with the metadata kept, and hand
it to the boundary checks rather than reimplementing them.

## Scope

### A separate package, not a subpackage

`gagelink` depends on `quantity-guard` and does not live inside it. Three reasons, in order
of weight.

The dependency footprints diverge. `quantity-guard` depends on `pint` and its adoption case
rests on staying that way. This needs HTTP, certificates, and caching, and would have needed
array and geometry handling had the gridded sources been read rather than downloaded.

The release cadences diverge. A boundary-enforcement library should be stable. A client
tracking a service in active migration will not be.

The audiences diverge. The core carries adoption; this carries the domain credibility.

### Which sources, in what order

Six families were named in the original design. They were built in order of how much could
be verified, not in order of how impressive they sound, and that ordering surfaced a
mistake in the plan. See "GRACE is not queued" below.

| source | shape | why it was reached for |
|---|---|---|
| USGS Water Data | synchronous JSON | the observations everything else is checked against |
| NOAA NWPS | synchronous JSON | a stage means nothing without a flood threshold |
| USGS NLDI | synchronous JSON | upstream means along the river, not within a radius |
| NOAA NWM | synchronous JSON | the reaches no gauge stands on |
| SWOT | synchronous JSON | the rivers no gauge stands on, measured from orbit |
| ERA5 | queued, credentialed | precipitation for a catchment with no gauge in it |
| GRACE | open search, credentialed download | the water nobody can see |
| CAMELS, HydroSHEDS | local files | no service exists |

### Building against a service mid-migration

USGS is decommissioning the WaterServices API family, with decommission scheduled for the
first quarter of 2027. Everything here targets the replacement at
`api.waterdata.usgs.gov/ogcapi/v0` only.

That is a risk and it is also the reason the timing was favourable: every client in the
ecosystem needs rewriting, including the R `dataRetrieval` and Python `dataretrieval`
packages, so incumbency in this category is temporarily worth nothing.

The risk is managed by a single seam rather than by care. Every request goes through one
function that records the full URL, parameters, response hash, retrieval timestamp, status,
and quota headers. Tests run against recorded responses, a live suite runs behind a marker,
and when a collection renames a field the blast radius is the normalisation layer alone.

## Transport

### Rate limits are a design constraint, not a footnote

The service allows 50 requests per IP per hour unauthenticated and 1,000 with a free key.
A single agent question comparing conditions at five sites costs roughly 15 to 25 requests,
so an unauthenticated user gets two or three questions per hour.

Three consequences, all of them structural rather than tuning.

Caching is load-bearing infrastructure. Responses are cached for the life of the process,
which is the span of one conversation, because a conversation asks about the same three
stations a dozen times.

Remaining quota is reported to the model on every result. An agent that knows it has nine
requests left can plan; one that discovers the limit as a failure cannot.

Locations are fetched once per session and registered on arrival, not on demand.

### The key travels in a header

`X-Api-Key`, never in a query string, and it is not recorded at all. The reason is
downstream: a manifest is meant to be published, and a key pasted into a recorded URL
travels with it. This is a two-line decision that only makes sense once provenance exists,
and it was easier to make early than to retrofit.

### Certificates come from certifi

Discovered by installing the built wheel into a clean environment, which is not something
the test suite could have caught, because every test in it uses a fake transport.

A Python installed outside the system keychain, the default for a python.org build on
macOS, ships with no trust store, and every HTTPS call fails verification. Since fetching
over HTTPS is the whole of what this package does, the certificates come from `certifi`
rather than from wherever the interpreter was installed. All clients share one fetch, so it
is one fix.

## Meaning

### A unit is refused, not guessed

An unmapped unit spelling raises rather than being interpreted, because a value read in the
wrong unit is the error the package exists to prevent.

There is one deliberate exception with a warning. A spelling `pint` can parse but that has
no entry in the table is allowed through and the assumption is made audible. The standing
example is `ppt`: `pint` reads parts per trillion and USGS means parts per thousand, which
is a factor of 10⁹ between two dimensionally identical readings. So `ppt` has an explicit
entry mapping to its own dimension, and the general fallback warns rather than staying
silent.

Four spellings of cubic feet per second now appear across two agencies: `cfs` in flood
categories, `kcfs` in the status block of the same response, `ft^3/s` from the observation
service, and `ft³/s` from the model. None is convertible by a plain reading.

### Two fields are published with no unit at all

`altitude` and `drainage_area` come back as bare numbers and the collection schema states
no unit for either. Only `contributing_drainage_area` mentions square miles, in prose, in a
description.

The conventions of feet and square miles are therefore applied in `normalise.py`, where
they are visible and can be argued with, rather than assumed silently three layers
downstream. Every result that returns a drainage area says in a note that the service
published no unit.

### A datum is registered whether or not it can be converted

A station's own frame is registered on arrival in every case. Where the altitude and its
vertical datum are both published, the offset is registered too and a stage can be moved
onto that datum. Where either is missing, the frame stands without an offset and
`to_datum` refuses.

Guessing which datum an altitude is measured from is the same class of error as guessing an
offset between two datums, arriving one step earlier and being correspondingly harder to
observe. Boulder Creek publishes its altitude on NGVD29, not NAVD88, which is what that
refusal is protecting.

Registration happens when a location is fetched rather than when a conversion is attempted.
Lazily would leave a stage retrieved before the site record as a bare length, which is the
shape the package exists to refuse.

### Time needs two fields, not one

A station timezone is resolved from the abbreviation together with the daylight saving
flag. MST without daylight saving is Arizona and MST with it is Colorado, and they differ
by an hour for eight months of the year. Zones that do not observe daylight saving are
given as fixed offsets, because that is what they are.

A daily value carries a date and not an instant. It is not converted into one, because a
daily mean describes a day and giving it a clock time asserts something the service did not
publish. Age is measured from the end of the day, the earliest moment the value could have
been complete.

### Quality grades down, never up

Approval arrives as `Provisional` or `Approved`, where WaterServices used `P` and `A`, so
the mapping in `quantity-guard` does not carry over.

Condition codes are graded rather than discarded, and they grade below their review status:
approved record of an ice-affected measurement is not approved-quality data. An
unrecognised code grades to unverified with a warning, because absence of a mapping is not
evidence of good record.

That default is load-bearing rather than defensive. Two qualifiers turned up in live record
that appear in no documentation found: `DIFFDATUM`, which marks a gage height not on the
station's current datum and is the datum hazard arriving as a qualifier, and `REVISED`,
which describes the record rather than its trustworthiness and so does not grade down. The
vocabulary is not published anywhere, so the package will keep meeting codes it has not
seen.

## The tool surface

Thirteen tools over five services. Three constraints shaped it, all following from the
consumer being a model rather than a program.

### Organised by verb, not by agency

Tool count is a cost, because models degrade as the list grows. The surface is organised by
verb and the choice of which service answers is made by the server, not put to the caller.
A list mirroring five APIs would be several times longer and would make a caller learn an
agency's vocabulary before asking a question.

The count is tested against a budget of fourteen rather than an exact number, so adding a
source is a design decision to take deliberately rather than a test to update.

### Size is a cost, and silence about it is worse

A year of 15-minute record is 35,000 points and no answer needs them in context. Series
return a handle, a summary, and at most twenty downsampled points. Results are budgeted,
and anything dropped to stay inside the budget is stated in the payload.

That last part is not politeness. A result that dropped 900 of 1,000 items and does not say
so reads as complete, which is worse than failing.

The same reasoning keeps every polygon out of every answer. A delineated basin is around
1,750 coordinate pairs, and a SWOT reach centreline is a few hundred. Both are the answer
to a mapping question and the wrong answer to every question an agent asks.

This turned out to be the one thing the benchmark could measure cleanly. On the two tasks
requiring a long record, raw JSON cost 42,000 and 50,000 prompt tokens against 2,400 and
5,500 through the toolkit, and the model degenerated into repeating a number rather than
answering.

### Failure is a return value

Every failure carries a typed code and a repair string, and the MCP server returns it as
content marked in error rather than as a protocol fault. A raised exception ends the turn.
A failure carrying a usable correction keeps the model in the conversation, which is what
the `guarded_repair` condition in the `quantity-guard` benchmark measured recovering.

Error codes earn their place by naming a different repair. Two failures fixed the same way
do not need two codes.

### Values leave with their frames attached

A quantity is serialised as an object with its unit, datum, and grade beside it, not as a
string a model has to parse back apart. Units are written the way the service writes them,
`ft^3/s` rather than `pint`'s `ft ** 3 / s`, so a model can match what it reads against what
it was told.

A missing value says it is missing rather than arriving as an absent key, since a reading
rendered without a value looks like a rendering fault and this one is a fact about the
record.

### Tool descriptions are product, not documentation

The `quantity-guard` evaluation found that declaring physical metadata in the schema
without enforcing it still recovered a third of the runs failing at baseline. So what a
description says about datums, staleness, and provisional record does work before any
validation runs.

`get_latest` says latest is not the same as current. `get_forecast` says its stages are on
the gage datum and cannot be compared to a survey without an offset. `get_model_forecast`
says its values are modelled and may have nothing observed behind them. There is a test
asserting those sentences are present, so they cannot be edited away casually.

## Provenance

### The manifest is assembled from what happened

Every retrieval is recorded at the transport layer with its URL, parameters, timestamp,
status, and body hash, and every quantity crossing a tool boundary is entered in the
ledger. The manifest is built from those records rather than reconstructed from what the
code was supposed to have done.

The distinction is the whole reason the session exists. A pipeline that reports its own
provenance from its source code reports the provenance it intended, and the two differ
exactly when something went wrong.

### Replay, and the decomposition nothing else offers

Hydrology reproduces at 1.6% in the tested literature. The usual explanation is that data
and code were not published, and it hides the more interesting failure: a published
pipeline against a live service does not reproduce either, because the service revised the
record underneath it.

The modern USGS API publishes a `time-series-revisions` collection recording changes and
deletions to approved record. Nothing consumes it. That is what turns a manifest from an
export into a decomposition.

| mode | procedure | isolates |
|---|---|---|
| `offline` | recompute from archived bodies | code and library change |
| `strict` | re-fetch, require identical responses | any drift at all |
| `revision_aware` | re-fetch, diff, ask the service to account for each difference | data revision |

Attribution joins on the `time_series_id` the reading already carries, so it is a lookup
rather than a heuristic. Demonstrated against record USGS revised in August 2024, where the
same bundle and the same re-fetch give `changed` under `strict` and `reproduced` under
`revision_aware`, with each difference carrying the agency's own note. A difference with no
published revision behind it stays reported as unexplained, which is what keeps the check
from being vacuous, and there is a test for that failure mode.

Two details took measurement rather than reasoning. A daily value has to be matched against
a revision window by interval overlap rather than as the instant of midnight, because the
windows open at a clock time and point matching left the first day of every revision
unattributed. And archived bodies are verified against their hashes before anything is
compared, since a bundle whose archive has drifted makes a difference unattributable rather
than merely unexplained. That second one was found by writing an incoherent test fixture
and watching replay report a confident wrong answer.

## Sentinels, and why there is no list of them

Three services, three spellings of "no value", each dimensionally valid and each passing
every check downstream.

| service | fill | notes |
|---|---|---|
| WaterServices, now retiring | -999999 | published per variable in `noDataValue` |
| NOAA NWPS | -9999 | for a flood threshold never set |
| SWOT | -999999999999.0 | for any field a retrieval failed to produce |

The modern USGS API appears to have dropped the sentinel entirely: a missing value is
`null` and the qualifier says why, observed as `["EQUIP"]` for an equipment outage. That is
a real improvement and it removes a hazard the old pack spent code on.

SWOT fills are caught by magnitude rather than by exact value, because the products use
more than one and every one of them is far outside any physical range. A list would miss a
spelling; a threshold does not.

## Two facts that decide answers and appear in no documentation

### NWPS stages are on the gage datum

The forecast service publishes flood thresholds in feet and does not say what they are
measured from. The evidence that they are on the gage's own datum is that the observed
stage published there matches USGS parameter 00065 at the same station and time exactly.

The entire freeboard calculation turns on this. Thresholds and gage heights can be
differenced with no shift. A surveyed elevation cannot. Get it wrong in the direction of
assuming a national datum and the answer is the levee-is-safe error.

### The USGS datum arithmetic is checkable against the agency's own figure

At Little Falls, gage height is 3.03 ft on the station datum, the station's zero is at
37.04 ft NAVD88, and USGS separately publishes the water surface elevation as parameter
63160 at 40.07 ft NAVD88. The three agree.

That is used as the ground truth for the benchmark's freeboard task, which means the
expected answer rests on an agency figure rather than on this project's arithmetic.

## Measurement

`bench/` measures what the toolkit is worth to a model: nine tasks at one station over nine
hazards, every one observed in a live payload rather than invented.

Three conditions differ only in the interface, never the data. `http_only` is a fetch tool
returning the service's own JSON, which is what a developer has today. `toolkit_plain` is
the tools with results stripped to bare magnitudes and notes removed, which is what a
competent wrapper returns. `toolkit` is the tools as they are.

The middle condition is what makes the measurement worth taking. Without it, a gap between
the first and last would only show that structured retrieval beats raw JSON, which nobody
doubts. The difference between the last two is what the metadata is worth on its own.

Ground truth is derived from the same recorded responses the tools serve, and a test solves
every task through the toolkit and checks the result against the declared answer. A task
whose answer cannot be reached that way is a broken task and that is where it shows up.
Scoring was written and committed before the first sweep.

### What it found

216 runs on one model. `http_only` 61/72, `toolkit_plain` 63/72, `toolkit` 70/72.

The honest reading is in the README and is repeated here: the gap is driven almost entirely
by the two tasks whose raw payload will not fit. On everything else the toolkit and raw
fetching are level. The suite sits at ceiling on six tasks of nine, which is a finding about
the suite. Eight of 27 cells split across their replicates, so differences under roughly two
runs in eight are not separated by this design, and one model is one model.

The one clean separation is the opaque-unit task, where `toolkit_plain` scores 1/8 with the
recorded trap firing seven times against 7/8 with the frames left on. That contrast is
partly self-referential, since `toolkit_plain` is a condition built by degrading this
package's own output.

### Three defects it found before it measured anything

None of these could have been caught by the test suite, because all three look like working
code until something drives them over a real question.

A text query on `parameter-codes` is ignored silently. `q=discharge` answers 200 with the
first rows of the collection, which reads as a result and is not one.

`latest-continuous` pages at ten and publishes `numberMatched` as null, so the only signal
that a response is partial is a `next` link, and nothing checked it. Little Falls publishes
nineteen series and a listing that read as complete was missing the station's discharge.

`ConnectionResetError` arrives as a bare `OSError` rather than through `URLError`, so the
retry loop never saw it. Four of the first 46 runs of a sweep died that way, all on one
condition because that is what happened to be in flight. That biases a denominator rather
than merely shrinking it, and the condition is the independent variable.

## Decisions the evidence reversed

The part of a design history worth reading.

### GRACE is not queued

The plan grouped ERA5 and GRACE together as queued sources needing a job surface. ERA5 is
queued. GRACE is not: its collections and granules can be searched with no account at all,
and only the data file is protected. Confirmed by fetching both links: the file answers 401
and the checksum beside it answers 206.

So the module keeps search open and download credentialed, and that boundary is a test, so
a change moving it fails rather than surprising someone mid-question. The queued surface
was still worth building, for ERA5 and for whatever comes next.

### The benchmark denominator was flattering raw JSON

Correctness was first reported over runs that produced an answer, excluding eight that did
not. Seven of those belonged to `http_only` on the two long-record tasks, where the payload
is 42,000 tokens and the model degenerates instead of answering.

Those failures are caused by the condition. Excluding them credited raw JSON for the runs
its own payload size destroyed, and moved its score from 85% to 95%. Correctness now counts
every run. This changed after the sweep, which is stated in the README and the commit,
because a denominator changed after seeing results is exactly the thing a reader should be
told about; the scoring rules themselves did not change.

### Exact parameter names are not a usable interface

Name lookup was first routed to the service's `parameter_name` filter, which matches in
full. The published name for specific conductance is `Specific cond at 25C`, which nothing
would guess, and a title-cased guess matches nothing.

Descriptions are now matched against a table held in the package, and only a code or an
exact name is put to the service. The failure message says names are not searchable there
and suggests looking at what the station measures instead.

### `kcfs` nearly cubed a prefix

Written into the unit table as `kilofoot**3/second`, which is (1000 ft)³ and out by a
factor of 10⁹. Caught within a minute, and it is the error this package exists to prevent
arriving in its own unit table. `pint` reads the bare spelling correctly, because
`quantity-guard` defines `cfs` as a unit so the prefix applies to the whole. Pinned by a
test.

## What is deliberately not built

Coordinate reprojection, which belongs to a geometry type this package does not define. CRS
is carried as a consistency tag and checked for equality.

Reading gridded arrays. ERA5 and GRACE files are NetCDF, and a reader for them is a
dependency and a design not taken on. What comes back is the path and what is in it, which
is more useful than a half-hearted interpretation.

Fetching the static archives. CAMELS and HydroSHEDS are gigabytes with their own terms of
use, so the readers take a path and the download is the caller's business.

Reading the HydroSHEDS geometry. The attributes are read from the dBase table beside the
shape file, which is a documented format that needs no geospatial dependency, and the
polygon is never opened.

## What is not verified

Stated here rather than left to be discovered.

ERA5 submission and GRACE download need free accounts that were never obtained, so those
paths are exercised against a fake transport only. Everything up to them is live: the
dataset descriptions, the collection and granule searches, and the refusals.

The CAMELS and HydroSHEDS readers are written to published format specifications and tested
against files built to those specifications. Neither has been run against a real
distribution.

The benchmark has been run on one model.

Nobody outside this work has used any of it, which remains worth more than everything else
on the list.
