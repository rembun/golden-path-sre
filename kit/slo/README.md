# SLO as code

Write one spec file for your service; the kit generates the Prometheus recording rules and
multi-window burn-rate alerts. Adding a service means adding one file.

## Add your service

1. Copy [availability-api.yaml](availability-api.yaml) to `kit/slo/<your-service>.yaml` and edit it.
2. Generate the rules and commit the result:
   ```bash
   python kit/slo/generate.py --spec kit/slo/<your-service>.yaml \
     --output platform/alerts/generated/<your-service>-slo.yaml
   ```
   (For this repo's service: `make slo-generate`.)
   and the dashboard:
   ```bash
   python kit/slo/generate_dashboard.py --spec kit/slo/<your-service>.yaml \
     --output platform/dashboards/generated/<your-service>-dashboard.yaml
   ```
   (For this repo's service, `make slo-generate` does both.)
3. Apply them: `kubectl apply -f platform/alerts/generated/<your-service>-slo.yaml -f platform/dashboards/generated/<your-service>-dashboard.yaml`.
   Prometheus picks up the `PrometheusRule`, and Grafana the dashboard, from any namespace.

The pipeline's policy gate requires a spec for every service exposed through an Ingress, and fails
if the committed rules don't match what the spec generates.

## The spec

```yaml
service: availability-api            # used in rule and alert names
namespace: bookings                  # where the PrometheusRule is created (your team's namespace)
prometheus_job: bookings/availability-api   # <service.namespace>/<service.name> from your telemetry
runbook_url: https://…/docs/runbooks/dependency-failure.md   # linked from every alert

slo:
  name: availability-availability
  description: Availability requests succeed within 300 milliseconds
  target: 0.9985                     # 99.85% good, so the error budget is 0.15%
  window: 30d

sli:
  histogram: http_server_request_duration_seconds   # from the telemetry kit
  latency_threshold_seconds: 0.3     # must be a histogram bucket boundary (it is in the kit)
  bad_status_regex: "5.."            # 4xx are the client's problem, not ours

burn_rate_alerts:                    # Google SRE workbook defaults; keep unless you have a reason
  - { short_window: 5m,  long_window: 1h,  burn_rate: 14.4 }   # pages
  - { short_window: 30m, long_window: 6h,  burn_rate: 6 }      # pages
  - { short_window: 2h,  long_window: 24h, burn_rate: 3 }      # ticket
  - { short_window: 6h,  long_window: 3d,  burn_rate: 1 }      # ticket
```

**The SLI:** a request is good if it is not a 5xx **and** it completes within the latency
threshold. Failures and slow responses both spend the budget, because customers experience both.

## What gets generated

For every window used (the SLO window plus every alert window), four recording rules:

| Rule | Meaning |
|------|---------|
| `slo:<service>:requests:rate<window>` | all requests per second |
| `slo:<service>:good_requests:rate<window>` | good requests per second |
| `slo:<service>:error_ratio:rate<window>` | 1 − good / all |
| `slo:<service>:burn_rate:rate<window>` | error ratio ÷ error budget (1 = spending exactly on budget) |

Then one alert per `burn_rate_alerts` entry, firing when **both** windows are above the burn rate.
Burn rates of 6 or more are `severity: critical` (page); lower ones are `warning` (ticket).
Windows of a day or more are evaluated every 5 minutes instead of every 30 seconds, because
they're expensive to compute and change slowly.

Useful queries:
- Budget left this month: `1 - slo:availability_api:error_ratio:rate30d / 0.0015`
- Current burn: `slo:availability_api:burn_rate:rate1h`

## Dashboard

The same spec generates a Grafana dashboard, delivered as a ConfigMap labelled
`grafana_dashboard: "1"` in your namespace (Grafana loads those from every namespace). It's built
on the recording rules above, so the dashboard and the alerts can never disagree.

| Section | Panels |
|---------|--------|
| SLO | Error budget left, SLI over the SLO window, current burn rate, alerts firing; burn rate over time with the paging thresholds drawn as lines |
| Traffic and latency | Requests per second by status code; p50 and p99 latency with the SLO threshold drawn as a line |
| Your service | Anything in the spec's optional `dashboard.extra_panels` |

Service-specific panels are a query and a title; `type: state` turns a numeric state into coloured
bands (the circuit breaker uses it):

```yaml
dashboard:
  extra_panels:
    - title: Venue-capacity calls by outcome
      query: sum by (outcome) (rate(capacity_requests_total{job="bookings/availability-api"}[1m]))
      legend: "{{outcome}}"
      unit: reqps
    - title: Circuit breaker state per pod
      type: state
      query: max by (instance) (capacity_circuit_state{job="bookings/availability-api"})
      legend: "{{instance}}"
      states:
        - { value: 0, text: closed, color: green }
        - { value: 2, text: open, color: red }
```

The dashboard is marked read-only: change the spec and regenerate, so it never drifts from what's in
git. The 30-day panels fill in within 5 minutes of startup (long windows are evaluated every 5 minutes).

## Checks

- `make slo-test`: unit tests for both generators (valid names, every referenced series is recorded,
  severities, runbook links; dashboard panels don't overlap, thresholds match the spec).
- `make slo-verify`: `promtool check rules` on the rules **and on every dashboard query**, plus
  `promtool` unit tests that replay incidents: a retry storm (the fast-burn page must fire) and a
  flapping circuit breaker (the cause alert must fire). See [tests/promtool](tests/promtool).

## Why not Sloth or Pyrra?

Both are good tools that do this. The generator here is under 200 lines with no runtime component,
its output is a plain `PrometheusRule` that's committed and reviewable in a pull request, and the
spec can grow fields we need (like `runbook_url`) without waiting on an upstream project. If the
platform outgrows it, each spec translates directly into a Sloth spec.
