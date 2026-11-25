# Runbook: venue-capacity outage and retry storm

| | |
|---|---|
| **Service** | availability-api (namespace `bookings`) |
| **Alerts** | **Pages:** `AvailabilityApiSLOBurnRate5M` (14.4× burn over 5m and 1h), `AvailabilityApiSLOBurnRate30M` (6× over 30m and 6h). **Context only (warning):** `AvailabilityDependencyCircuitOpen`, `AvailabilityDependencyTimeouts` |
| **Owner** | Bookings team; venue-capacity is owned by its own team |
| **Last reviewed** | 25 November 2026, by project author |

## Customer impact

Customers can't see open slots on the website or app: availability returns `503` ("try again
shortly"). Holds on slots already shown still work, because they don't call venue-capacity.
The website and app retry failed calls immediately, so traffic to us roughly **triples within a
minute**. At normal traffic a full outage uses the monthly error budget in about an hour; during
the Thursday 09:00 launch peak it uses it in about **4.5 minutes**.

## Check (first 5 minutes)

Start with Grafana's **availability-api · SLO** dashboard: it shows all of the below on one screen.
The queries are here for Explore, or if Grafana is down.

1. **How fast is the budget burning?**
   `slo:availability_api:burn_rate:rate5m`. Bad: above 14.4 (a full outage reads several hundred).
2. **Is the cause venue-capacity?**
   `sum by (outcome) (rate(capacity_requests_total[1m]))`. Bad: `timeout`, `upstream_error`,
   `connection_error` or `circuit_open` instead of `ok`. `auth_rejected` means our credentials
   were revoked or rotated: a different fix (see Fix).
3. **Is the circuit breaker protecting the dependency?**
   `max by (instance) (capacity_circuit_state)`. `2` = open, `1` = half-open and `0` = closed.
   Check each pod separately (the `instance` label is the pod name): breaker state is local to
   each API process, not shared across replicas. An open breaker refuses dependency calls until
   its reset timeout expires.
   **Half-open on a pod with no traffic is harmless:** the breaker is waiting for the next request
   to test venue-capacity. What matters is whether it is *refusing* requests:
   `sum by (instance) (rate(capacity_requests_total{outcome="circuit_open"}[1m]))`
   (this is what `AvailabilityDependencyCircuitOpen` alerts on).
4. **Is it a retry storm?**
   `sum(rate(http_server_request_duration_seconds_count{job="bookings/availability-api"}[1m]))`.
   Bad: about 3× the normal rate for this time of day.
5. **Are we still fast?** (we should be, even while failing)
   `histogram_quantile(0.99, sum by (le) (rate(http_server_request_duration_seconds_bucket{job="bookings/availability-api"}[5m])))`.
   Expected: a few milliseconds while the breaker is open. If it's near 0.15 s, calls are
   waiting on the timeout and the breaker isn't opening.

## Mitigate (stop the impact first)

1. **Check breaker state on every pod** (check 3). The deployed configuration uses
   `BREAKER_RESET_S=180` (3 minutes). After that interval, a pod can enter half-open and allow
   a trial request. State is per process, so replicas can recover at different times. The
   implementation has a known half-open concurrency limitation; do not treat the trial as a
   cluster-wide single request guarantee.
2. **Find out whether venue-capacity is down or overloaded.** Contact its owners. In this
   environment: `kubectl -n bookings exec deploy/venue-capacity-fake -- python -c "import urllib.request as u; print(u.urlopen('http://localhost:8081/admin/mode').read().decode())"`.
   An `inflight` near `max_inflight` after the outage has ended means it's being kept down by load.
3. **If it's overloaded after coming back, avoid shortening the breaker reset period.** The
   current deployment uses 180 seconds. Do not change `BREAKER_RESET_S` during an incident
   without agreement from the availability-api owner: changing it triggers a deployment
   rollout, and newly started pods begin with closed breaker state.
4. **Cut the retry traffic at its source.** Ask the website and app owners to switch on backoff
   or honour `Retry-After`; at the edge, tighten the per-client rate limit on
   `/venues/*/availability` if one is configured.

**Don't:**
- **Don't restart or scale up availability-api to "fix" it.** New pods start with closed breakers
  and send their first requests straight to the struggling dependency. Our pods are healthy; the
  dependency isn't.
- Don't disable the breaker or add retries inside the API. Both multiply load on venue-capacity.

## Fix (remove the cause)

- **venue-capacity outage:** owned by the venue-capacity team. Track their incident and its cause.
- **`auth_rejected`:** our token was rejected. Check whether `CAPACITY_TOKEN` was rotated on their
  side; update the `availability-api` secret, then
  `kubectl -n bookings rollout restart deployment/availability-api`.
- **Retry behaviour (why a short outage became a storm):** the website and app should retry at
  most once, with exponential backoff and jitter, and honour `Retry-After`. Raise it with their
  owners; this is the fix that stops the incident repeating.

## Verify recovery

- `slo:availability_api:burn_rate:rate5m` back below 1.
- `capacity_circuit_state` is `0` (closed) on every instance.
- `capacity_requests_total` is almost entirely `outcome="ok"`.
- Request rate is back to normal for the time of day (the retries have stopped).
- The page resolves once the 5-minute window recovers; slower alerts can stay active for a while
  because their long windows still contain the incident. That's expected.

