from concurrent.futures import ThreadPoolExecutor

from tests.conftest import VENUE


def get_availability(client, day, venue=VENUE):
    return client.get(f"/venues/{venue}/availability", params={"date": day.isoformat()})


def test_lists_open_slots(client, slot_ids, tomorrow):
    r = get_availability(client, tomorrow)
    assert r.status_code == 200
    assert [s["slot_id"] for s in r.json()["slots"]] == slot_ids


def test_unknown_venue_has_no_slots(client, slot_ids, tomorrow):
    r = get_availability(client, tomorrow, venue="venue-999")
    assert r.status_code == 200
    assert r.json()["slots"] == []


def test_invalid_input_is_rejected(client, slot_ids):
    assert client.get(f"/venues/{VENUE}/availability", params={"date": "not-a-date"}).status_code == 422
    assert client.get("/venues/bad$id/availability", params={"date": "2030-01-01"}).status_code == 422


def test_hold_removes_slot_from_availability(client, slot_ids, tomorrow):
    r = client.post(f"/venues/{VENUE}/holds", json={"slot_id": slot_ids[0]})
    assert r.status_code == 201
    assert r.json()["slot_id"] == slot_ids[0]
    listed = [s["slot_id"] for s in get_availability(client, tomorrow).json()["slots"]]
    assert listed == slot_ids[1:]


def test_second_hold_on_same_slot_conflicts(client, slot_ids):
    assert client.post(f"/venues/{VENUE}/holds", json={"slot_id": slot_ids[0]}).status_code == 201
    r = client.post(f"/venues/{VENUE}/holds", json={"slot_id": slot_ids[0]})
    assert r.status_code == 409
    assert r.json()["detail"] == "already_held"


def test_concurrent_holds_only_one_wins(client, slot_ids):
    def hold(_):
        return client.post(f"/venues/{VENUE}/holds", json={"slot_id": slot_ids[1]}).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(hold, range(8)))
    assert codes.count(201) == 1
    assert codes.count(409) == 7


def test_hold_on_unknown_slot_or_wrong_venue_is_404(client, slot_ids):
    assert client.post(f"/venues/{VENUE}/holds", json={"slot_id": 999999}).status_code == 404
    assert client.post("/venues/venue-002/holds", json={"slot_id": slot_ids[0]}).status_code == 404


def test_closed_venue_returns_no_slots(client, slot_ids, tomorrow, fake_capacity):
    fake_capacity.accepting = False
    assert get_availability(client, tomorrow).json()["slots"] == []


def test_capacity_errors_fail_closed_with_retry_after(client, slot_ids, tomorrow, fake_capacity):
    fake_capacity.status = 500
    r = get_availability(client, tomorrow)
    assert r.status_code == 503
    assert r.json()["reason"] == "upstream_error"
    assert "Retry-After" in r.headers


def test_rejected_credentials_are_reported(client, slot_ids, tomorrow, fake_capacity):
    fake_capacity.status = 401
    assert get_availability(client, tomorrow).json()["reason"] == "auth_rejected"


def test_breaker_stops_calling_failing_dependency(client, slot_ids, tomorrow, fake_capacity):
    fake_capacity.raise_timeout = True
    reasons = [get_availability(client, tomorrow).json()["reason"] for _ in range(6)]
    # threshold is 3: three real attempts, then the breaker answers without calling out
    assert reasons[:3] == ["timeout"] * 3
    assert reasons[3:] == ["circuit_open"] * 3
    assert fake_capacity.calls == 3


def test_readiness_does_not_depend_on_capacity(client, slot_ids, fake_capacity):
    fake_capacity.status = 503
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_capacity_call_has_a_total_deadline(db_url, slot_ids, tomorrow):
    """A dependency that trickles its response must not hold us past the deadline,
    even though no single httpx phase times out."""
    import asyncio
    import time

    import httpx
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(1.0)
            return httpx.Response(200, json={"accepting_bookings": True})

    settings = Settings(database_url=db_url, capacity_url="http://capacity.test",
                        capacity_token="t", capacity_timeout_s=0.15)
    with TestClient(create_app(settings, capacity_transport=SlowTransport())) as c:
        start = time.monotonic()
        r = c.get(f"/venues/{VENUE}/availability", params={"date": tomorrow.isoformat()})
        elapsed = time.monotonic() - start
    assert r.status_code == 503
    assert r.json()["reason"] == "timeout"
    assert elapsed < 0.5, f"took {elapsed:.2f}s; the deadline is 0.15s"
