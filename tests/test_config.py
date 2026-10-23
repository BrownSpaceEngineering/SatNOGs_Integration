import datetime as dt
from pathlib import Path

import pytest

from satnogs_integration.config import ConfigError, Settings

GOOD = {"SATNOGS_DB_TOKEN": "abc", "NORAD_CAT_ID": "62394", "DECODER": "crocube"}
NOW = dt.datetime(2026, 10, 18, 12, 0, tzinfo=dt.UTC)


def test_defaults():
    s = Settings.from_env(GOOD, now=NOW)
    assert s.db_url == "https://db.satnogs.org"
    assert s.norad_cat_id == 62394
    assert s.state_db == Path("data/state.db")
    assert s.ingest_start == NOW - dt.timedelta(days=7)
    assert s.overlap == dt.timedelta(hours=48)
    assert s.poll_seconds == 600


def test_explicit_values():
    s = Settings.from_env(
        {
            **GOOD,
            "INGEST_START": "2026-10-01T00:00:00Z",
            "OVERLAP_HOURS": "6",
            "POLL_SECONDS": "60",
            "SATNOGS_DB_URL": "https://db.test/",
        }
    )
    assert s.ingest_start == dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    assert s.overlap == dt.timedelta(hours=6)
    assert s.db_url == "https://db.test"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"SATNOGS_DB_TOKEN": ""}, "SATNOGS_DB_TOKEN"),
        ({"NORAD_CAT_ID": ""}, "NORAD_CAT_ID is not set"),
        ({"NORAD_CAT_ID": "x"}, "must be an integer"),
        ({"NORAD_CAT_ID": "0"}, "positive"),
        ({"DECODER": ""}, "DECODER"),
        ({"INGEST_START": "yesterday"}, "ISO-8601"),
        ({"OVERLAP_HOURS": "inf"}, "OVERLAP_HOURS"),
        ({"OVERLAP_HOURS": "nan"}, "OVERLAP_HOURS"),
        ({"POLL_SECONDS": "0"}, "POLL_SECONDS"),
    ],
)
def test_rejects_bad_values(overrides, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env({**GOOD, **overrides})
