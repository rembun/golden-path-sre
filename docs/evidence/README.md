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
| [Prometheus pod](picture/Screenshot%202026-11-24%20224053-PromethuesPod.png) | Prometheus pod evidence |
| [HPA scale](picture/Screenshot%202026-11-24%20224622-HPA-Scale.png) | Horizontal Pod Autoscaler evidence |
| [k6 storm CLI](picture/k6-storm-cli.png) | Load-test execution evidence |
| [Diagram 1](picture/Diagram%201%20Image%202026-11-24%20at%2021.13.46.jpeg) | Supporting architecture diagram |
| [Diagram 2](picture/Diagram%202%20Image%202026-11-24%20at%2021.14.32.jpeg) | Supporting architecture diagram |

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
