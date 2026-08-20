"""The services, served from recorded responses.

Every benchmark run answers from the same recorded bodies, for three reasons. The suite is
deterministic, so a difference between conditions is the condition rather than the river
having risen between runs. It costs no requests against an allowance of 1,000 an hour,
which a sweep of several hundred runs would otherwise exhaust in minutes. And a task's
expected answer can be computed from the same bytes the model is answering from, so ground
truth and served data cannot drift apart.

Everything here was captured live from USGS, NOAA, and the network index. Nothing is
invented.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from gagelink import Forecasts, Network, Service, Session

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

#: The station every task is set at. It carries the whole hazard set: a gage datum offset
#: from NAVD88, a forecast point with flood thresholds, series of mixed age, a missing
#: value, and 190 years of peak record.
STATION = "USGS-01646500"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


LOCATION = fixture("monitoring_location_01646500")
LATEST = fixture("latest_continuous_01646500")
PEAKS = fixture("peaks_01646500")
PARAMETER_CODES = fixture("parameter_codes")
GAUGE = fixture("nwps_gauge_01646500")
EMPTY = {"type": "FeatureCollection", "features": [], "numberReturned": 0}

PAGES = {
    "monitoring-locations": LOCATION,
    "latest-continuous": LATEST,
    "peaks": PEAKS,
    "parameter-codes": PARAMETER_CODES,
}


def _query(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def _filtered(page: dict[str, Any], query: Mapping[str, str]) -> dict[str, Any]:
    """The recorded page as the service would have answered this particular query.

    The filters have to be applied here rather than ignored, or a request for one
    parameter comes back carrying every parameter and the harness stops standing in for
    the service it is replacing.
    """
    features = list(page.get("features") or [])

    name = query.get("parameter_name")
    if name:
        features = [
            f for f in features
            if (f.get("properties") or {}).get("parameter_name", "").lower()
            == name.lower()
        ]

    wanted = query.get("parameter_code")
    if wanted:
        codes = {code.strip() for code in wanted.split(",")}
        features = [
            f for f in features
            if (f.get("properties") or {}).get("parameter_code") in codes
        ]

    if "parameter-codes" in str(query.get("__collection", "")) or (
        query.get("id") and str(query["id"]).isdigit()
    ):
        code = query.get("id")
        if code:
            features = [
                f for f in features if (f.get("properties") or {}).get("id") == code
            ]
        location = None
    else:
        location = query.get("monitoring_location_id") or query.get("id")
    if location:
        features = [
            f for f in features
            if (f.get("properties") or {}).get("monitoring_location_id", location)
            == location
            and (f.get("properties") or {}).get("id", location) == location
        ]

    window = query.get("datetime")
    if window and "/" in window:
        start, end = window.split("/", 1)
        features = [
            f for f in features
            if start <= str((f.get("properties") or {}).get("time") or "")[:10] <= end
        ]

    limit = int(query.get("limit", "10000"))
    trimmed = features[:limit]
    answer = {
        **{k: v for k, v in page.items() if k not in {"features", "numberReturned", "links"}},
        "features": trimmed,
        "numberReturned": len(trimmed),
    }
    # The service signals a partial answer with a next link and nothing else, so the
    # stand-in has to signal it the same way or the truncation check has nothing to read.
    if len(features) > limit:
        answer["links"] = [{"rel": "next", "href": "https://example.invalid/next"}]
    return answer


def usgs_fetch(url: str, headers: Any) -> tuple[int, dict, str]:
    query = _query(url)
    for collection, page in PAGES.items():
        if f"/{collection}/items" in url:
            return (
                200,
                {"X-RateLimit-Remaining": "999"},
                json.dumps(_filtered(page, query)),
            )
    return 200, {}, json.dumps(EMPTY)


def gauge_fetch(url: str, headers: Any) -> tuple[int, dict, str]:
    return 200, {}, json.dumps(GAUGE)


def session(question: str = "") -> Session:
    """A session wired to the recorded services."""
    return Session(
        service=Service(fetch=usgs_fetch),
        forecasts=Forecasts(fetch=gauge_fetch),
        network=Network(fetch=gauge_fetch),
        question=question,
    )


def raw(path: str) -> str:
    """What a plain HTTP tool receives for a request, which is the service's own JSON.

    The http_only condition needs exactly this: the bytes an agent developer gets today
    from calling the endpoint directly, with nothing interpreted. The same filters apply,
    so neither condition is handed more of the response than it asked for.
    """
    query = _query(path)
    for collection, page in PAGES.items():
        if collection in path:
            return json.dumps(_filtered(page, query))
    if "nwps" in path or "gauges" in path:
        return json.dumps(GAUGE)
    return json.dumps(
        {"error": "no such path", "available": ENDPOINT_PATHS}
    )


#: Listed in an error so a wrong path costs a turn rather than the run.
ENDPOINT_PATHS = [
    "usgs/monitoring-locations/items?id=USGS-01646500",
    "usgs/latest-continuous/items?monitoring_location_id=USGS-01646500",
    "usgs/peaks/items?monitoring_location_id=USGS-01646500",
    "nwps/gauges/01646500",
]
