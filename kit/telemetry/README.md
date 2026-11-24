# Telemetry standard

How a service on the golden path emits traces, metrics and logs. Follow this and your
service gets dashboards, SLOs and alerts from the kit with no extra work, and it won't
run up the observability bill.

## Adding telemetry to a service (Python)

1. Add the kit package to your image:
   ```dockerfile
   COPY kit/telemetry/python /tmp/golden-telemetry
   RUN pip install --no-cache-dir /tmp/golden-telemetry
   ```
2. One line in your app:
   ```python
   import golden_telemetry
   app = FastAPI()
   golden_telemetry.instrument(app)
   ```
3. Set these environment variables in your Deployment:

   | Variable | Value | Why |
   |----------|-------|-----|
   | `OTEL_SERVICE_NAME` | your service name, e.g. `availability-api` | becomes the `job` label with the namespace |
   | `POD_NAME` | from the downward API: `valueFrom.fieldRef.fieldPath: metadata.name` | used in the next line |
   | `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=<team namespace>,deployment.environment.name=<env>,service.instance.id=$(POD_NAME),k8s.pod.name=$(POD_NAME)` | groups services by team; the `instance` label is the pod name (otherwise it's a random id per process) |
   | `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector.observability.svc:4318` | the platform collector |
   | `OTEL_TRACES_SAMPLER_ARG` | `0.1` (default) | fraction of traces kept |
   | `LOG_LEVEL` | `WARNING` (default) | see logs below |

That's it. Inbound HTTP, outbound HTTP (httpx) and database calls (psycopg) are traced
and measured automatically. Other languages: use the OpenTelemetry SDK for your language
with the same variables and the rules below; the collector enforces the label rules
either way.

## Naming rules

- **Use OpenTelemetry semantic conventions** for anything they cover. HTTP uses the
  stable conventions: `http.server.request.duration` in **seconds**. Prometheus sees it as
  `http_server_request_duration_seconds`.
- **Custom metrics** are `<thing>.<measurement>` in lower case with dots, plural nouns
  for counters, a unit, and a description. Example: `capacity.requests` (counter,
  unit `{request}`), `capacity.circuit.state` (gauge).
- **Don't put the service name in the metric name.** It's already in the `job` label.

## Label rules (the expensive part)

Every unique combination of label values is a separate time series, and the platform
bills per series. One label with an unbounded value (a venue id, a user id, a URL) can
turn one metric into millions of series.

- **Allowed:** values from a small, fixed set you could list on one screen: HTTP method,
  route **template** (`/venues/{venue_id}/availability`, never the filled-in path),
  status code, an `outcome` with a handful of values.
- **Never:** ids of any kind, URLs or paths, query strings, email addresses, timestamps,
  free-text error messages, IPs.
- **Rule of thumb:** if you can't write down every possible value, it's not a label.
  Put it on the trace span instead; spans are sampled, so high-cardinality detail is cheap there.

How this is enforced, in two layers:
1. **In the SDK** (`golden_telemetry.views()`): HTTP metrics keep only method, route and
   status code; everything else is dropped before it leaves the process. Body-size
   histograms are dropped entirely.
2. **In the collector** (`transform/cardinality`): known-dangerous attributes (`url.*`,
   `user.id`, `client.address`, ports) are deleted from all metrics, even from services
   that don't use the kit.

## Latency buckets

HTTP duration histograms use these bucket boundaries (seconds):
`0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5, 5`.
**0.3 must stay**: it is the default SLO latency threshold, and "faster than 300 ms" can
only be computed exactly at a bucket boundary. OpenTelemetry's default buckets don't
include it. If your SLO uses a different threshold, add that boundary.

## Traces

- **Head sampling at 10%**, parent-based: if the caller already decided to sample a
  request, we follow that decision so traces stay complete across services.
- Health and readiness probes (`/healthz`, `/readyz`) are not traced or measured.
- SQL statements are recorded without parameter values; query strings and authorization
  headers are stripped at the collector.
- Retention: **24 hours** in Tempo. Traces are for debugging now, not for history;
  metrics hold the history.

## Logs

- **Default level WARNING.** Requests are covered by metrics and traces; per-request INFO
  logs at 1,000 req/s are the quickest way to run up a logging bill. Raise `LOG_LEVEL`
  temporarily when debugging.
- **No HTTP access logs** (Uvicorn `--no-access-log`), for the same reason.
- JSON, one object per line, with `trace_id` and `span_id` when inside a request, so a log
  line links straight to its trace.

## Cost defaults at a glance

| Control | Default | Where |
|---------|---------|-------|
| Metric labels | allow-list per metric | SDK views + collector |
| Trace sampling | 10% head, parent-based | SDK (`OTEL_TRACES_SAMPLER_ARG`) |
| Trace retention | 24 h | Tempo |
| Metric retention | 30 days or 4 GB, whichever first | Prometheus |
| Stale series | dropped after 5 min | collector Prometheus exporter |
| Log level | WARNING, no access logs | SDK + container command |

## Checking it locally

- Metrics: Grafana → Explore → Prometheus, query
  `http_server_request_duration_seconds_count{job="<namespace>/<service>"}`.
- Traces: Grafana → Explore → Tempo, search by service name.
- What the collector receives: `kubectl -n observability port-forward svc/otel-collector 8889`
  then open `http://localhost:8889/metrics`.
