import binascii
import datetime as dt
import sqlite3

import pytest

from satnogs_integration.calibration import Calibration
from satnogs_integration.store import MIGRATIONS, SCHEMA, Store
from tests.support import NORAD, OBSERVERS, PAGE, PAGE1_ROWS, UTC

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


def read(path, sql):
    # a second plain connection, the way a dashboard reads the file
    with sqlite3.connect(path) as db:
        return db.execute(sql).fetchall()


def test_receptions_close_in_time_are_one_transmission(store, tmp_path):
    first = PAGE1_ROWS[0]  # received at 18:00:00 by station one
    rows = [
        first,
        dict(first, observer=OBSERVERS[1], timestamp="2026-10-18T18:00:10Z"),
        dict(first, timestamp="2026-10-18T18:10:00Z"),  # sent again later
    ]
    store.save_page(NORAD, rows, next_url=None, fetched_at=T0)
    assert read(
        tmp_path / "state.db",
        "SELECT timestamp, receptions, stations FROM transmissions ORDER BY timestamp",
    ) == [("2026-10-18T18:00:00Z", 2, 2), ("2026-10-18T18:10:00Z", 1, 1)]


def test_different_frames_are_different_transmissions(store, tmp_path):
    store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0)
    path = tmp_path / "state.db"
    assert read(path, "SELECT COUNT(*), SUM(receptions) FROM transmissions") == [
        (PAGE, PAGE)
    ]
    # the row kept is a stored frame
    assert read(
        path,
        "SELECT COUNT(*) FROM transmissions t JOIN frames f ON f.id = t.frame_id "
        "WHERE f.timestamp = t.timestamp AND f.sha256 = t.sha256",
    ) == [(PAGE,)]


def test_an_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as db:  # as written before schema versions existed
        db.executescript(SCHEMA)
        db.execute(
            "INSERT INTO frames (norad_cat_id, timestamp, observer, sha256, raw, "
            "fetched_at) VALUES (?, '2026-10-18T18:00:00Z', 'a', 'x', x'00', 't')",
            (NORAD,),
        )
    assert read(path, "PRAGMA user_version") == [(0,)]
    for _ in range(2):  # the second open changes nothing
        with Store(path) as store:
            assert store.counts(NORAD)["frames"] == 1
        assert read(path, "PRAGMA user_version") == [(len(MIGRATIONS),)]
    assert read(path, "SELECT receptions FROM transmissions") == [(1,)]


def test_telemetry_applies_calibrations(store, tmp_path):
    store.save_page(NORAD, PAGE1_ROWS, next_url=None, fetched_at=T0)
    fields = {"obc_bat": 8205, "obc_uptime": 5, "src_callsign": "9A0CC"}
    store.save_decode(
        store.undecoded(NORAD)[0].id, decoder="crocube", decoded_at=T0, fields=fields
    )
    sql = "SELECT field, raw, value, unit FROM telemetry ORDER BY field"
    raw_only = [("obc_bat", 8205, 8205.0, None), ("obc_uptime", 5, 5.0, None)]
    # numbers only, and unchanged without a calibration
    assert read(tmp_path / "state.db", sql) == raw_only
    store.set_calibrations("crocube", [Calibration("obc_bat", 0.001, 0.0, "V")])
    bat, uptime = read(tmp_path / "state.db", sql)
    assert bat == ("obc_bat", 8205, pytest.approx(8.205), "V")
    assert uptime == ("obc_uptime", 5, 5.0, None)
    # a decoder's calibrations are replaced as a set
    store.set_calibrations("crocube", [])
    assert read(tmp_path / "state.db", sql) == raw_only
