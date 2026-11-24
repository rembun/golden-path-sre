# DECISIONS.md

The decisions behind this golden path: what we chose, why, and what we rejected. Sections follow
the brief.

> Status: all build phases complete; see [docs/PLAN.md](docs/PLAN.md). Items marked
> **Author:** Rembuluwani Nemakhavhani. AI assistance: OpenAI ChatGPT. The author reviewed, tested and takes responsibility for the submitted work.

---

## 1. Key decisions

### D1. Platform: k3s, run through k3d

**Chosen:** k3s as the Kubernetes distribution, created and destroyed with **k3d** (k3s in Docker).

**Why:** we want a real Kubernetes cluster (so the kit uses the same objects a team would use in
production: Deployments, ServiceMonitors, PrometheusRules), but it must run on a reviewer's laptop
with one command. k3d gives us exactly k3s, inside Docker, on Linux, macOS or Windows, and the
brief lists k3d explicitly.

**Rejected:**
- *k3s installed directly on a host.* Same distribution, but it needs a Linux machine and root, and
  it modifies the host (systemd units, iptables). A reviewer on a Mac couldn't run it.
- *docker compose.* Simpler, but kube-prometheus-stack, ServiceMonitors and PrometheusRules are
  the things a product team would actually adopt; compose would mean demonstrating a different
  stack from the one we recommend.
- *kind.* Equally valid. k3d starts faster, ships a lighter footprint and includes a load balancer
  and local registry support out of the box.

**Also supported:** an existing k3s install (`make up RUNTIME=k3s`). Same manifests and charts;
only cluster access, image import (`k3s ctr images import`) and the ingress port differ. This
shows the kit isn't tied to the local tooling, and `make down` in that mode removes only what
the repo created.

### D2. Observability: kube-prometheus-stack + OpenTelemetry Collector + Tempo

**Chosen:** kube-prometheus-stack (Prometheus Operator, Prometheus, Alertmanager, Grafana) for
metrics, alerting and dashboards; an OpenTelemetry Collector as the single entry point for
telemetry; Grafana Tempo for traces.

**Why:** kube-prometheus-stack gives teams the operator CRDs (`ServiceMonitor`, `PrometheusRule`),
which is what makes "add a service by writing one spec file" possible: the kit generates CRDs and
the operator picks them up. Services emit OpenTelemetry only, so they are not tied to Prometheus;
the Collector is where cost controls (sampling, attribute dropping) live, in one place.

**Rejected:** Jaeger for traces (Tempo integrates directly with Grafana, so one UI for metrics and
traces); services exposing Prometheus `/metrics` directly (would bypass the Collector, so cost
controls would have to be implemented in every service).

### D3. Service language: Python + FastAPI, plain SQL

**Why:** small, readable, and well supported by OpenTelemetry auto-instrumentation. The brief scores
the platform work, not the framework, so we chose what is quickest to read in a review.
Plain SQL via psycopg (no ORM): there are four queries, and they are easier to review as SQL.

### D4. Holds expire by timestamp, not by a background job

A hold is active while `expires_at > now()`. Availability excludes slots with an active hold.
Nothing has to run to "release" a hold, so there is no job that can fall behind or fail.
Expired rows stay in the table; purging them is a known gap (see section 7).

### D5. Double holds prevented with a row lock

`POST /holds` runs `SELECT ... FOR UPDATE` on the slot, checks for an active hold, then inserts,
in one transaction. Concurrent requests for the same slot serialise on the row lock, so exactly one
wins and the rest get `409`. Covered by `test_concurrent_holds_only_one_wins`.

**Rejected:** an exclusion constraint on active holds. Postgres can't index on `now()`, so a
constraint can't express "active" directly; the row lock is simpler and easy to explain.

### D6. Calling the capacity dependency: short timeout, no retries, circuit breaker, fail closed

- **Timeout 150 ms**, half the 300 ms SLO threshold, leaving room for the database query.
- **No retries inside a request.** Retries multiply load on a dependency that is already
  struggling; that is how retry storms start and how they get worse.
- **Circuit breaker** opens after 5 consecutive failures and allows a trial request after
  its reset interval. The code default is 5 s; the deployed Kubernetes configuration overrides
  this with `BREAKER_RESET_S=180` (3 minutes). While open, we answer `503` without calling
  the dependency. This is the built-in mitigation for the retry-storm scenario.
- **Fail closed** (`503` + `Retry-After`) rather than showing slots we can't confirm. This is a
  business decision, recorded as an assumption and a question for the product team (section 4).

### D7. Readiness does not depend on the capacity service

