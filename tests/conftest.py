import pytest

from satnogs_integration.store import Store
from tests.support import FakeClock, FakeDb, make_client


@pytest.fixture
def fake_db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(fake_db, clock):
    with make_client(fake_db, clock, min_interval=0) as c:
        yield c


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as s:
        yield s
