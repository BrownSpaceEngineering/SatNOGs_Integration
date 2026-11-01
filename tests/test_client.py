import datetime as dt

import httpx
import pytest

from satnogs_integration.satnogs_db import DbError, parse_retry_after
from tests.support import NORAD, PAGE, SINCE, UTC, make_client


def test_first_page_url(client):
    url = client.telemetry_url(NORAD, start=SINCE)
    expected = "https://db.test/api/telemetry/?satellite=62394&format=json"
    assert url == expected + "&start=2026-10-11T00%3A00%3A00Z"


def test_follows_next_until_null(client, fake_db):
    pages = list(client.pages(client.telemetry_url(NORAD, start=SINCE)))
    assert [len(p.rows) for p in pages] == [PAGE, PAGE, 0]
    assert pages[0].next_url == fake_db.page1_next and pages[-1].next_url is None
    assert len(fake_db.calls) == 3


def test_every_request_carries_token_and_user_agent(client, fake_db):
    client.fetch_page(client.telemetry_url(NORAD))
    request = fake_db.calls[0]
    assert request.headers["Authorization"] == "Token test-token"
    assert request.headers["User-Agent"].startswith("satnogs-integration/")


def test_requests_are_paced(fake_db, clock):
    with make_client(fake_db, clock) as client:  # default interval: 10.5 s
        list(client.pages(client.telemetry_url(NORAD)))
    assert clock.sleeps == [pytest.approx(10.5), pytest.approx(10.5)]


def test_429_waits_retry_after(client, fake_db, clock):
    fake_db.queue(
        "page1",
        httpx.Response(429, headers={"Retry-After": "7"}, json={"detail": "throttled"}),
    )
    assert len(client.fetch_page(client.telemetry_url(NORAD)).rows) == PAGE
    assert clock.sleeps == [7.0]


def test_5xx_and_transport_errors_back_off_exponentially(client, fake_db, clock):
    fake_db.queue(
        "page1",
        httpx.Response(503),
        lambda req: httpx.ConnectError("refused", request=req),
        httpx.Response(500),
    )
    assert len(client.fetch_page(client.telemetry_url(NORAD)).rows) == PAGE
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_gives_up_after_max_retries(fake_db, clock):
    fake_db.queue("page1", *[httpx.Response(500)] * 3)
    with (
        make_client(fake_db, clock, min_interval=0, max_retries=2) as client,
        pytest.raises(DbError, match="after 2 retries"),
    ):
        client.fetch_page(client.telemetry_url(NORAD))
    assert clock.sleeps == [2.0, 4.0]


def test_401_names_the_token_problem(client, fake_db):
    fake_db.queue("page1", httpx.Response(401, json={"detail": "Invalid token."}))
    with pytest.raises(DbError, match="DB API key") as info:
        client.fetch_page(client.telemetry_url(NORAD))
    assert info.value.status == 401


def test_other_4xx_is_fatal_without_retry(client, fake_db, clock):
    fake_db.queue(
        "page1", httpx.Response(400, json={"detail": "satellite is required"})
    )
    with pytest.raises(DbError) as info:
        client.fetch_page(client.telemetry_url(NORAD))
    assert info.value.status == 400 and clock.sleeps == []


def test_unexpected_body_shape_is_an_error(client, fake_db):
    fake_db.queue("page1", httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(DbError, match="unexpected response shape"):
        client.fetch_page(client.telemetry_url(NORAD))


def test_parse_retry_after():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    now = dt.datetime(2026, 10, 21, 7, 27, 30, tzinfo=UTC)
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 30.0
