# Evidence index

This directory contains screenshots and test output from the Golden Path
availability API assessment.

## Screenshots

All screenshots are stored in `picture/`.

| File | Evidence |
|---|---|
| [01-before-run](picture/01-before-run.png) | Environment before the storm test |
| [02-slo-dashboard](picture/02-slo-dashboard.png) | Availability API SLO dashboard |
| [03-traffic-outcomes-breakers](picture/03-traffic-outcomes-breakers.png) | Request outcomes and circuit-breaker behaviour |
| [04-alertmanager-overview](picture/04-alertmanager-overview.png) | Alertmanager overview |
| [05-burn-rate-alert-firing](picture/05-burn-rate-alert-firing.png) | SLO burn-rate alert firing |
| [06-burn-rates-recovery](picture/06-burn-rates-recovery.png) | Burn rates after dependency recovery |
| [07-circuit-breaker-recovery](picture/07-circuit-breaker-recovery.png) | Circuit-breaker recovery |
| [08-golden-path-architecture](picture/08-golden-path-architecture.jpeg) | Golden Path architecture diagram |
| [09-observability-architecture](picture/09-observability-architecture.jpeg) | Observability architecture diagram |
| [10-prometheus-pod](picture/10-prometheus-pod.png) | Prometheus pod evidence |
| [11-hpa-scaling](picture/11-hpa-scaling.png) | Horizontal Pod Autoscaler evidence |
| [12-k6-storm-test](picture/12-k6-storm-test.png) | Load-test execution evidence |
## Latest storm test summary

The latest recorded storm-test output is in
[k6-storm-summary.txt](k6-storm-summary.txt).

The 25 November run did not meet the configured 99.85% SLO threshold.
It recorded 79.22% SLO-good requests and 8,472 client retries.
See the summary file for the complete measurements.

## Historical test results

The separate 24 November run and its measurements are documented in
[the retry-storm report](../retry-storm.md).

The two test runs are kept separate; their metrics should not be combined.

## 25 November 2026 — SLO alert evidence

Prometheus captured `AvailabilityApiSLOBurnRate5M` in the `firing` state
at 15:27:29 UTC. The alert became active at 15:26:51 UTC, with critical
severity for `availability-api`.

Captured burn rates:
- 5-minute: 259.99x (14.4x paging threshold)
- 1-hour: 336.11x (14.4x paging threshold)
- 30-minute: 336.11x
- 6-hour: 161.95x

Raw evidence:
- `prometheus-alerts-20261125T152429Z.json`
- `alertmanager-alerts-20261125T152429Z.json`
- `burn-rates-20261125T152429Z.json`

The alert fired during a controlled dependency-outage exercise. The
dependency was subsequently restored to normal and the API pods recovered.
The associated k6 job ended in Failed, so this capture demonstrates alert
behaviour, not a successful load-test run.
