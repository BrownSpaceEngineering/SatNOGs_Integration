#test sample data shared by the test modules; see conftest.py

import datetime as dt

import httpx

from satnogs_integration.satnogs_db import DbClient

NORAD = 62394  #crocube
UTC = dt.UTC
SINCE = dt.datetime(2026, 10, 11, tzinfo=UTC)
BASE = "https://db.test"
PAGE = 3  #limit to 3 rows per fake page 

# three frames CroCube transmitted on 2026-10-18
OBC_BEACON = (
    "86A240404040607282608686406103F04F42432C33352C32323536323630392C"
    "35373530303239362C383230352C313236382C34333730"
)
UHF_BEACON = (
    "86A240404040607282608686406103F0552C32323539363633322C35373535383734372C"
    "31382C302C313938382C323130382C313836382C3733302C444C374E44522C3333333837"
    "2C333238333038332C39332C31323100"
)
# nine bytes with no AX.25 header
BINARY = "000CB41E3DD8F06103"
FRAMES = [OBC_BEACON, UHF_BEACON, BINARY]
OBSERVERS = ["Station One-AA00aa", "Station Two-BB11bb"]
NEWEST = dt.datetime(2026, 10, 18, 18, 0, tzinfo=UTC)


def make_row(i: int) -> dict:
    #rows for the fake db
    received = NEWEST - dt.timedelta(minutes=i)
    return {
        "app_source": "network",
        "associated_satellites": [],
        "decoded": "influxdb",
        "frame": FRAMES[i % len(FRAMES)],
        "norad_cat_id": NORAD,
        "observation_id": 15223000 + i,
        "observer": OBSERVERS[i % len(OBSERVERS)],
        "sat_id": "HADL-0708-3181-3728-3212",
        "station_id": 2380 + i % len(OBSERVERS),
        "timestamp": received.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "transmitter": "",
        "version": "",
    }


PAGE1_ROWS = [make_row(i) for i in range(PAGE)]
PAGE2_ROWS = [make_row(i) for i in range(PAGE, 2 * PAGE)]


def page_url(cursor: str) -> str:
    return f"{BASE}/api/telemetry/?cursor={cursor}&format=json&satellite={NORAD}"


class FakeDb:
    #calls a test db that kind of resembles satnogs db

    page1_next = page_url("page2")
    page2_next = page_url("page3")

    def __init__(self) -> None:
        self.bodies = {
            "page1": {"next": self.page1_next, "previous": None, "results": PAGE1_ROWS},
            "page2": {"next": self.page2_next, "previous": BASE, "results": PAGE2_ROWS},
            "page3": {"next": None, "previous": self.page1_next, "results": []},
        }
        self.inject: dict[str, list] = {}
        self.calls: list[httpx.Request] = []

    def key(self, url: str) -> str:
        if url == self.page1_next:
            return "page2"
        if url == self.page2_next:
            return "page3"
        if "cursor=" in url:
            return "unknown"
        return "page1"  # any first-page query

    def queue(self, key: str, *responses) -> None:
        self.inject.setdefault(key, []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = self.key(str(request.url))
        if self.inject.get(key):
            canned = self.inject[key].pop(0)
            if isinstance(canned, Exception):
                raise canned
            if callable(canned):
                raise canned(request)
            return canned
        if key == "unknown":
            return httpx.Response(404, json={"detail": "Invalid cursor"})
        return httpx.Response(200, json=self.bodies[key])


class FakeClock:
    #clock that records the requested duration and advances time. 

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeNow:
    #advances one second per call

    def __init__(self, start: dt.datetime) -> None:
        self.current = start

    def __call__(self) -> dt.datetime:
        self.current += dt.timedelta(seconds=1)
        return self.current


def make_client(fake: FakeDb, clock: FakeClock, **kwargs) -> DbClient:
    kwargs.setdefault("jitter", lambda: 0.0)
    return DbClient(
        BASE,
        "test-token",
        transport=httpx.MockTransport(fake.handler),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )
