"""The service emits what its SLO and runbook depend on."""
from tests.conftest import VENUE, metric_points


def outcome_counts():
    return {p.attributes["outcome"]: p.value for p in metric_points("capacity.requests")}


def test_capacity_outcomes_are_counted(client, slot_ids, tomorrow, fake_capacity):
    before = outcome_counts()
    client.get(f"/venues/{VENUE}/availability", params={"date": tomorrow.isoformat()})
    fake_capacity.status = 500
    client.get(f"/venues/{VENUE}/availability", params={"date": tomorrow.isoformat()})
    after = outcome_counts()
    assert after.get("ok", 0) - before.get("ok", 0) == 1
    assert after.get("upstream_error", 0) - before.get("upstream_error", 0) == 1


def test_http_slis_use_route_template_not_venue_id(client, slot_ids, tomorrow):
    for venue in ("venue-001", "venue-777"):
        client.get(f"/venues/{venue}/availability", params={"date": tomorrow.isoformat()})
    routes = {p.attributes.get("http.route") for p in metric_points("http.server.request.duration")}
    assert "/venues/{venue_id}/availability" in routes
    assert not any("venue-" in (r or "") for r in routes)


def test_breaker_state_gauge_reports_open(client, slot_ids, tomorrow, fake_capacity):
    fake_capacity.raise_timeout = True
    for _ in range(3):
        client.get(f"/venues/{VENUE}/availability", params={"date": tomorrow.isoformat()})
    values = [p.value for p in metric_points("capacity.circuit.state")]
    assert 2 in values  # 2 = open
