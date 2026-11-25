# Retry storm: onboarding exercise

The brief's scenario: venue-capacity has a short outage; the website and app retry aggressively,
so traffic to the API roughly triples within a minute and venue-capacity struggles to recover.
This page shows how to reproduce it, which alert fires and why, and the mitigation built into the
service. The response steps are in the [runbook](runbooks/dependency-failure.md).

## Reproduce it

```bash
make up                       # or: make up RUNTIME=k3s
make load SCENARIO=storm      # add RATE_SCALE=0.5 on a small machine
```

The storm scenario in [load/availability.js](../load/availability.js) runs for 12 minutes:

| Time | What happens |
|------|--------------|
| 0–3 min | Steady ~70 req/s. Everything good. |
| 3–4 min | The script flips venue-capacity into `outage` (its chaos switch). Every failed call is retried twice immediately, ignoring `Retry-After`, so requests to the API roughly triple. |
| 4 min | The script switches venue-capacity back to `normal`. |
| 4–12 min | Recovery. Without protection, the retries would keep venue-capacity above its in-flight limit (`MAX_INFLIGHT=40`) and it would stay down. |

Watch it in Grafana on the **availability-api · SLO** dashboard (generated from the SLO spec):
burn rate crossing the paging lines, 503s and request rate jumping, capacity calls switching to
`circuit_open`, and the breaker state per pod. Alerts are also in Alertmanager: `kubectl -n observability port-forward svc/kps-kube-prometheus-stack-alertmanager 9093`
then http://localhost:9093.

## The alert that fires, and why that signal

**`AvailabilityApiSLOBurnRate5M`** (critical, pages): the error budget is burning faster than
14.4× over both the last 5 minutes and the last hour.

- **It measures what customers get.** The SLI counts a request as bad if it fails *or* takes
  longer than 300 ms, which is exactly what someone checking availability experiences. It doesn't
  care why: a dependency outage, a bad deploy and a slow database all page the same way, and
  nothing that customers don't notice pages anyone.
- **It's fast where it matters.** During the outage roughly a third of requests fail, a burn rate
  of about 200×. The 5-minute window crosses 14.4× within a couple of rule evaluations (about
  1–2 minutes including scrape and evaluation intervals). At the Thursday peak the whole monthly
  budget would last about 4.5 minutes, so slower alerting would page after the budget had gone.
- **It doesn't flap or fire on blips.** The 1-hour window must also exceed 14.4×, so a few seconds
  of errors don't page; and the alert clears soon after recovery because the 5-minute window
  recovers quickly.
- **It's tested.** [kit/slo/tests/promtool](../kit/slo/tests/promtool/availability-api-retry-storm.test.yaml)
  replays this storm through the generated rules with `promtool`: no page before the outage, a
  page during it, and a 5-minute burn rate of exactly 200× when 30% of requests fail. Run it with
  `make slo-verify`; it also runs in CI.

`AvailabilityDependencyCircuitOpen` (once the breaker has been refusing requests for over a minute;
DECISIONS.md D29 explains why it watches refused requests rather than the breaker's state) and `AvailabilityDependencyTimeouts` fire alongside it as
**warnings**: they explain the cause in the same view but don't page separately. One incident,
one page.

## The mitigation built into the service

A **circuit breaker with a short timeout and no retries** on the venue-capacity call
([app/breaker.py](../services/availability-api/app/breaker.py), [app/capacity.py](../services/availability-api/app/capacity.py)):

- **Timeout 150 ms**, so a slow dependency can't push our responses past 300 ms.
- **No retries inside the API**, so one customer request is at most one dependency call.
- **After 5 consecutive failures the breaker opens.** The API then answers `503` with
  `Retry-After: 5` in about a millisecond, without calling venue-capacity at all.
- **After the configured reset interval (180 seconds in the deployed cluster), one trial request per pod** checks whether venue-capacity is back. Success
  closes the breaker; failure keeps it open.

What that does in the storm: tripled client traffic hits **our** API, which absorbs it cheaply,
while venue-capacity receives roughly one request per pod every 5 seconds instead of three times
its normal load. That's what lets it recover. The effect shows in
`sum by (outcome) (rate(capacity_requests_total[1m]))`: during the outage, calls to venue-capacity
drop to almost nothing (`circuit_open` dominates) instead of tripling.

Verified outside the cluster with the real API and fake service: with the dependency down and
clients retrying twice, 16,245 requests (5,415 customer attempts, each retried twice) were all
answered with fast `503`s at around 2 ms each.

### Optional: see the difference without the breaker

```bash
kubectl -n bookings set env deployment/availability-api BREAKER_FAILURE_THRESHOLD=1000000
make load SCENARIO=storm
kubectl -n bookings set env deployment/availability-api BREAKER_FAILURE_THRESHOLD=5
```

Without the breaker, every retry reaches venue-capacity: it stays saturated after the outage ends,
and the API's responses wait on the 150 ms timeout instead of failing fast.

## Evidence to capture (for the review)

Save screenshots in [docs/evidence/](evidence/):

- [x] The **availability-api · SLO** dashboard during the outage

- [ ] Alertmanager showing `AvailabilityApiSLOBurnRate5M` firing during the outage (specific alert screenshot still outstanding)
- [x] Burn rate over time: `slo:availability_api:burn_rate:rate5m`
- [x] Capacity call outcomes: `sum by (outcome) (rate(capacity_requests_total[1m]))`
- [x] Request rate tripling: `sum(rate(http_server_request_duration_seconds_count{job="bookings/availability-api"}[1m]))`
- [x] The k6 summary at the end of `make load SCENARIO=storm`

## Observed test evidence (24 November 2026)

The captured k6 storm run produced the following results:

| Metric | Observed result |
|---|---:|
| HTTP requests | 23,250 |
| Average request rate | 32.29 req/s |
| SLO-good requests | 51.09% |
| HTTP failures | 48.89% |
| p95 latency | 26.15 ms |
| p99 latency | 30.82 ms |
| Client retries | 7,581 |
| Maximum active VUs | 5 |

The run failed its configured 99.85% SLO-good-request threshold. These results demonstrate
the observed behaviour under this test run; they do not establish that the service meets
its SLO under storm conditions or validate peak production capacity.

Raw output: [k6-storm-summary.txt](evidence/k6-storm-summary.txt).

Screenshots and supporting evidence are stored in [evidence/picture/](evidence/picture/).