`/readyz` checks the database only. If it also checked the capacity service, a capacity outage
would mark every pod unready, remove them all from the Service, and turn a degraded API into a total
outage. `/healthz` (liveness) checks nothing external, so dependency problems never cause restarts.

### D8. One Uvicorn worker per container

Scale with replicas, not in-process workers. Resource limits, the per-process circuit breaker and
per-pod metrics all stay simple to reason about.

### D9. Credentials only from the environment

`DATABASE_URL` and `CAPACITY_TOKEN` have no defaults in code and are required at startup.
In the cluster they come from Kubernetes Secrets; locally from a git-ignored `.env`. Nothing real
is committed.

### D10. Telemetry is a kit package, and the service onboards with one line

`kit/telemetry/python` (`golden_telemetry`) wraps the OpenTelemetry SDK with the platform's
rules. A team adds the package and calls `golden_telemetry.instrument(app)`. The rules are
enforced by the kit's own tests (0.3 s bucket present, only allowed labels, probes excluded),
so the standard can't silently drift.

**Rejected:** OpenTelemetry zero-code auto-instrumentation (`opentelemetry-instrument`). It's
convenient, but it can't apply our views (custom buckets, label allow-list), and those views
are the cost controls.

### D11. Cardinality is limited in two layers

1. In the SDK: metric views keep only an allow-list of labels per HTTP metric.
2. In the collector: a `transform` processor deletes known-dangerous attributes from every
   metric, so a service that skips the kit still can't create unbounded series.

The first layer is cheap and precise; the second is the safety net that the platform team
controls. See `kit/telemetry/README.md`.

### D12. The collector config is written out in full

The collector chart merges its own default config (Jaeger, Zipkin and debug exporters) with
ours unless we use `alternateConfig`. We write the whole config, so what runs is exactly what
is in the repo, and it's validated with the matching `otelcol-contrib validate`.

### D13. No CPU limits, memory limits only

CPU limits cause throttling, and throttling shows up as latency, which is precisely what our
SLO measures. Pods get CPU *requests* (for scheduling) and memory *limits* (memory isn't
compressible, so an unbounded leak should be killed and restarted).

### D14. Safe-by-default Deployment

`maxUnavailable: 0` rollouts, a PodDisruptionBudget of 2, topology spread across nodes, a
5-second `preStop` pause so the pod leaves the Service before it stops accepting
connections, non-root, read-only root filesystem, all capabilities dropped, and no service
account token mounted. These are the defaults another team copies.

### D15. Pinned versions and deliberate upgrade review

k3s, Helm charts and Python packages are pinned. The normal upgrade preference is to wait
at least 14 days after a release, allowing time for early regressions or security concerns
to surface. This is a review policy, not an absolute rule: the kube-prometheus-stack upgrade
to 93.9.0 was an explicit exception, reviewed against the chart changes and validated with
a Helm server-side dry run before deployment.

Versions: k3s v1.37.2+k3s1, kube-prometheus-stack 93.9.0 (Prometheus Operator
v0.95.2, Prometheus 3.16.0, Grafana 13.3.1, Alertmanager 0.35.0), Tempo chart 3.2.1
(application 3.1.1), OpenTelemetry Collector chart 0.178.2 (collector 0.163.2),
k6 1.8.2, Trivy 0.76.2, kind 0.33.0, kubeconform 0.8.0. Binaries the pipeline
downloads are checked against their published SHA-256 checksums.

Note: the Tempo single-binary chart was deprecated in `grafana/helm-charts` and is now
maintained in `grafana-community/helm-charts`; we use the maintained one.

### D16. Local secrets are generated, never committed

`scripts/bootstrap-secrets.sh` generates random passwords and tokens once into `.env.local`
(git-ignored) and creates Kubernetes Secrets from it. In Azure these would come from Key
Vault via the Secrets Store CSI driver or workload identity.

### D17. k3s control-plane scrape targets are disabled

k3s runs etcd, the scheduler and the controller manager inside one binary rather than as
pods, so kube-prometheus-stack's default scrape targets for them don't exist. Leaving them on
gives permanent "target down" alerts, which teach people to ignore alerts.

### D18. SLO as code: a small generator, committed output

`kit/slo/generate.py` turns one spec file per service into a `PrometheusRule`: four recording
rules per window (requests, good requests, error ratio, burn rate) and one multi-window alert per
burn-rate pair. The generated file is committed, so every rule change is reviewable in a pull
request, and the pipeline fails if it's out of date with the spec.

- The SLI is **combined**: good = not 5xx **and** under 300 ms, from one histogram. Failures and
  slow responses both spend the budget, because customers experience both.
- Recording rules for one window stay in one group, so they evaluate in order. Windows of a day or
  more go in a second group evaluated every 5 minutes: they're the expensive queries and change slowly.
