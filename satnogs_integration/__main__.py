# satnogs-integration command: sweep, decode, repeat
# exit codes: 0 ok, 1 sweep failed (--once), 2 bad config, 3 key refused, 130 ctrl-c

import argparse
import datetime as dt
import json
import logging
import sys
import time
from collections.abc import Callable

import httpx

from .calibration import load_calibrations
from .config import ConfigError, Settings
from .decode import DecodeError, KaitaiDecoder, decode_pending
from .satnogs_db import DbClient, DbError
from .store import Store
from .sweep import SweepResult, run_sweep

log = logging.getLogger("satnogs_integration")

MAX_FAILURE_PAUSE_SECONDS = 6 * 3600.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="satnogs-integration",
        description="Mirror one satellite's frames from SatNOGS DB and decode them.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one sweep and one decode pass, then exit",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        metavar="N",
        help="stop a sweep after N pages (it resumes next run)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="print the database counters as JSON and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def pause_after(
    poll_seconds: float, failures: int, retry_after: float | None = None
) -> float:
    # poll interval, doubled per failed cycle up to 6 h, never under retry_after
    ceiling = max(poll_seconds, MAX_FAILURE_PAUSE_SECONDS)
    pause = min(poll_seconds * 2 ** min(failures, 16), ceiling)
    return pause if retry_after is None else max(pause, retry_after)


def run_cycle(
    settings: Settings,
    store: Store,
    client: DbClient,
    decoder,
    *,
    overlap: dt.timedelta | None = None,
    max_pages: int | None = None,
) -> SweepResult:
    error = None
    try:
        result = run_sweep(
            client,
            store,
            norad_cat_id=settings.norad_cat_id,
            start=settings.ingest_start,
            overlap=settings.overlap if overlap is None else overlap,
            max_pages=max_pages,
        )
    except DbError as exc:
        error = exc
    # frames saved before a failure are still decoded
    decode_pending(store, decoder, settings.norad_cat_id)
    if error is not None:
        raise error
    return result


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        decoder = KaitaiDecoder(settings.decoder)
        calibrations = load_calibrations(decoder.name)
    except (ConfigError, DecodeError) as exc:
        log.error("configuration error: %s", exc)
        return 2
    norad = settings.norad_cat_id
    short_overlap = min(settings.poll_overlap, settings.overlap)
    with Store(settings.state_db) as store:
        if args.stats:
            print(json.dumps(store.counts(norad), indent=2))
            return 0
        store.set_calibrations(decoder.name, calibrations)
        with DbClient(
            settings.db_url,
            settings.db_token,
            min_interval=settings.min_interval,
            transport=transport,
            sleep=sleep,
        ) as client:
            log.info(
                "satnogs-integration: NORAD %d, decoder %s, state db %s",
                norad,
                decoder.name,
                settings.state_db,
            )
            # full overlap once per DEEP_SWEEP_HOURS, short overlap otherwise
            last_deep: float | None = None
            deep = False
            failures = 0
            try:
                while True:
                    retry_after = None
                    try:
                        if not store.get_cursor(norad).running:
                            deep = (
                                args.once
                                or last_deep is None
                                or time.monotonic() - last_deep
                                >= settings.deep_interval.total_seconds()
                            )
                        result = run_cycle(
                            settings,
                            store,
                            client,
                            decoder,
                            overlap=settings.overlap if deep else short_overlap,
                            max_pages=args.max_pages,
                        )
                        if deep and result.completed:
                            last_deep = time.monotonic()
                        failures = 0
                    except DbError as exc:
                        log.error("sweep failed: %s", exc)
                        if exc.status in (401, 403):
                            # retrying a refused key only adds load
                            log.error("SatNOGS DB refused access; stopping")
                            return 3
                        if args.once:
                            return 1
                        failures += 1
                        retry_after = exc.retry_after
                    if args.once:
                        return 0
                    pause = pause_after(settings.poll_seconds, failures, retry_after)
                    log.info("sleeping %.0f s", pause)
                    sleep(pause)
            except KeyboardInterrupt:
                log.info("interrupted; progress is saved")
                return 130


if __name__ == "__main__":
    sys.exit(main())
