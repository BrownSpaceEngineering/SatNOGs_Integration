import datetime as dt
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from satnogs_integration.satnogs_db import DbError
from satnogs_integration.sweep import compute_since, run_sweep
from tests.support import NORAD, PAGE, SINCE, UTC, FakeClock, FakeNow, make_client

OVERLAP = dt.timedelta(hours=48)
START_CLOCK = dt.datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def sweep(client, store, **kw):
    kw.setdefault("now", FakeNow(START_CLOCK))
    return run_sweep(
        client, store, norad_cat_id=NORAD, start=SINCE, overlap=OVERLAP, **kw
    )


def start_param(request: httpx.Request) -> str:
    return parse_qs(urlparse(str(request.url)).query)["start"][0]


def test_compute_since():
    assert compute_since(SINCE, None, OVERLAP) == SINCE
    assert compute_since(
        SINCE, SINCE + dt.timedelta(days=5), OVERLAP
    ) == SINCE + dt.timedelta(days=3)
    assert compute_since(SINCE, SINCE + dt.timedelta(hours=1), OVERLAP) == SINCE


def test_first_sweep_stores_everything_and_sets_watermark(client, store, fake_db):
    now = FakeNow(START_CLOCK)
    result = sweep(client, store, now=now)
    assert (
        result.completed
        and result.pages == 3
        and result.rows_seen == 2 * PAGE
        and result.rows_new == 2 * PAGE
    )
    assert store.counts(NORAD)["frames"] == 2 * PAGE
    assert store.get_cursor(NORAD).watermark == START_CLOCK + dt.timedelta(
        seconds=1
    )  # sweep open time
    assert start_param(fake_db.calls[0]) == "2026-10-11T00:00:00Z"


def test_rerun_is_idempotent_and_starts_at_watermark_minus_overlap(
    client, store, fake_db
):
    sweep(client, store)
    first_watermark = store.get_cursor(NORAD).watermark
    result = sweep(client, store, now=FakeNow(START_CLOCK + dt.timedelta(days=3)))
    assert result.rows_new == 0 and result.rows_seen == 2 * PAGE
    assert result.since == first_watermark - OVERLAP
    assert start_param(fake_db.calls[3]) == (first_watermark - OVERLAP).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_max_pages_pauses_and_the_next_run_resumes(client, store, fake_db):
    paused = sweep(client, store, max_pages=1)
    assert (
        not paused.completed
        and paused.pages == 1
        and store.counts(NORAD)["frames"] == PAGE
    )
    cursor = store.get_cursor(NORAD)
    assert (
        cursor.running
        and cursor.next_url == fake_db.page1_next
        and cursor.watermark is None
    )
    resumed = sweep(client, store, now=FakeNow(START_CLOCK + dt.timedelta(days=2)))
    assert resumed.resumed and resumed.completed and resumed.pages == 2
    assert store.counts(NORAD)["frames"] == 2 * PAGE
    assert store.get_cursor(NORAD).watermark == START_CLOCK + dt.timedelta(
        seconds=1
    )  #original open time
    assert len(fake_db.calls) == 3  # page 1 was not fetched again


def make_client_with_retries(fake_db):
    """A client that gives up after one retry, so a failing page fails fast."""
    return make_client(fake_db, FakeClock(), min_interval=0, max_retries=1)


def test_a_crash_mid_sweep_resumes_from_the_saved_page(client, store, fake_db):
    fake_db.queue(
        "page2", httpx.Response(500), httpx.Response(500), httpx.Response(500)
    )
    with pytest.raises(DbError):
        run_sweep(
            make_client_with_retries(fake_db),
            store,
            norad_cat_id=NORAD,
            start=SINCE,
            overlap=OVERLAP,
            now=FakeNow(START_CLOCK),
        )
    assert (
        store.counts(NORAD)["frames"] == PAGE
        and store.get_cursor(NORAD).next_url == fake_db.page1_next
    )
    result = sweep(client, store)
    assert (
        result.resumed
        and result.completed
        and store.counts(NORAD)["frames"] == 2 * PAGE
    )


def test_rejected_cursor_restarts_the_sweep_from_its_first_page(client, store, fake_db):
    store.open_sweep(
        NORAD,
        since=SINCE,
        started_at=START_CLOCK,
        first_url="https://db.test/api/telemetry/?cursor=stale&satellite=62394",
    )
    with pytest.raises(DbError) as info:
        sweep(client, store)
    assert info.value.status == 404
    assert store.get_cursor(NORAD).next_url == client.telemetry_url(NORAD, start=SINCE)
    result = sweep(client, store)
    assert result.completed and store.counts(NORAD)["frames"] == 2 * PAGE
