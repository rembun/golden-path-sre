"""Golden path telemetry for Python HTTP services.

One call gives a service traces, metrics and structured logs that follow the
platform's telemetry standard (docs/telemetry-standard.md):

    import golden_telemetry
    app = FastAPI()
    golden_telemetry.instrument(app)

What it enforces, so teams don't have to remember:
  * Metric and span names follow OpenTelemetry semantic conventions (stable HTTP).
  * HTTP duration histograms have a bucket at exactly 0.3 s, the SLO threshold.
  * HTTP metrics keep ONLY method, route template and status code as labels.
    Anything else (URLs, user ids, venue ids) is dropped at the source, which is
    the main defence against label-cardinality bills.
  * Health and readiness probes are excluded from traces and metrics, so probe
    traffic never pollutes SLIs.
  * Traces are head-sampled (default 10%) and logs default to WARNING.

Configuration is standard OpenTelemetry environment variables (OTEL_SERVICE_NAME,
OTEL_RESOURCE_ATTRIBUTES, OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_TRACES_SAMPLER_ARG)
plus LOG_LEVEL. If OTEL_EXPORTER_OTLP_ENDPOINT is not set (unit tests, a laptop
without a collector), instrumentation is still applied but nothing is exported.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Iterable

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Seconds. 0.3 is the platform's default latency SLO threshold: it MUST be a
# bucket boundary, or "faster than 300 ms" cannot be computed exactly.
LATENCY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0)

# The only labels allowed on HTTP metrics. All bounded: a handful of methods,
# one value per route template, ~20 status codes.
HTTP_SERVER_LABELS = {"http.request.method", "http.route", "http.response.status_code"}
HTTP_CLIENT_LABELS = {"http.request.method", "http.response.status_code", "server.address"}

PROBE_PATHS = "healthz,readyz,livez,metrics"

_DEFAULTS = {
    # Emit stable HTTP semantic conventions (http.server.request.duration, seconds).
    "OTEL_SEMCONV_STABILITY_OPT_IN": "http",
    # Cost control: keep 10% of traces, but always follow the caller's decision.
    "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
    "OTEL_TRACES_SAMPLER_ARG": "0.1",
}

_configured = False
_meter_provider: MeterProvider | None = None
_tracer_provider: TracerProvider | None = None
_registered_span_processors: list[SpanProcessor] = []


def views() -> list[View]:
    """Metric views that enforce the standard. Exposed for testing."""
    return [
        View(
            instrument_name="http.server.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(LATENCY_BUCKETS_S),
            attribute_keys=HTTP_SERVER_LABELS,
        ),
        View(
            instrument_name="http.client.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(LATENCY_BUCKETS_S),
            attribute_keys=HTTP_CLIENT_LABELS,
        ),
        # Request/response body size histograms add series without helping SLOs.
        View(instrument_name="http.server.request.body.size", aggregation=_drop()),
        View(instrument_name="http.server.response.body.size", aggregation=_drop()),
        View(instrument_name="http.client.request.body.size", aggregation=_drop()),
        View(instrument_name="http.client.response.body.size", aggregation=_drop()),
    ]


def _drop():
    from opentelemetry.sdk.metrics.view import DropAggregation

    return DropAggregation()


def configure(
    metric_readers: Iterable[MetricReader] | None = None,
    span_processors: Iterable[SpanProcessor] | None = None,
) -> None:
    """Configure global providers once and allow additional readers/processors."""
    global _configured, _meter_provider, _tracer_provider

    for key, value in _DEFAULTS.items():
        os.environ.setdefault(key, value)

    readers = list(metric_readers or [])
    processors = list(span_processors or [])

    if not _configured:
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            interval_ms = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "15000"))
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(),
                    export_interval_millis=interval_ms,
                )
            )
            processors.append(BatchSpanProcessor(OTLPSpanExporter()))

        _configured = True

    if readers or processors:
        resource = Resource.create()

        if _tracer_provider is None:
            _tracer_provider = TracerProvider(resource=resource)
            trace.set_tracer_provider(_tracer_provider)

        for processor in processors:
            if processor not in _registered_span_processors:
                _tracer_provider.add_span_processor(processor)
                _registered_span_processors.append(processor)

        if _meter_provider is None:
            _meter_provider = MeterProvider(
                resource=resource,
                metric_readers=readers,
                views=views(),
            )
            metrics.set_meter_provider(_meter_provider)
        else:
            for reader in readers:
                _meter_provider.add_metric_reader(reader)

    _configure_logging()


def instrument(app) -> None:
    """Configure providers (if not done yet) and instrument a FastAPI app plus
    its outbound HTTP (httpx) and database (psycopg) calls."""
    configure()
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls=PROBE_PATHS)
    if not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry:
        HTTPXClientInstrumentor().instrument()
    if not PsycopgInstrumentor().is_instrumented_by_opentelemetry:
        # Cost control: never record SQL parameters (they can contain personal data).
        PsycopgInstrumentor().instrument(enable_commenter=False)


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, with trace and span ids for log/trace correlation."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            entry["trace_id"] = format(ctx.trace_id, "032x")
            entry["span_id"] = format(ctx.span_id, "016x")
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _configure_logging() -> None:
    # Cost control: WARNING by default. Per-request INFO logs at 1,000 req/s are
    # the fastest way to run up a logging bill; metrics and traces cover requests.
    level = os.environ.get("LOG_LEVEL", "WARNING").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
