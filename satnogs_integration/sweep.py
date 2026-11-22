#sweep satnogs db

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .satnogs_db import DbClient, DbError
from .store import Store
from .timeutil import utcnow

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    since: dt.datetime
    resumed: bool = False
    completed: bool = False
    pages: int = 0
    rows_seen: int = 0
    rows_new: int = 0

    def summary(self) -> str:
        state = "completed" if self.completed else "paused (will resume)"
        return (
            f"sweep {state}: {self.pages} page(s), {self.rows_seen} row(s), "
            f"{self.rows_new} new frame(s), since {self.since:%Y-%m-%dT%H:%M:%SZ}"
        )


def compute_since(
    start: dt.datetime, watermark: dt.datetime | None, overlap: dt.timedelta
) -> dt.datetime:
    return start if watermark is None else max(start, watermark - overlap)


def run_sweep(
    client: DbClient,
    store: Store,
    *,
    norad_cat_id: int,
    start: dt.datetime,
    overlap: dt.timedelta,
    now: Callable[[], dt.datetime] = utcnow,
    max_pages: int | None = None,
) -> SweepResult:
    cursor = store.get_cursor(norad_cat_id)
    if cursor.running:
        url, since, resumed = cursor.next_url, cursor.since or start, True
        log.info("resuming sweep from saved cursor (since %s)", since)
    else:
        since = compute_since(start, cursor.watermark, overlap)
        url = client.telemetry_url(norad_cat_id, start=since)
        store.open_sweep(norad_cat_id, since=since, started_at=now(), first_url=url)
        resumed = False
        log.info("new sweep since %s", since)

    result = SweepResult(since=since, resumed=resumed)
    try:
        for page in client.pages(url):
            new = store.save_page(
                norad_cat_id, page.rows, next_url=page.next_url, fetched_at=now()
            )
            result.pages += 1
            result.rows_seen += len(page.rows)
            result.rows_new += new
            log.info("page %d: %d row(s), %d new", result.pages, len(page.rows), new)
            if page.next_url is None:
                result.completed = True
                break
            if max_pages is not None and result.pages >= max_pages:
                break
    except DbError as exc:
        if resumed and result.pages == 0 and exc.status in (400, 404):
            # drop saved cursor when no longer accepted
            log.warning("saved cursor rejected (%s); discarding it", exc)
            store.open_sweep(
                norad_cat_id,
                since=since,
                started_at=now(),
                first_url=client.telemetry_url(norad_cat_id, start=since),
            )
        raise
    log.info(result.summary())
    return result
