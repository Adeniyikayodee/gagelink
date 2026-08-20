"""Transport behaviour, checked against recorded responses rather than the service."""

import json

import pytest

from gagelink import (
    MemoryCache,
    Quota,
    QuotaExhausted,
    Service,
    ServiceUnavailable,
    UnknownCollection,
)

PAGE = json.dumps({"type": "FeatureCollection", "features": [], "numberReturned": 0})


def recorded(status=200, headers=None, body=PAGE):
    """A fetch that answers from a fixed response and records what it was asked."""
    calls = []

    def fetch(url, request_headers):
        calls.append((url, dict(request_headers)))
        return status, headers or {}, body

    fetch.calls = calls
    return fetch


def test_an_unknown_collection_is_refused_at_the_call():
    """A typo should fail here, not as a 404 from three layers away."""
    with pytest.raises(UnknownCollection) as caught:
        Service(fetch=recorded()).items("latest_continuous")
    assert "latest-continuous" in str(caught.value)


def test_the_api_key_travels_in_a_header_and_not_in_the_url():
    """Manifests are meant to be published, so a key in the query string would travel
    with them. It goes in a header and is not recorded at all."""
    fetch = recorded()
    service = Service(api_key="secret", fetch=fetch)
    _, retrieval = service.items("monitoring-locations", id="USGS-07374000")

    url, headers = fetch.calls[0]
    assert headers["X-Api-Key"] == "secret"
    assert "secret" not in url
    assert "secret" not in json.dumps(retrieval.record())


def test_parameters_are_ordered_so_one_query_has_one_url():
    """The URL is the cache key and part of the manifest, so two spellings of the same
    query must not produce two records."""
    service = Service(fetch=recorded())
    first = service.url_for("daily", parameter_code="00060", monitoring_location_id="X")
    second = service.url_for("daily", monitoring_location_id="X", parameter_code="00060")
    assert first == second


def test_an_empty_parameter_is_dropped_rather_than_sent_empty():
    url = Service(fetch=recorded()).url_for("daily", monitoring_location_id="X", limit=None)
    assert "limit" not in url


def test_the_retrieval_records_what_a_replay_needs():
    fetch = recorded(headers={"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "999"})
    _, retrieval = Service(fetch=fetch).items("daily", monitoring_location_id="X")

    record = retrieval.record()
    assert record["collection"] == "daily"
    assert record["status"] == 200
    assert record["size"] == len(PAGE)
    assert len(record["sha256"]) == 64
    assert record["retrieved_at"].endswith("+00:00")
    assert record["from_cache"] is False


def test_remaining_allowance_is_read_from_the_response():
    """An agent that knows it has nine requests left can plan; one that finds out by
    failing cannot."""
    fetch = recorded(headers={"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "9"})
    service = Service(fetch=fetch)
    _, retrieval = service.items("daily", monitoring_location_id="X")
    assert retrieval.quota == Quota(limit=50, remaining=9)
    assert service.quota.remaining == 9


def test_an_absent_allowance_is_unknown_rather_than_unlimited():
    _, retrieval = Service(fetch=recorded()).items("daily", monitoring_location_id="X")
    assert retrieval.quota.is_known is False


def test_exhausted_quota_names_the_fix():
    """Without a key the limit is 50 an hour, which is two or three agent questions, so
    the repair is worth stating rather than leaving to be discovered."""
    fetch = recorded(status=429, headers={"X-RateLimit-Limit": "50"}, body="")
    with pytest.raises(QuotaExhausted) as caught:
        Service(fetch=fetch).items("daily", monitoring_location_id="X")
    assert "signup" in str(caught.value)

    with pytest.raises(QuotaExhausted) as keyed:
        Service(api_key="k", fetch=fetch).items("daily", monitoring_location_id="X")
    assert "signup" not in str(keyed.value)


def test_a_service_error_is_not_returned_as_data():
    fetch = recorded(status=503, body="upstream unavailable")
    with pytest.raises(ServiceUnavailable):
        Service(fetch=fetch).items("daily", monitoring_location_id="X")


def test_a_body_that_is_not_json_fails_as_a_service_error():
    fetch = recorded(body="<html>maintenance</html>")
    with pytest.raises(ServiceUnavailable):
        Service(fetch=fetch).items("daily", monitoring_location_id="X")


def test_a_repeated_query_is_served_from_cache():
    """The allowance is 1,000 an hour with a key, and a conversation asks about the same
    three sites a dozen times, so this is a correctness constraint rather than a speed
    one."""
    fetch = recorded()
    service = Service(fetch=fetch, cache=MemoryCache())
    service.items("daily", monitoring_location_id="X")
    _, second = service.items("daily", monitoring_location_id="X")

    assert len(fetch.calls) == 1
    assert second.from_cache is True
    assert second.record()["from_cache"] is True


def test_a_failed_request_is_not_cached():
    """Caching a 503 would make one bad minute look like a broken station."""
    fetch = recorded(status=503, body="down")
    service = Service(fetch=fetch)
    for _ in range(2):
        with pytest.raises(ServiceUnavailable):
            service.items("daily", monitoring_location_id="X")
    assert len(fetch.calls) == 2


def test_the_revisions_collection_is_reachable():
    """Replay attributing a changed answer to a revised measurement depends on it."""
    assert "time-series-revisions" in __import__("gagelink").COLLECTIONS


def test_a_service_error_carries_the_service_s_own_explanation():
    """A bare 400 does not identify a wrong query parameter, and the description the
    service returns does."""
    body = json.dumps(
        {
            "code": "InvalidQuery",
            "type": "InvalidQuery",
            "description": "At least one requested property wasn't found",
        }
    )
    fetch = recorded(status=400, body=body)
    with pytest.raises(ServiceUnavailable) as caught:
        Service(fetch=fetch).items("monitoring-locations", nonsense="x")
    assert "InvalidQuery" in str(caught.value)
    assert "wasn't found" in str(caught.value)


def test_an_unexplained_error_still_names_the_status():
    fetch = recorded(status=500, body="<html>oops</html>")
    with pytest.raises(ServiceUnavailable) as caught:
        Service(fetch=fetch).items("daily", monitoring_location_id="X")
    assert "500" in str(caught.value)


def test_https_verification_does_not_depend_on_how_python_was_installed():
    """A python.org build on macOS ships without a trust store, and every call then fails
    verification. Fetching over HTTPS is the whole of what this package does, so the
    certificates come from certifi rather than from the interpreter's surroundings."""
    from gagelink.service import _trust_store

    context = _trust_store()
    assert context.verify_mode.name == "CERT_REQUIRED"
    assert context.get_ca_certs()


def test_every_client_shares_that_trust_store():
    """Three services, one fetch, so a fix in one place covers all of them."""
    from gagelink import nldi, nwps
    from gagelink.service import _http

    assert nwps._http is _http
    assert nldi._http is _http
