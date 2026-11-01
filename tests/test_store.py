import binascii
import datetime as dt

from satnogs_integration.store import Store
from tests.support import NORAD, PAGE, PAGE1_ROWS, UTC

T0 = dt.datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def test_same_rows_twice_store_once(store):
    assert store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0) == PAGE
    assert store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0) == 0
    assert store.counts(NORAD)["frames"] == PAGE


def test_malformed_rows_are_skipped(store):
    rows = [
        dict(PAGE1_ROWS[0], frame="zz"),
        dict(PAGE1_ROWS[1], timestamp=None),
        dict(PAGE1_ROWS[2], frame=""),
    ]
    assert store.save_page(NORAD, rows, next_url=None, fetched_at=T0) == 0


def test_sweep_cursor_lifecycle(store):
    first = "https://db.test/api/telemetry/?satellite=62394"
    assert not store.get_cursor(NORAD).running
    store.open_sweep(NORAD, since=T0, started_at=T0, first_url=first)
    cursor = store.get_cursor(NORAD)
    assert (
        cursor.running
        and cursor.next_url == first
        and cursor.since == T0
        and cursor.watermark is None
    )
    store.save_page(
        NORAD, PAGE1_ROWS[:1], next_url="https://db.test/page2", fetched_at=T0
    )
    assert store.get_cursor(NORAD).next_url == "https://db.test/page2"
    store.save_page(NORAD, PAGE1_ROWS[1:], next_url=None, fetched_at=T0)
    cursor = store.get_cursor(NORAD)
    assert not cursor.running and cursor.watermark == T0 and cursor.next_url is None


def test_watermark_never_moves_backwards(store):
    store.open_sweep(NORAD, since=T0, started_at=T0, first_url="u")
    assert store.save_page(NORAD, [], next_url=None, fetched_at=T0) == 0
    earlier = T0 - dt.timedelta(hours=1)
    store.open_sweep(NORAD, since=earlier, started_at=earlier, first_url="u")
    store.save_page(NORAD, [], next_url=None, fetched_at=T0)
    assert store.get_cursor(NORAD).watermark == T0


def test_cursor_survives_reopen(tmp_path):
    path = tmp_path / "s.db"
    with Store(path) as store:
        store.open_sweep(
            NORAD, since=T0, started_at=T0, first_url="https://db.test/first"
        )
    with Store(path) as store:
        assert store.get_cursor(NORAD).next_url == "https://db.test/first"


def test_decode_bookkeeping(store):
    store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0)
    pending = store.undecoded(NORAD)
    assert len(pending) == PAGE and pending[0].raw == binascii.unhexlify(
        PAGE1_ROWS[0]["frame"]
    )
    store.save_decode(
        pending[0].id, decoder="crocube", decoded_at=T0, fields={"obc_uptime": 5}
    )
    store.save_decode(pending[1].id, decoder="crocube", decoded_at=T0, error="x" * 600)
    assert [f.id for f in store.undecoded(NORAD)] == [pending[2].id]
    counts = store.counts(NORAD)
    assert counts["decoded"] == 1 and counts["errors"] == 1 and counts["pending"] == 1
    assert store.undecoded(NORAD, limit=1)[0].id == pending[2].id