## Escalate and communicate

- Page the venue-capacity on-call if their service is down, or not recovering within 10 minutes.
- During the Thursday launch window, tell the product owner straight away: the budget can be gone
  in minutes.
- Status updates in the bookings incident channel every 15 minutes until resolved.

## Validation evidence — 25 November 2026

The dependency-outage storm scenario was executed against the rebuilt local
K3s environment using:

`make load SCENARIO=storm`

The test injected a 60-second venue-capacity outage. k6 reported the outage
starting at 10:09:42 UTC and ending at 10:10:42 UTC.

### k6 results

| Measurement | Observed result |
|---|---:|
| Total HTTP requests | 61,156 |
| SLO-good requests | 48,447 |
| SLO-good request rate | 79.22% |
| HTTP request failure rate | 20.77% |
| Client retries | 8,472 |
| Average throughput | 84.93 requests/s |
| Average HTTP latency | 14.56 ms |
| HTTP latency p95 | 25.35 ms |
| HTTP latency p99 | 27.17 ms |
| Maximum HTTP latency | 340.64 ms |
| Injected dependency outage | 60 seconds |

The k6 SLO threshold was `rate>=0.9985` (99.85%). The observed rate was
79.22%, so the threshold failed.

### Grafana observations

The availability-api · SLO dashboard, captured after the test, showed:

- SLI over 30 days: 79.117%.
- Error budget remaining: -13,822.2%.
- One-hour burn rate: 139.2x.
- Alerts firing: 2.
- The burn-rate graph showed a sharp increase around the injected outage,
  followed by elevated longer-window burn-rate values.

These are dashboard observations at the time of capture. The 30-day SLI
and error-budget figures are dashboard query results and should not be
treated as the k6 test's standalone measurements.

### Operational interpretation

The test demonstrates that the injected dependency outage and associated
request failures caused a significant availability SLO breach. The 8,472
client retries provide evidence of retry activity during the scenario.

The dashboard's elevated burn rate and firing alerts demonstrate that the
observability and alerting path detected the impact.

The test results alone do not establish that venue-capacity recovered
without continued retry pressure, or that every API replica's circuit
breaker transitioned through the expected states. Those behaviours must
be verified using the circuit-state and dependency-outcome queries in
this runbook.

### Follow-up actions

- Verify circuit-breaker state on every availability-api replica during
  and after the outage.
- Check whether dependency requests return to predominantly `ok` after
  the injected outage ends.
- Compare request rates before, during and after the outage to identify
  sustained retry amplification.
- Investigate why the SLO-good request rate fell to 79.22%, against the
  99.85% target.
- Capture the alert names and firing timestamps for the assessment evidence.


## Storm-test evidence — 25 November 2026

The dependency-outage storm scenario was executed against the local K3s environment using `make load SCENARIO=storm`.

The test injected a 60-second venue-capacity outage. The k6 output recorded:

- Outage started: 10:09:42 UTC
- Outage ended: 10:10:42 UTC
- Total HTTP requests: 61,156
- SLO-good requests: 48,447 (79.22%)
- HTTP request failure rate: 20.77%
- Client retries: 8,472
- Average throughput: 84.93 requests/second
- Average latency: 14.56 ms
- p95 latency: 25.35 ms
- p99 latency: 27.17 ms
- Maximum latency: 340.64 ms

The required SLO threshold was 99.85%. The observed 79.22% success rate breached this threshold.

### Dashboard observations

The Grafana availability-api · SLO dashboard showed:

- SLI over 30 days: 79.117%
- Error budget remaining: -13,822.2%
- One-hour burn rate: 139.2×
- Alerts firing: 2

The dashboard observations and k6 results represent different measurement windows and should be interpreted separately.

### Evidence

Screenshots are stored in `docs/evidence/picture/`.

1. [Storm-test dashboard before the test](../evidence/picture/Screenshot%202026-11-25%20115547-storm-test-before.png.png)
2. [Storm-test dashboard after the test](../evidence/picture/Screenshot%202026-11-25%20125623-storm-test-after.png.png)
3. [Additional post-test dashboard evidence](../evidence/picture/Screenshot%202026-11-25%20125710-storm-test-after.png.png)
4. [Alert evidence](../evidence/picture/Screenshot%202026-11-25%20125750-Alert.png)
5. [k6 command-line results](../evidence/picture/Screenshot%202026-11-25%20125836-storm-test-cli-results.png)

### Findings

The test demonstrates that a short dependency outage can cause substantial availability degradation and error-budget consumption.

The client retry count confirms retry activity during the test. Grafana's elevated burn rate and firing alerts demonstrate that the monitoring and alerting pipeline detected the impact.

Further verification is required to establish circuit-breaker behaviour on every replica and confirm that dependency traffic returned to normal after the outage.

### Follow-up actions

- Verify circuit-breaker transitions across all availability-api replicas.
- Confirm venue-capacity recovery after the injected outage.
- Investigate retry amplification and client backoff behaviour.
- Review the SLO breach and its impact on the error budget.
- Retain the screenshots and k6 output as assessment evidence.

## Afterwards

- Post-incident review for every page: how much error budget was used, and whether the
  error-budget policy now applies (README, "When the error budget runs out").
- Follow-up tickets: client retry behaviour (website, app); venue-capacity recovery under load.
