# Runbook: venue-capacity outage and retry storm

| | |
|---|---|
| **Service** | availability-api (namespace `bookings`) |
| **Alerts** | **Pages:** `AvailabilityApiSLOBurnRate5M` (14.4× burn over 5m and 1h), `AvailabilityApiSLOBurnRate30M` (6× over 30m and 6h). **Context only (warning):** `AvailabilityDependencyCircuitOpen`, `AvailabilityDependencyTimeouts` |
| **Owner** | Bookings team; venue-capacity is owned by its own team |
| **Last reviewed** | 23 November 2026, by project author |

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

## Afterwards

- Post-incident review for every page: how much error budget was used, and whether the
  error-budget policy now applies (README, "When the error budget runs out").
- Follow-up tickets: client retry behaviour (website, app); venue-capacity recovery under load.