- Rule names are validated at generation time (Prometheus rejects a whole group for one bad name).

**Rejected:** Sloth and Pyrra. Both are good, but they add a tool or a running controller for
something a short script does, and our spec can grow fields we need (like `runbook_url`) without
waiting on an upstream project. A spec translates directly into Sloth's format if the platform
outgrows the generator.

### D19. Burn-rate thresholds from the Google SRE workbook

14.4× over 5m and 1h, and 6× over 30m and 6h, page (`critical`); 3× over 2h and 24h, and 1× over
6h and 3d, raise tickets (`warning`). Both windows must exceed the threshold: the long one proves it
matters, the short one makes the alert clear quickly after recovery. The numbers are unchanged
from the workbook because they're well understood; the reasoning is what matters for this service:
at the Thursday peak the monthly budget lasts about 4.5 minutes, so the 5-minute window is essential.

### D20. Page on symptoms only; cause alerts are warnings

Only SLO burn alerts page. `AvailabilityDependencyCircuitOpen` and `AvailabilityDependencyTimeouts`
are `warning`: they explain *why* the SLO is burning, in the same view, but a dependency problem
that customers don't feel (for example at 3 a.m. with almost no traffic) shouldn't wake anyone, and
one incident shouldn't produce two pages. Both rule files live in the team's `bookings` namespace.

### D21. Every alert links to a runbook, and the alerts are tested

The SLO spec has a `runbook_url` field that the generator adds to every alert. A `promtool` unit
test replays a retry storm through the generated rules and asserts the outcome: no page before the
outage, the fast-burn page during it, and a 5-minute burn rate of exactly 200× when 30% of requests
fail. Alerts are code, and untested alerting code fails silently, at the worst moment.

### D22. Prometheus retention covers the SLO window

Retention was raised from 7 to 30 days, because a 30-day SLO computed over 7 days of data is
silently a 7-day SLO. Prometheus is configured with `retention: 30d` and `retentionSize: 4GB`;
the 4GB cap can be reached before 30 days and therefore remains a limitation (see section 7).
The Prometheus PVC requests 5Gi. Its persistent volume uses the `Retain` reclaim policy so
deleting the PVC does not automatically delete the underlying volume. This is not a backup.
In Azure, Managed Prometheus keeps 18 months by default, so this is a local-cluster concern.

### D23. Load testing with k6, in the cluster, measuring the same SLI

The k6 script has three scenarios: **steady** (~70 req/s), **peak** (ramp to ~1,000 req/s and
hold 25 minutes) and **storm** (steady traffic; the script itself switches venue-capacity into an
outage for 60 s while clients retry twice immediately, ignoring `Retry-After`). It runs as a
Kubernetes Job, so load reaches the Service directly and nobody needs k6 installed. Its pass/fail
threshold is the SLI itself (not 5xx and under 300 ms, ≥ 99.85%), so the load test and the alerts
agree on what "good" means. `RATE_SCALE` scales every rate down for small machines.

**Rejected:** Locust (needs a Python runtime and more resources per request generated); running
k6 on the laptop through the ingress (measures laptop networking, not the service).

### D24. Golden-path part built: the pipeline template (GitHub Actions)

A reusable workflow (`on: workflow_call`) that teams reference rather than copy, so improvements
reach every team at once. Stages: **test** (real PostgreSQL; any skipped test fails the run, so a
missing database can't look like a pass) → **policy gate** → **build and scan** (Trivy; fails on
fixable critical vulnerabilities) → **deploy** to an ephemeral kind cluster with a smoke test that
also checks fail-closed behaviour (`503` + `Retry-After` during a dependency outage). Deploy needs
all three earlier stages.

- **The policy gate** (`kit/pipeline/policy_check.py`) enforces the golden-path defaults: pinned
  image tags, resource requests and memory limits, non-root without privilege escalation, probes,
  a PodDisruptionBudget for replicated Deployments, and an SLO spec for every service behind an
  Ingress. On its first run against this repo it found three real gaps (no resources on the seed
  Job, no liveness probe on the fake service), which were then fixed.
- **Supply chain:** Trivy and promtool run from their official container images rather than
  third-party actions; downloaded binaries are checksum-verified; workflow permissions are
  read-only by default.

**Why this option over the other two:** it is the part every team touches on every change, it
can be demonstrated end to end without a cloud account, and it's where the other golden-path
parts get enforced (SLO spec present and current, telemetry and deployment defaults).
**Rejected:** Azure Pipelines (preferred in the brief, but it can't run from a public GitHub repo
without an Azure DevOps organisation, so reviewers couldn't see it run; the structure maps
directly onto Azure Pipelines templates).

