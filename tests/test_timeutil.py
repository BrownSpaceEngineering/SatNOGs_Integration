import datetime as dt

from satnogs_integration.timeutil import parse_iso8601, to_iso_z, utcnow

UTC = dt.UTC


def test_round_trip_z_form():
    assert to_iso_z(parse_iso8601("2026-10-18T18:06:09Z")) == "2026-10-18T18:06:09Z"


def test_offset_and_naive_and_fractional():
    assert parse_iso8601("2026-10-18T20:06:09+02:00") == dt.datetime(
        2026, 10, 18, 18, 6, 9, tzinfo=UTC
    )
    assert parse_iso8601("2026-10-18T18:06:09") == dt.datetime(
        2026, 10, 18, 18, 6, 9, tzinfo=UTC
    )
    assert parse_iso8601("2026-10-18T18:06:09.250Z").microsecond == 250000


def test_utcnow_is_aware_whole_seconds():
    now = utcnow()
    assert now.tzinfo is not None and now.microsecond == 0
