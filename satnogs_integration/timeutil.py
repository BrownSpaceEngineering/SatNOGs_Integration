# a utc time helper to standardize time to timezone-aware UTC

import datetime as dt

ISO_Z = "%Y-%m-%dT%H:%M:%SZ"

# current aware UTC


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


# parse ISO-8601 string to aware UTC


def parse_iso8601(text: str) -> dt.datetime:
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


# format time as YYYY-MM-DDTHH:MM:SSZ
# only accepted form satnogs will take ??


def to_iso_z(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime(ISO_Z)