### D25. Hostnames under `.test`

The API and Grafana are served as `api.sre.test` and `grafana.sre.test`. `.test` is reserved
(RFC 2606) and never resolves on the internet, so a typo can't send traffic anywhere real. It
needs one `/etc/hosts` line, documented in the README.

### D26. Images built with Docker, imported into k3s's containerd

With `RUNTIME=k3s`, images are built with Docker and copied into k3s's containerd with
`k3s ctr images import`: k3s runs images but can't build them. With k3d, `k3d image import` does
the same job.

### D27. The repository root is the kit

The README, `.github/workflows` and everything else sit at the repository root. GitHub Actions only
reads workflows from the root, and the README is the first thing a team (or reviewer) sees.

### D28. Dashboard generated from the SLO spec

`kit/slo/generate_dashboard.py` builds a Grafana dashboard from the same spec as the alerts,
shipped as a ConfigMap in the team's namespace (`grafana_dashboard: "1"`), so adding a service
still means writing one file. Standard panels (budget left, SLI, burn rate against the paging
thresholds, alerts firing, traffic by status, p50/p99 against the latency threshold) use the
generated recording rules, so the dashboard and the alerts can't disagree; services add their own
panels in the spec (here, capacity call outcomes and breaker state per pod). The dashboard is
read-only in Grafana: changes go through the spec and a pull request.

**Verified against live data:** the service, collector and a real Prometheus (with the generated
rules) were run through normal traffic and then an outage with aggressive retries. Every panel
query returned data except the two 30-day stats, which appear within 5 minutes because their rules
are evaluated every 5 minutes. Both paging alerts fired. Every query also passes `promtool` in CI.

