import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.build_top_scorers import build

FIXTURES = Path(__file__).parent / "fixtures"


def build_fixture_db(db_path: str) -> dict:
    return build(
        str(FIXTURES / "goalscorers.csv"),
        db_path,
        results_path=str(FIXTURES / "results.csv"),
        shootouts_path=str(FIXTURES / "shootouts.csv"),
        former_names_path=str(FIXTURES / "former_names.csv"),
    )


def pytest_configure(config: pytest.Config) -> None:
    # app.main builds a module-level app on import; point it at the fixture database.
    db_path = os.path.join(tempfile.mkdtemp(), "fixture.db")
    build_fixture_db(db_path)
    os.environ["APP_DB_PATH"] = db_path


@pytest.fixture(scope="session")
def fixture_db() -> str:
    return os.environ["APP_DB_PATH"]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(fixture_db: str, clock: FakeClock) -> Callable[..., TestClient]:
    from app.config import Settings
    from app.main import create_app

    def make(cache_ttl: float = 60, rate_limit: float = 0) -> TestClient:
        settings = Settings(db_path=fixture_db, cache_ttl_seconds=cache_ttl,
                            rate_limit_per_minute=rate_limit)
        return TestClient(create_app(settings, clock=clock))

    return make


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
