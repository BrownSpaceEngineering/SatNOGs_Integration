import datetime as dt
import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import httpx

from satnogs_integration import __main__ as cli
from satnogs_integration.__main__ import main, pause_after
from satnogs_integration.config import ConfigError
from satnogs_integration.store import Store
from satnogs_integration.timeutil import parse_iso8601, utcnow
from tests.support import NORAD, PAGE

POLL = 1000  # longer than any client retry wait


def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SATNOGS_DB_TOKEN", "test-token")
    monkeypatch.setenv("NORAD_CAT_ID", str(NORAD))
    monkeypatch.setenv("DECODER", "crocube")
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("INGEST_START", "2026-10-11T00:00:00Z")
    monkeypatch.setenv("POLL_SECONDS", str(POLL))


class Sleeper:
    # records sleeps; raises ctrl-c on the nth pause between cycles
    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.sleeps: list[float] = []

    @property
    def pauses(self) -> list[float]:
        return [s for s in self.sleeps if s >= POLL]

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.pauses) >= self.stop_after:
            raise KeyboardInterrupt


def serve(fake_db, sleeper):
    return main([], transport=httpx.MockTransport(fake_db.handler), sleep=sleeper)


def start_param(request: httpx.Request) -> str:
    return parse_qs(urlparse(str(request.url)).query)["start"][0]


def test_once_sweeps_and_decodes(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    assert (
        main(
            ["--once", "--max-pages", "1"],
            transport=httpx.MockTransport(fake_db.handler),
        )
        == 0
    )
    with Store(tmp_path / "state.db") as store:
        counts = store.counts(NORAD)
    assert (
        counts["frames"] == PAGE
        and counts["pending"] == 0
        and counts["sweep_running"] is True
    )


def test_configuration_error_exits_2(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)
    monkeypatch.setenv("SATNOGS_DB_TOKEN", "")
    assert main(["--once"]) == 2


def test_stats_prints_json(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    assert main(["--stats"]) == 0
    assert json.loads(capsys.readouterr().out)["frames"] == 0


def test_pause_after():
    assert pause_after(600, 0) == 600
    assert pause_after(600, 1) == 1200 and pause_after(600, 3) == 4800
    assert pause_after(600, 50) == 6 * 3600  # capped at six hours
    assert pause_after(600, 0, retry_after=86400) == 86400
    assert pause_after(86400, 2) == 86400  # a long poll is never shortened


def test_refused_key_stops_the_service(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    fake_db.queue("page1", httpx.Response(401, json={"detail": "Invalid token."}))
    sleeper = Sleeper(stop_after=1)
    assert serve(fake_db, sleeper) == 3
    assert len(fake_db.calls) == 1 and sleeper.sleeps == []


def test_a_failed_cycle_doubles_the_pause(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    fake_db.queue("page1", *[httpx.Response(500)] * 7)  # the first try and 6 retries
    sleeper = Sleeper(stop_after=2)
    assert serve(fake_db, sleeper) == 130
    assert sleeper.pauses == [2 * POLL, POLL]  # resets after a good cycle
    with Store(tmp_path / "state.db") as store:
        assert store.counts(NORAD)["frames"] == 2 * PAGE


def test_frames_saved_before_a_failure_are_decoded(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    fake_db.queue("page2", *[httpx.Response(500)] * 7)  # the first try and 6 retries
    transport = httpx.MockTransport(fake_db.handler)
    assert main(["--once"], transport=transport, sleep=lambda seconds: None) == 1
    with Store(tmp_path / "state.db") as store:
        counts = store.counts(NORAD)
    assert counts["frames"] == PAGE and counts["pending"] == 0


def test_a_long_retry_after_sets_the_pause(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    fake_db.queue("page1", httpx.Response(429, headers={"Retry-After": "86400"}))
    sleeper = Sleeper(stop_after=1)
    assert serve(fake_db, sleeper) == 130
    assert sleeper.sleeps == [86400] and len(fake_db.calls) == 1


def test_only_the_first_sweep_rescans_the_full_overlap(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    sleeper = Sleeper(stop_after=3)
    assert serve(fake_db, sleeper) == 130
    first, second, third = (start_param(fake_db.calls[i]) for i in (0, 3, 6))
    assert first == "2026-10-11T00:00:00Z"  # INGEST_START
    # later sweeps use the 2 h overlap, not 48 h
    for start in (second, third):
        age = utcnow() - parse_iso8601(start)
        assert dt.timedelta(hours=1, minutes=59) < age < dt.timedelta(hours=3)


def test_values_are_stored_with_units(monkeypatch, tmp_path, fake_db):
    env(monkeypatch, tmp_path)
    transport = httpx.MockTransport(fake_db.handler)
    assert main(["--once", "--max-pages", "1"], transport=transport) == 0
    with sqlite3.connect(tmp_path / "state.db") as db:
        row = db.execute(
            "SELECT raw, value, unit FROM telemetry WHERE field = 'obc_bat'"
        ).fetchone()
    assert row[0] == 8205 and abs(row[1] - 8.205) < 1e-9 and row[2] == "V"


def test_a_bad_calibration_file_exits_2(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path)

    def broken(decoder):
        raise ConfigError("crocube.toml: bad")

    monkeypatch.setattr(cli, "load_calibrations", broken)
    assert main(["--once"]) == 2
