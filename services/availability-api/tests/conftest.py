"""Test fixtures. Integration tests run against a real PostgreSQL (the brief asks
for the real dependency), pointed to by TEST_DATABASE_URL. They are skipped, not
faked, when no database is available."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import golden_telemetry
import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app.config import Settings
from app.main import create_app

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
VENUE = "venue-001"

# Telemetry goes to memory in tests, so we can assert on what the service emits.
METRICS = InMemoryMetricReader()
golden_telemetry.configure(metric_readers=[METRICS])


def metric_points(name: str) -> list:
    points = []
    for rm in METRICS.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    points.extend(m.data.data_points)
    return points


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; skipping database tests")
    with psycopg.connect(url) as conn:
        conn.execute(SCHEMA.read_text())
    return url


@pytest.fixture
def tomorrow():
    return (datetime.now(timezone.utc) + timedelta(days=1)).date()


@pytest.fixture
def slot_ids(db_url, tomorrow) -> list[int]:
    """Three slots tomorrow at 10:00, 10:30, 11:00 UTC, on a clean database."""
    base = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 10, tzinfo=timezone.utc)
    with psycopg.connect(db_url) as conn:
        conn.execute("TRUNCATE holds, slots RESTART IDENTITY")
        ids = []
        for i in range(3):
            start = base + timedelta(minutes=30 * i)
            cur = conn.execute(
                "INSERT INTO slots (venue_id, starts_at, ends_at) VALUES (%s, %s, %s) RETURNING id",
                (VENUE, start, start + timedelta(minutes=30)),
            )
            ids.append(cur.fetchone()[0])
        return ids


class FakeCapacity:
    """In-process stand-in for the venue-capacity service (httpx MockTransport)."""

    def __init__(self) -> None:
        self.status = 200
        self.accepting = True
        self.raise_timeout = False
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.raise_timeout:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(self.status, json={"accepting_bookings": self.accepting})


@pytest.fixture
def fake_capacity() -> FakeCapacity:
    return FakeCapacity()


@pytest.fixture
def client(db_url, fake_capacity):
    settings = Settings(
        database_url=db_url,
        capacity_url="http://capacity.test",
        capacity_token="test-token",
        breaker_failure_threshold=3,
        breaker_reset_s=60,
    )
    app = create_app(settings, capacity_transport=httpx.MockTransport(fake_capacity.handler))
    with TestClient(app) as c:
        yield c
