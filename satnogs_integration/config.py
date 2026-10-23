import datetime as dt
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .timeutil import parse_iso8601, utcnow


class ConfigError(ValueError):
    """A missing or malformed setting."""


@dataclass(frozen=True)
class Settings:
    db_url: str
    db_token: str
    norad_cat_id: int
    decoder: str
    state_db: Path
    ingest_start: dt.datetime
    overlap: dt.timedelta
    poll_seconds: float

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, now: dt.datetime | None = None
    ) -> Self:
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return env.get(name, "").strip() or default

        def need(name: str) -> str:
            value = get(name)
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        def number(name: str, default: str, minimum: float) -> float:
            raw = get(name, default)
            try:
                value = float(raw)
            except ValueError:
                raise ConfigError(f"{name} must be a number, got {raw!r}") from None
            if not math.isfinite(value) or value < minimum:
                raise ConfigError(
                    f"{name} must be a finite number >= {minimum}, got {raw!r}"
                )
            return value

        norad_raw = need("NORAD_CAT_ID")
        try:
            norad = int(norad_raw)
        except ValueError:
            raise ConfigError("NORAD_CAT_ID must be an integer") from None
        if norad < 1:
            raise ConfigError("NORAD_CAT_ID must be positive")

        start_raw = get("INGEST_START")
        if start_raw:
            try:
                start = parse_iso8601(start_raw)
            except ValueError:
                raise ConfigError(
                    f"INGEST_START is not ISO-8601: {start_raw!r}"
                ) from None
        else:
            start = (now or utcnow()) - dt.timedelta(days=7)

        return cls(
            db_url=get("SATNOGS_DB_URL", "https://db.satnogs.org").rstrip("/"),
            db_token=need("SATNOGS_DB_TOKEN"),
            norad_cat_id=norad,
            decoder=need("DECODER"),
            state_db=Path(get("STATE_DB", "data/state.db")),
            ingest_start=start,
            overlap=dt.timedelta(hours=number("OVERLAP_HOURS", "48", 0)),
            poll_seconds=number("POLL_SECONDS", "600", 1),
        )
