# client for satnogs db telemetry endpoint with 25 rows per page; 1 request/10s

import datetime as dt
import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Self
from urllib.parse import urlencode

import httpx

from . import __version__
from .timeutil import to_iso_z

log = logging.getLogger(__name__)

REQUESTS_PER_MINUTE = 6
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 300.0
USER_AGENT = f"satnogs-integration/{__version__}"


class DbError(RuntimeError):
    #failed request

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    url: str
    rows: list[dict] = field(default_factory=list)
    next_url: str | None = None


def parse_retry_after(
    value: str | None, *, now: dt.datetime | None = None
) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    now = now or dt.datetime.now(dt.UTC)
    return max(0.0, (when - now).total_seconds())


class DbClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        min_interval: float | None = None,
        max_retries: int = 6,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # 5 % under the published throttle: 10.5 s between requests.
        self.min_interval = (
            60 / REQUESTS_PER_MINUTE * 1.05 if min_interval is None else min_interval
        )
        self.max_retries = max_retries
        self._sleep, self._monotonic, self._jitter = sleep, monotonic, jitter
        self._last_call: float | None = None
        self.requests_made = 0
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def telemetry_url(
        self, norad_cat_id: int, *, start: dt.datetime | None = None
    ) -> str:
        """First-page URL for one satellite, optionally bounded below by timestamp."""
        params: dict[str, str | int] = {"satellite": norad_cat_id, "format": "json"}
        if start is not None:
            params["start"] = to_iso_z(start)
        return f"{self.base_url}/api/telemetry/?{urlencode(params)}"

    def fetch_page(self, url: str) -> Page:
        body = self._get_json(url)
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise DbError(f"unexpected response shape from {url}")
        return Page(url=url, rows=body["results"], next_url=body.get("next") or None)

    def pages(self, first_url: str) -> Iterator[Page]:
        """Yield pages from first_url, following "next" until it is null."""
        url: str | None = first_url
        while url:
            page = self.fetch_page(url)
            yield page
            url = page.next_url

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self._last_call + self.min_interval - self._monotonic()
            if wait > 0:
                log.debug("pacing: waiting %.1f s", wait)
                self._sleep(wait)

    def _backoff(self, attempt: int, retry_after: float | None, reason: str) -> None:
        delay = (
            retry_after
            if retry_after is not None
            else BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        )
        delay = min(delay, BACKOFF_MAX_SECONDS)
        delay += delay * 0.1 * self._jitter()
        log.warning(
            "%s; retry %d/%d in %.1f s", reason, attempt, self.max_retries, delay
        )
        self._sleep(delay)

    def _get_json(self, url: str) -> object:
        attempt = 0
        while True:
            self._pace()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise DbError(
                        f"{url}: {exc} after {self.max_retries} retries"
                    ) from exc
                self._backoff(attempt, None, f"transport error: {exc}")
                continue
            finally:
                self._last_call = self._monotonic()
                self.requests_made += 1
            status = response.status_code
            if status == 200:
                return response.json()
            if status == 429 or status >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise DbError(
                        f"HTTP {status} after {self.max_retries} retries: {url}",
                        status=status,
                    )
                self._backoff(
                    attempt,
                    parse_retry_after(response.headers.get("Retry-After")),
                    f"HTTP {status}",
                )
                continue
            if status == 401:
                raise DbError(
                    "SatNOGS DB rejected the token (401): SATNOGS_DB_TOKEN must be a "
                    "DB API key from db.satnogs.org, not a Network key",
                    status=401,
                )
            raise DbError(
                f"HTTP {status} from {url}: {response.text[:200]}", status=status
            )