**Rejected:** clicking a dashboard together in Grafana and exporting JSON (drifts from the SLO
and from git, and every team's looks different); Grafonnet/Jsonnet (another language to learn for
a dashboard that's 90% standard).

### D29. The circuit-breaker alert watches refused requests, not breaker state

This alert took three versions, each driven by evidence:

1. **`capacity_circuit_state == 2` for 30 s never fired during a real outage.** A live run showed
   the breaker cycling open → half-open (one trial) → open every few seconds, so the gauge was
   never 2 for 30 s straight.
2. **"Not closed at any point in the last minute" fired forever on idle pods.** The storm-run
   dashboard showed a pod sitting in half-open for 15 minutes with no traffic: after an outage, a
   breaker stays half-open until the next request arrives. That's harmless, but the rule would
   have kept a warning open indefinitely.
3. **Now: the breaker has been refusing requests for over a minute**
   (`rate(capacity_requests_total{outcome="circuit_open"}[1m]) > 0` for 2 minutes). It alerts on
   what the breaker *does* to customers, which is what matters. The state misleads both ways.

A `promtool` test covers all three situations: a 4-minute outage fires; a 30-second blip and an
idle half-open pod don't. Version 2, given the same data, fires on the idle pod too.

### D30. The `instance` label is the pod name

The OpenTelemetry SDK gives every process a random `service.instance.id`, so dashboards showed
UUIDs, and a restarted pod appeared as a new, unexplained series. The Deployment now sets it to the
pod name through the downward API (`service.instance.id=$(POD_NAME)`), so a panel or alert names
the pod you can `kubectl logs` directly. It's in the telemetry standard for every team. Cardinality
is unchanged: still one series set per pod, expiring 5 minutes after the pod goes.

### D31. The 150 ms capacity timeout is a deadline for the whole call

httpx applies its timeout to each phase separately (waiting for a pooled connection, connecting,
writing, reading), so "150 ms" could add up to several times that, and a dependency that responds
slowly but steadily could hold a request much longer. The call is now wrapped in
`asyncio.timeout(...)`, a single deadline for the whole call. A test with a dependency that takes a
second shows the old code waiting the full second and the new code giving up at 150 ms.

### D32. Availability API autoscaling with a conservative HPA

The availability API now has a Kubernetes HorizontalPodAutoscaler using CPU utilization
against the container's 250m CPU request. The initial policy keeps at least 3 replicas and
allows scaling to 5 at a 70% utilization target. Scale-up can add up to 2 pods per minute;
scale-down is deliberately slower, with a 5-minute stabilization window and at most one
pod removed per minute.

Metrics Server was verified, and the HPA was confirmed active on k3s: `ScalingActive=True`,
`AbleToScale=True`, and `ScalingLimited=False`. At verification time, the Deployment had
3 available replicas and CPU utilization was approximately 1%.

The maximum of 5 is a conservative initial ceiling for the single-node assessment cluster,
not a demonstrated capacity guarantee. The constrained peak-profile test observed HPA
scale-up and scale-down, but the application did not meet its SLO and peak capacity was
not validated. CPU-based scaling is reactive and will not necessarily detect latency or
dependency saturation when CPU remains low. Scheduled pre-scaling ahead of the known
Thursday 09:00 peak is added in D33.

### D33. Scheduled pre-scaling before the Thursday 09:00 launch

Bookings open at a known time, and traffic rises about 14× within a minute. A CPU-based HPA
reacts only after CPU (and latency) has risen, which the storm run showed: two pods were added
*during* the incident. So two CronJobs raise the HPA's floor at 08:45 every Thursday (UK time)
and lower it at 10:00. The HPA stays in charge, and can still scale above the floor.

- **The floor, not the replica count, is changed.** Setting `replicas` directly would fight the
  HPA; raising `minReplicas` cooperates with it.
- **Every Thursday, not just launch Thursdays.** Cron can't express "first Thursday of the month"
  (day-of-month and day-of-week are OR'd, so `1-7 * 4` would also fire every day from the 1st to
  the 7th), and an extra hour of pods once a week is cheap insurance.
- **Least privilege:** the CronJobs' service account can `get` and `patch` this one HPA, nothing
  else. The script uses the same Python base image as the service, so no new image needs vetting.
- **Lab vs production values.** In the lab, the floor rises to the HPA ceiling of 5 (D32), which the
  single-node VM can hold. For production: floor = ceil(peak req/s ÷ per-pod req/s at p99 < 300 ms ×
  1.3 headroom), measured on representative infrastructure, with the HPA maximum above it and the
  database connection limit checked (pods × `DB_POOL_SIZE` < `max_connections`).
- **Tested** against a fake Kubernetes API: the exact merge-patch request is sent with the pod's
  token, and an RBAC denial exits non-zero, so the CronJob records a failure (kube-prometheus-stack's
  default `KubeJobFailed` alert then fires). Try it any time with `make prescale-up` /
  `make prescale-down`.

### D34. The reusable pipeline brings its own kit

A team's repository doesn't contain `kit/`, so the reusable workflow checks out the kit into
`.golden-path` (inputs `kit-repository`, `kit-ref`) alongside the team's code, and runs the policy
gate, SLO generators and telemetry package from there. Teams pin a released kit version
(`@v1` for the workflow, `kit-ref: v1` for the kit) and get fixes without copying anything; a
breaking change becomes `v2`. This repository calls the workflow with `kit-ref: ${{ github.sha }}`,
so a pull request that changes the kit is tested with that change. A simulated team repository
(own manifests and SLO spec, no `kit/`) passes the policy stage through `.golden-path`, and the
gate still rejects it when its SLO spec is removed.

### D35. The policy gate understands autoscaling

Once the HPA owns the replica count, the Deployment rightly has no `replicas` field, and the
gate's PodDisruptionBudget rule read it as 1 replica and **silently skipped the check**. It now
counts the HPA's `minReplicas` (D2), rejects a Deployment that sets `replicas` while an HPA
manages it (D3: every `kubectl apply` would reset the HPA's scaling), and checks CronJob
containers like any other workload.

---

## 2. The service build: what we kept small, what we didn't build

**Kept small:** two endpoints, two tables, four queries. Venue ids are free-form strings; slots
are 30-minute blocks from 09:00 to 21:00 UTC, seeded by an idempotent job.

**Deliberately not built:**
- Bookings (turning a hold into a booking). Out of scope for the ticket.
- Authentication on our API. In production this sits behind the edge and an identity layer.
- Caching capacity responses. It would improve latency and resilience, but it changes what
  customers see (possibly stale capacity), which needs a product decision first.
- An ORM, migrations framework, or API versioning.

**Fake venue-capacity service:** modes `normal`, `slow`, `error`, `unauthorized`, `outage`, switched
at runtime via `PUT /admin/mode`. It also models overload: past `MAX_INFLIGHT` concurrent requests
it slows down and sheds load with `503`, which is what makes a retry storm "struggle to recover".

---

## 3. Where AI helped, and what changed

I used OpenAI ChatGPT as an AI-assisted engineering and review tool during this project. I made the engineering decisions, reviewed the suggestions, validated changes through tests and deployment, and take responsibility for the final implementation.

My main review decisions were to keep the SLO definition as code and generate the
Prometheus rules and dashboard from it; configure Prometheus for 30-day retention with a
4GB size cap (so retention may be shorter under heavy ingestion); use `.test` hostnames
only for local/test ingress; and use OTLP through the Collector as the application
tracing path. Tempo's Jaeger receivers remain enabled but unused, and are a cleanup item.

| Area | What I asked for | What it produced | What I changed, rejected or caught |
|------|------------------|------------------|------------------------------------|
| Service (phase 1) | A small availability API with holds, a fake dependency with a chaos switch, tests against real PostgreSQL | FastAPI service, circuit breaker, row-lock holds, 22 tests | Verified the API test suite against a throwaway PostgreSQL instance: 22 tests pass. Reviewed database-backed holds and dependency-failure handling |
| Telemetry kit (phase 2) | OpenTelemetry with cost controls | `golden_telemetry` package, collector config, telemetry standard | It noticed OpenTelemetry's default HTTP buckets don't include 0.3 s, so the SLO couldn't be computed exactly, and added explicit buckets with a test. The telemetry test suite passes all 5 tests |
| Telemetry kit | — | First version registered the breaker gauge once per app instance | Its own test failed (duplicate instruments ignored); fixed to register once per process |
| Platform | Pinned Helm charts | Reviewed chart versions and upgrade changes; normally prefer releases at least 14 days old | Found the Tempo chart had moved to `grafana-community`; used the maintained one. The kube-prometheus-stack 93.9.0 upgrade was a documented exception, checked with a Helm server-side dry run before deployment |
| Load, pipeline, runbook (this review) | Fill the gaps against the brief | k6 script, reusable pipeline, policy gate, promtool storm test, runbook template and filled runbook, these docs | The policy gate caught missing resource settings on the seed Job and an unsuitable fake liveness probe. The k6 storm summary and Grafana dashboard/Alertmanager overview screenshots are committed under `docs/evidence/`; the named firing Alertmanager alert screenshot is included in `docs/evidence/grafana-picture/` |
| Runbook | — | — | Caught during review: the committed runbook was the shell script that should have generated it, cut off after 45 lines. Rewritten |
| Storm-run review | Review my storm-run dashboard screenshots and fix what's wrong | Outcome-based breaker alert, pod-name instance labels, whole-call deadline, dashboard fixes (burn rate 0 instead of blank with no traffic, dashed threshold lines, red 5xx) | The previous AI-written alert fix (D29 version 2) was wrong: it fired forever on an idle pod. Spotted on my own dashboard (a pod in half-open for 15 minutes), confirmed with promtool, replaced |
| Dashboard | A dashboard for the service, generated from the SLO spec | Generator, tests, live verification against a real Prometheus | The live run exposed a bug in an earlier AI-written cause alert (D29): it could never fire during an outage. Fixed and covered by a promtool test |

**Where AI was least reliable:** anything depending on current versions or third-party chart
layouts (verified against the actual chart sources), and numbers in the cost estimate (treat them as
order-of-magnitude, verified against the Azure pricing calculator before relying on them).

---

## 4. Questions for the product team, and assumptions

| # | Question | Assumption until answered |
|---|----------|---------------------------|
| Q1 | When the error budget is exhausted, what happens? (Freeze features? Reliability work only?) | Feature releases pause until the 30-day budget recovers; reliability fixes and security patches continue. Full policy in the README, "When the error budget runs out". |
| Q2 | If live capacity can't be confirmed, should customers see possibly-stale slots, or none? | Fail closed: show none (`503`). Overselling is worse than a short outage. |
| Q3 | Which time zone defines "a day" for a venue? | UTC. A real system needs each venue's time zone. |
| Q4 | Do `409` conflicts (slot already held) count against the SLO? | No. They are correct behaviour. Only `5xx` and slow responses are "bad". |
| Q5 | Can a customer hold more than one slot at once? | Yes; no per-customer limit in this version. |
| Q6 | Do the website and app honour `Retry-After`, and how do they retry? | They retry immediately up to twice (as in the storm scenario). Backoff with jitter is the real fix (runbook, "Fix"). |
| Q7 | Who owns venue-capacity, and what's its SLO? | Another team; no published SLO. Our SLO can't be better than our hard dependencies allow. |
| Q8 | Is ~1,000 req/s for ~25 minutes the right peak forecast, and is it always Thursday 09:00? | Yes, monthly. Pre-scaling should be scheduled for it (section 8). |
| Q9 | Which time zone is "Thursday 09:00" in? | UK time (`Europe/London`), used by the pre-scaling CronJobs. |

---

## 5. Monthly cost estimate

Approximate Azure list prices (pay-as-you-go, a European region), rounded. They're for comparing
options and spotting the big items; check them in the Azure pricing calculator before quoting.

**Assumptions:** ~70 req/s average plus one 25-minute peak at ~1,000 req/s → about **183 million
requests a month**. Responses ~1–2 KB. The service runs on a shared AKS cluster, so it carries a
share of compute rather than a whole cluster. Telemetry uses the kit's defaults (10% trace
sampling, WARNING logs, label allow-list).

| Item | Choice | ≈ per month | Notes |
|------|--------|------------:|-------|
| Compute | Share of shared AKS: 3 pods × 0.25 vCPU baseline, scaling for the peak | $80 | A dedicated small cluster would be ~$300+ |
| Database | PostgreSQL Flexible Server, General Purpose 2 vCores, zone-redundant HA, 64 GB | $270 | HA roughly doubles the compute cost; worth it for the most-used customer journey |
| Edge | Front Door **Premium** (managed WAF rules, Private Link to the origin) | $330 base + ~$180 requests + ~$25 data | **Largest single item.** Standard is ~$35 base but only custom WAF rules and no Private Link |
| Secrets, registry | Key Vault, Container Registry Basic | $10 | |
| **Metrics** | Azure Monitor managed Prometheus: ~600 series at 15 s ≈ 100 M samples ingested | $2 | Tiny because of the label rules. Adding a `venue_id` label with 50 venues would multiply this ~50×; with thousands of real venues, by thousands |
| Metric queries | Recording rules, mostly the 30-day window every 5 minutes | ~$10 | The 30-day rule is the most expensive query; evaluating it less often is the lever |
| **Traces** | Application Insights: 10% of 183 M requests × ~5 spans × ~1 KB ≈ 90 GB | ~$210 | **Second-largest item.** At 100% sampling it would be ~$2,100. Dropping FastAPI's internal send/receive spans (2 of the 5) would cut ~40% |
| Logs | WARNING level, no access logs: well under 1 GB | ~$3 | Per-request INFO logs would be ~100+ GB |
| Grafana | Azure Managed Grafana Standard | (shared) | ~$60 for the platform, not per service |
| **Total** | | **≈ $1,100** (range $900–1,400) | |

**What this tells us:** the edge and the traces cost more than running the service itself. The
telemetry defaults are doing their job: metrics are nearly free, and the trace bill is a tenth of
what it would be without sampling.

---

## 6. The two golden-path parts not built

### Edge and security (Azure Front Door)

A Terraform module per service, using Front Door **Premium**:

- **Origin reachable only through the edge:** Front Door connects to the AKS internal load
  balancer over **Private Link**, so the origin has no public IP at all. Where Private Link isn't
  possible, the fallback is an ingress allow-list for the `AzureFrontDoor.Backend` service tag plus
  a check that the `X-Azure-FDID` header matches our Front Door profile id (the service tag alone
  would admit any Front Door customer).
- **TLS and certificates:** a custom domain with an **AFD-managed certificate**, so issuance and
  renewal are automatic. TLS 1.2 minimum; HTTP redirects to HTTPS.
- **WAF policy** in prevention mode: Microsoft's default managed rule set and the bot manager rule
  set, plus custom **rate limits** per client IP: generous on
  `GET /venues/*/availability` (customers refresh during the launch), strict on `POST .../holds`
  (each hold locks a slot for 10 minutes, so it's the abuse target). Rate limits are also what
  turns a client retry storm into fast `429`s at the edge instead of load on us.
- **Health probes** on `/readyz`, so Front Door stops sending traffic to an origin that can't serve.
- **Alert before a certificate expires:** managed certificates renew on their own, but renewals can
  fail (for example if DNS validation breaks), so a blackbox-exporter probe alerts when
  `probe_ssl_earliest_cert_expiry` is less than 21 days away. That works for any certificate,
  managed or not.
- **Locally:** Traefik (already in k3s) with a self-signed or mkcert certificate and Traefik's
  rate-limit middleware shows the same shape.

### Azure IaC module (Terraform)

A module `modules/golden-service` that creates everything a service needs outside Kubernetes:
resource group, PostgreSQL Flexible Server with private networking (HA optional by environment),
Key Vault, a user-assigned managed identity with a federated credential for **AKS workload
identity** (so pods get secrets without any stored credentials), an `AcrPull` role assignment, and
diagnostic settings sending logs to the shared workspace.

- **Cost tags enforced twice.** In the module, a `tags` variable whose `validation` block requires
  `owner`, `service`, `environment` and `cost_centre` (non-empty), merged onto every resource, so
  `terraform plan` fails without them. In Azure, an **Azure Policy** assignment with the built-in
  "Require a tag on resources" in `deny` mode, so resources created outside the module are refused
  too. The module makes it easy; the policy makes it unavoidable.
- **Budget alert:** an `azurerm_consumption_budget_resource_group` per service, notifying the
  `owner` at 80% and 100% of the expected monthly spend.
- **Checks in the pipeline:** `terraform fmt -check`, `terraform validate`, tflint and checkov in
  the policy-gate stage, so infrastructure gets the same gate as Kubernetes manifests.

---

## 7. Known gaps and shortcuts

**Verification status**
- The service, kit and alert logic are tested: service tests against real PostgreSQL, telemetry kit
  rules, SLO generator, policy gate, and a promtool test that replays the retry storm.
- The pipeline workflows pass `actionlint`. All third-party GitHub Actions are pinned to full commit SHAs, with release-version comments. GitHub Actions passed for commit `0f6c9eb`: https://github.com/rembun/golden-path-sre/actions/runs/45474780419.
- `make up RUNTIME=k3s` was validated on the existing k3s cluster. The default k3d path remains unverified: k3d is not installed on the assessment host. The configuration and dry-run were reviewed; no separate cluster was created.

**Gaps**
- **Autoscaling and scheduled pre-scaling are implemented (D32, D33); production sizing is not.**
  The availability API HPA targets 70% CPU utilisation, with a minimum of three and maximum of
  five replicas, and the Thursday CronJobs raise the floor to five.
  Metrics Server was verified, and the HPA scaled the deployment from three to five replicas
  during load, then back to three as CPU utilisation fell. This demonstrates the HPA scaling
  path, not production capacity.
- **Constrained-environment peak test did not meet the SLO.** A peak-profile run using
  `RATE_SCALE=0.1` and a two-minute peak generated 22,095 requests at approximately 52.6 req/s
  average. The good-request ratio was 11.55%; 88.19% of requests resulted in HTTP failures.
  p95 latency was 420.72 ms and p99 was 3.1 s. The test did not achieve the intended 100 req/s
  profile or validate peak capacity.
- **Test-environment limitation.** The single-node assessment VM has approximately 4.1 GiB RAM.
  Significant swap activity, Kubernetes API-server timeouts and probe failures across multiple
  observability components were observed during the run. These findings indicate cluster-wide
  resource contention; they do not establish a production service capacity limit or prove OOM.
- **Follow-up:** repeat representative peak testing on adequately sized multi-node infrastructure
  before making production capacity commitments, then set the pre-scaling floor and HPA ceiling
  from it (D33).
- **`retentionSize: 4GB` can silently shorten the 30-day SLO window** if data grows past it. Fine
  at local volumes; in Azure, Managed Prometheus retention removes the issue.
- **A p99 latency spike to ~2.5 s at the start of a storm run**, far beyond the capacity timeout.
  Not yet explained. Candidates: CPU contention on a single-node lab cluster also running
  Prometheus and Grafana; the per-phase timeout gap fixed in D31; or a pod restart (one pod was
  replaced during that run). Next step: find the slow traces for that minute in Tempo, and check
  `kubectl -n bookings get pods` restarts and `kubectl describe pod` for `OOMKilled`.
- **Tempo still has the chart's Jaeger receivers enabled.** Only OTLP is used.
  Both Jaeger and OTLP receivers were present in the initial committed Tempo values; Git history records no later re-enable or rationale. The application uses OTLP through the Collector, so Jaeger is currently unused and remains a cleanup item.
- **Grafana ingress is managed by Helm** using `grafana.sre.test` in the kube-prometheus-stack
  values. The separate manual ingress manifest has been removed to maintain one source of truth.
- **Alert `runbook_url`s** point to the repository's dependency-failure runbook.
- **The pipeline deploys to an ephemeral cluster only.** A real environment needs an approval-gated
  job with OIDC to AKS.
- FastAPI's instrumentation creates internal send/receive spans that add trace volume without
  insight (section 5).
- Carried over: expired holds are never purged; breaker state is per pod; no NetworkPolicies; the
  fake's admin endpoint is unauthenticated; single local Postgres replica with no exporter.

**Cut to stay within 3 days:** production capacity sizing, tail-based trace sampling, a
NetworkPolicy set, and a real-environment deploy. The CPU-based HPA was implemented and
verified; peak capacity was not validated in the constrained assessment environment.

---

## 8. One more sprint

1. **Size for the peak:** repeat the peak load test on adequately sized infrastructure to
   establish a defensible per-pod capacity, then set the pre-scaling floor and HPA ceiling (D33)
   from it.
2. **Error-budget policy in the pipeline:** the deploy stage queries the remaining budget and blocks
   feature deploys when it's gone (with an override label for fixes).
3. **Tail-based sampling** in the collector: keep every error and every slow request, sample the
   rest at a lower rate. Cheaper and more useful than 10% head sampling.
4. **Build the edge module** (section 6) and the real-environment deploy with approvals and OIDC.
5. Add automated dependency-update PRs for pinned GitHub Actions; add NetworkPolicies.
