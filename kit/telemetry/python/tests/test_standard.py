"""The telemetry standard, enforced as tests. If someone changes the kit in a way
that breaks a rule (loses the 0.3 s bucket, leaks a high-cardinality label,
starts tracing health checks), these fail."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import golden_telemetry

READER = InMemoryMetricReader()
SPANS = InMemorySpanExporter()


@pytest.fixture(scope="module")
def client():
    os.environ["OTEL_SERVICE_NAME"] = "kit-test"
    os.environ["OTEL_TRACES_SAMPLER_ARG"] = "1.0"  # keep every trace in tests
    golden_telemetry.configure(metric_readers=[READER], span_processors=[SimpleSpanProcessor(SPANS)])

    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/venues/{venue_id}/things")
    def things(venue_id: str):
        return {"venue_id": venue_id}

    golden_telemetry.instrument(app)
    with TestClient(app) as c:
        yield c


def http_server_points():
    data = READER.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "http.server.request.duration":
                    return m, list(m.data.data_points)
    raise AssertionError("http.server.request.duration not emitted")


def test_uses_stable_http_metric_in_seconds(client):
    client.get("/venues/venue-001/things")
    metric, _ = http_server_points()
    assert metric.unit == "s"


def test_latency_histogram_has_slo_bucket(client):
    client.get("/venues/venue-001/things")
    _, points = http_server_points()
    assert 0.3 in points[0].explicit_bounds


def test_only_allowed_labels_and_route_template(client):
    for v in ("venue-001", "venue-002", "venue-003"):
        client.get(f"/venues/{v}/things")
    _, points = http_server_points()
    for p in points:
        assert set(p.attributes) <= golden_telemetry.HTTP_SERVER_LABELS
    routes = {p.attributes.get("http.route") for p in points}
    # Three different venue ids collapse into ONE series per route template.
    assert routes == {"/venues/{venue_id}/things"}


def test_probes_are_not_measured_or_traced(client):
    SPANS.clear()
    client.get("/healthz")
    assert SPANS.get_finished_spans() == ()
    _, points = http_server_points()
    assert all(p.attributes.get("http.route") != "/healthz" for p in points)


def test_requests_are_traced(client):
    SPANS.clear()
    client.get("/venues/venue-001/things")
    names = [s.name for s in SPANS.get_finished_spans()]
    assert any("/venues/{venue_id}/things" in n for n in names)
