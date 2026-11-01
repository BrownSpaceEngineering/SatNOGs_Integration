# SQLite database

import binascii
import datetime as dt
import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .timeutil import parse_iso8601, to_iso_z

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    id             INTEGER PRIMARY KEY,
    norad_cat_id   INTEGER NOT NULL,
    timestamp      TEXT    NOT NULL,   -- as SatNOGS DB returned it
    observer       TEXT    NOT NULL,
    sha256         TEXT    NOT NULL,
    raw            BLOB    NOT NULL,
    station_id     INTEGER,
    observation_id INTEGER,
    app_source     TEXT,
    transmitter    TEXT,
    fetched_at     TEXT    NOT NULL,
    decoder        TEXT,
    decoded_at     TEXT,
    decoded_json   TEXT,               -- NULL until decoded
    decode_error   TEXT,               -- NULL unless the decoder rejected it
    UNIQUE (norad_cat_id, timestamp, observer, sha256)
);
CREATE INDEX IF NOT EXISTS ix_frames_undecoded
    ON frames (norad_cat_id, id) WHERE decoded_at IS NULL;
CREATE TABLE IF NOT EXISTS cursor (
    norad_cat_id     INTEGER PRIMARY KEY,
    watermark        TEXT,             -- start time of the newest completed sweep
    since            TEXT,             -- lower bound of the running sweep
    sweep_started_at TEXT,             -- when the running sweep was opened
    next_url         TEXT              -- saved page cursor; NULL when no sweep is running
);
"""

INSERT_FRAME = (
    "INSERT OR IGNORE INTO frames (norad_cat_id, timestamp, observer, sha256, raw, "
    "station_id, observation_id, app_source, transmitter, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass(frozen=True)
class Cursor:
    watermark: dt.datetime | None
    since: dt.datetime | None
    sweep_started_at: dt.datetime | None
    next_url: str | None

    @property
    def running(self) -> bool:
        return self.next_url is not None


@dataclass(frozen=True)
class Frame:
    id: int
    raw: bytes
    timestamp: str
    observer: str


class Store:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":  # SQLite's name for an in-memory database
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    #sweep cursor

    def get_cursor(self, norad_cat_id: int) -> Cursor:
        row = self._db.execute(
            "SELECT * FROM cursor WHERE norad_cat_id = ?", (norad_cat_id,)
        ).fetchone()
        if row is None:
            return Cursor(None, None, None, None)

        def parse(text: str | None) -> dt.datetime | None:
            return parse_iso8601(text) if text else None

        return Cursor(
            parse(row["watermark"]),
            parse(row["since"]),
            parse(row["sweep_started_at"]),
            row["next_url"],
        )

    def open_sweep(
        self,
        norad_cat_id: int,
        *,
        since: dt.datetime,
        started_at: dt.datetime,
        first_url: str,
    ) -> None:
        """Start a new sweep; the watermark is kept until the sweep completes."""
        self._db.execute(
            "INSERT INTO cursor (norad_cat_id, since, sweep_started_at, next_url) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (norad_cat_id) DO UPDATE SET "
            "since = excluded.since, sweep_started_at = excluded.sweep_started_at, "
            "next_url = excluded.next_url",
            (norad_cat_id, to_iso_z(since), to_iso_z(started_at), first_url),
        )

    def save_page(
        self,
        norad_cat_id: int,
        rows: Iterable[dict],
        *,
        next_url: str | None,
        fetched_at: dt.datetime,
    ) -> int:
        """Store a page's rows and its next cursor in one transaction.

        When next_url is None the sweep is complete: the watermark advances to the
        sweep's own start time (never backwards) and the cursor is cleared.
        Returns the number of rows that were new.
        """
        params = list(self._frame_params(norad_cat_id, rows, fetched_at))
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            new = self._db.executemany(INSERT_FRAME, params).rowcount
            if next_url is None:
                self._db.execute(
                    "UPDATE cursor SET watermark = "
                    "MAX(COALESCE(watermark, ''), COALESCE(sweep_started_at, '')), "
                    "since = NULL, sweep_started_at = NULL, next_url = NULL "
                    "WHERE norad_cat_id = ?",
                    (norad_cat_id,),
                )
            else:
                self._db.execute(
                    "UPDATE cursor SET next_url = ? WHERE norad_cat_id = ?",
                    (next_url, norad_cat_id),
                )
            self._db.execute("COMMIT")
        return new

    def _frame_params(
        self, norad_cat_id: int, rows: Iterable[dict], fetched_at: dt.datetime
    ) -> Iterator[tuple]:
        """One parameter tuple per usable row.

        Rows without a frame or a timestamp, or with bad hex, are logged and skipped.
        """
        fetched = to_iso_z(fetched_at)
        for row in rows:
            frame_hex = row.get("frame") or ""
            timestamp = row.get("timestamp")
            if not frame_hex or not timestamp:
                log.warning(
                    "skipping row without frame or timestamp: %s",
                    {k: row.get(k) for k in ("timestamp", "observer")},
                )
                continue
            try:
                raw = binascii.unhexlify(frame_hex)
            except (binascii.Error, ValueError):
                log.warning(
                    "skipping row with invalid hex at %s from %s",
                    timestamp,
                    row.get("observer"),
                )
                continue
            yield (
                norad_cat_id,
                timestamp,
                row.get("observer") or "",
                hashlib.sha256(raw).hexdigest(),
                raw,
                row.get("station_id"),
                row.get("observation_id"),
                row.get("app_source"),
                row.get("transmitter") or None,
                fetched,
            )

    #decoding

    def undecoded(self, norad_cat_id: int, *, limit: int | None = None) -> list[Frame]:
        sql = (
            "SELECT id, raw, timestamp, observer FROM frames "
            "WHERE norad_cat_id = ? AND decoded_at IS NULL ORDER BY id"
        )
        params: list[object] = [norad_cat_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            Frame(r["id"], bytes(r["raw"]), r["timestamp"], r["observer"])
            for r in self._db.execute(sql, params)
        ]

    def save_decode(
        self,
        frame_id: int,
        *,
        decoder: str,
        decoded_at: dt.datetime,
        fields: dict | None = None,
        error: str | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE frames SET decoder = ?, decoded_at = ?, decoded_json = ?, "
            "decode_error = ? WHERE id = ?",
            (
                decoder,
                to_iso_z(decoded_at),
                json.dumps(fields, sort_keys=True) if fields else None,
                error[:500] if error else None,
                frame_id,
            ),
        )

    #reporting 

    def counts(self, norad_cat_id: int) -> dict[str, object]:
        row = self._db.execute(
            "SELECT COUNT(*) AS frames, SUM(decoded_json IS NOT NULL) AS decoded, "
            "SUM(decode_error IS NOT NULL) AS errors, SUM(decoded_at IS NULL) AS pending, "
            "MAX(timestamp) AS newest FROM frames WHERE norad_cat_id = ?",
            (norad_cat_id,),
        ).fetchone()
        cursor = self.get_cursor(norad_cat_id)
        return {
            "frames": row["frames"],
            "decoded": row["decoded"] or 0,
            "errors": row["errors"] or 0,
            "pending": row["pending"] or 0,
            "newest_frame": row["newest"],
            "watermark": to_iso_z(cursor.watermark) if cursor.watermark else None,
            "sweep_running": cursor.running,
        }
