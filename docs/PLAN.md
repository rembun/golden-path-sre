# Build plan

Built in phases so each one worked on its own and could be reviewed before the next.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Availability API, fake venue-capacity service, tests, Dockerfiles | ✅ Done |
| 2 | k3s (k3d or existing k3s), Postgres, deployment, kube-prometheus-stack, OTel Collector, Tempo, telemetry kit | ✅ Done |
| 3 | SLO as code: spec → recording rules, burn-rate alerts and a Grafana dashboard; cause alerts; promtool incident tests | ✅ Done |
| 4 | k6 load script (steady, peak, storm), runbook template, filled-in runbook, retry-storm exercise | ✅ Dashboard, Alertmanager overview and named firing-alert screenshots captured under `docs/evidence/picture/` |
| 5 | Pipeline template: reusable GitHub Actions workflow, policy gate, scan, ephemeral deploy | ✅ Done; green GitHub Actions run confirmed |
| 6 | README for product teams; DECISIONS.md | ✅ Documentation updated; authorship and AI assistance disclosed |
| 7 | Availability API autoscaling: HPA, CPU target, scale behavior and runtime verification | ✅ HPA scale-up/down observed; constrained-environment peak test documented; scheduled pre-scaling is an optional production optimisation, not an assessment requirement |

## Before submission (deadline: Wednesday 2 December 2026, 17:00)

- [ ] Repository root is the kit (README and `.github/` at the top level, not in a subfolder)
- [x] Configure repository runbook URLs, regenerate SLO artifacts, and commit
- [x] Push, and confirm the `ci` workflow runs green
- [ ] `make up` with k3d once (not verified: k3d is unavailable on the assessment host)
- [x] Deploy using `make up RUNTIME=k3s` on the existing k3s cluster
- [x] Configure and verify the availability API HPA on k3s (3–5 replicas, 70% CPU target)
- [x] Observe HPA scale-up from three to five replicas and scale-down to three during the constrained load test
- [x] Document the constrained-environment peak-test outcome and its limitations
- [x] Document scheduled pre-scaling as an optional production optimisation, subject to demand evidence and cost justification
- [ ] Validate peak capacity and node headroom on adequately sized infrastructure
- [x] Run `make load SCENARIO=storm`; save the k6 summary as `docs/evidence/k6-storm-summary.txt`
- [x] Save Grafana dashboard and Alertmanager overview screenshots to `docs/evidence/`
- [x] Replace generic author prompts in DECISIONS.md with review notes and verified evidence
- [x] Author and AI assistance disclosed in DECISIONS.md
- [ ] No secrets committed: `git grep -nE "password|token" -- ':!*.md'` shows only variable names

## SLO numbers

- **SLI:** good = not 5xx **and** completed in under 300 ms (one combined SLI).
- **SLO:** 99.85% over a rolling 30 days; error budget 0.15%.
- **Budget in requests:** ~183 M requests a month → roughly **274,000 bad requests**.
- **The peak:** at ~1,000 req/s a full outage uses the whole monthly budget in about **4.5 minutes**
  (the brief's "65 minutes" holds at average traffic only).

| Severity | Burn rate | Long window | Short window | Bad-request rate | Budget spent when it fires |
|----------|-----------|-------------|--------------|------------------|----------------------------|
| Page | 14.4× | 1 h | 5 min | 2.16% | 2% |
| Page | 6× | 6 h | 30 min | 0.9% | 5% |
| Ticket | 3× | 1 d | 2 h | 0.45% | 10% |
| Ticket | 1× | 3 d | 6 h | 0.15% | 10% |

Metric: `http_server_request_duration_seconds_bucket{job="bookings/availability-api", le="0.3", ...}`.

- [ ] Capture an Alertmanager screenshot showing `AvailabilityApiSLOBurnRate5M` firing
