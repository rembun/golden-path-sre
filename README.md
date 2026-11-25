# Golden path for services

A kit that gives your service production-ready observability, SLOs, alerting and safe delivery
without working it all out yourselves. It's shown here onboarding the Bookings team's
**availability API** (`GET /venues/{venueId}/availability`, `POST /venues/{venueId}/holds`),
which needs to be ready for the festive-season launch traffic.

- **Why things are the way they are:** [DECISIONS.md](DECISIONS.md)
- **The retry-storm exercise** (the alert that fires, and the built-in mitigation): [docs/retry-storm.md](docs/retry-storm.md)

## What your team gets

| You get | So that | Kit part |
|---------|---------|----------|
| Traces, metrics and logs from **one line of code** | you can see what every request did, and never debug blind | [kit/telemetry](kit/telemetry) |
| Defaults that **keep the observability bill down** | a busy day doesn't turn into a surprise invoice | [kit/telemetry](kit/telemetry) |
| An **SLO from one small file** | everyone agrees what "working" means, in numbers | [kit/slo](kit/slo) |
| A **dashboard from the same file**: budget left, burn rate, traffic, latency, your dependencies | one screen answers "are customers OK, and if not, why?" | [kit/slo](kit/slo/README.md#dashboard) |
| **Pages only when customers are hurting**, fast enough to matter | you sleep through blips and wake up for real incidents | [kit/slo](kit/slo) |
| A **one-page runbook template** | whoever is on call knows what to do first | [kit/runbook](kit/runbook) |
| A **pipeline you reference, not copy**: test, policy gate, scan, deploy | unsafe changes are stopped early, and fixes reach you automatically | [kit/pipeline](kit/pipeline) |

## Adopt it in five steps

**1. Add telemetry.** Add the package to your image and one line to your app:
```python
import golden_telemetry
golden_telemetry.instrument(app)
```
Then set `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_EXPORTER_OTLP_ENDPOINT` in your
Deployment. Read the [telemetry standard](kit/telemetry/README.md) once, especially the label
rules: labels are what the observability platform bills for.

**2. Write your SLO.** Copy [kit/slo/availability-api.yaml](kit/slo/availability-api.yaml), set
your target, latency threshold and runbook link, then generate and commit the rules and
dashboard ([how](kit/slo/README.md)). Not sure what target to pick? Start from what your customers would
notice, not from what you currently achieve, and agree it with your product owner.

**3. Start from the safe Deployment.** Copy [deploy/availability-api](deploy/availability-api):
zero-downtime rollouts, a disruption budget, probes that don't restart pods just because a
dependency is down, non-root, and no CPU limits (throttling shows up as latency). The policy gate
checks these for you.

**4. Use the pipeline.** About ten lines in your repo reference the
[reusable workflow](kit/pipeline/README.md). On every pull request your service is tested against
a real database, checked against the golden-path policy, scanned, and deployed to a throwaway
cluster with a smoke test.

**5. Write your first runbook.** Copy the [template](kit/runbook/TEMPLATE.md) for your most likely
failure and link it from your SLO spec's `runbook_url`. See the
[filled-in example](docs/runbooks/dependency-failure.md).

## How alerting works for you

- **Paging alerts** fire when the error budget is burning fast enough to matter: about 14× the
  sustainable rate over the last 5 minutes *and* hour, or 6× over 30 minutes *and* 6 hours. They
  link to your runbook.
- **Warnings** catch slow burns and explain causes (for example "circuit breaker open"). They go to
  the team channel and don't wake anyone.
- **Why budget burn and not "error rate > X%":** the same outage matters far more at peak. For the
  availability API, a full outage during the Thursday 09:00 peak (~1,000 req/s) uses the whole
  monthly budget in about 4.5 minutes; at normal traffic it takes about an hour.

## When the error budget runs out

Our assumption until the product team confirms it (DECISIONS.md, question Q1):

1. **Budget used up** over the rolling 30 days: feature releases pause. Reliability fixes and
   security patches still ship, and the top cause of budget spend goes to the top of the next sprint.
2. **Less than 25% left:** releases continue, but risky changes (schema migrations, dependency
   upgrades) wait, and the team reviews what's spending it.
3. **Budget recovers:** releases resume. Any single incident that used more than 20% of the budget
   gets a post-incident review either way.

Remaining error budget over the rolling 30-day SLO window, in Grafana: `1 - slo:availability_api:error_ratio:rate30d / 0.0015`.

## Verification and evidence

The latest GitHub Actions CI run passed for commit
[`3f7c7bb`](https://github.com/rembun/golden-path-sre/commit/3f7c7bba736c08d5ec1438bc647028566a191aff).

- [Latest CI run](https://github.com/rembun/golden-path-sre/actions/runs/45650887950)
- [Test and operational evidence](docs/evidence/README.md)
- [Dependency failure runbook](docs/runbooks/dependency-failure.md)
- [Retry-storm exercise](docs/retry-storm.md)
- [Architecture decisions, assumptions, costs and known gaps](DECISIONS.md)

Run the local validation checks with `make test`, `make slo-test`,
`make slo-verify` and `make policy`.

The existing-k3s runtime path was validated. The default k3d path has
not yet been exercised on the assessment host. Peak capacity also remains
unvalidated in the constrained single-node environment; see
[known gaps in DECISIONS.md](DECISIONS.md#7-known-gaps-and-shortcuts).

## Monitoring platform

The local platform uses the following pinned Helm chart versions:

| Component | Helm chart | Application |
|-----------|------------|-------------|
| kube-prometheus-stack | 93.9.0 | Prometheus Operator v0.95.2 |
| Prometheus | managed by kube-prometheus-stack | 3.16.0 |
| Grafana | managed by kube-prometheus-stack | 13.3.1 |
| Alertmanager | managed by kube-prometheus-stack | 0.35.0 |
| Tempo | 3.2.1 | 3.1.1 |
| OpenTelemetry Collector | 0.178.2 | 0.163.2 |

Versions are pinned in the repository Makefile. Review chart changes and
verify rendered manifests before applying upgrades.

### Prometheus storage and retention

Prometheus is configured to retain up to 30 days of metrics, subject to a
4GB retention-size limit. Its local PVC requests 5Gi. Whichever retention
limit is reached first determines how much history remains available, so
30 days is not guaranteed if the size limit is reached earlier.

The persistent volume uses the Retain reclaim policy so that deleting the
PVC does not automatically delete the underlying volume. This does not
protect against disk failure or data corruption and is not a substitute
for backups.

### Grafana resources

Grafana requests 500m CPU and 256Mi memory, with a 768Mi memory limit.
These settings were increased after the previous memory limit caused OOM
kills. Review resource usage when the cluster or workload changes.

### Monitoring verification

After deploying or upgrading the stack:

```bash
helm list -n observability
kubectl -n observability get pods
kubectl -n observability get pvc
```

Check Prometheus targets in its UI or through its `/api/v1/targets` endpoint.
All expected scrape targets should report `up`.

Chart values are maintained in `platform/helm/`, and deployment versions
are maintained in the root Makefile.

## Run it locally

Everything runs on your machine in Kubernetes (k3s), with the full observability stack.

**Prerequisites**, installed from official releases or your OS package manager (not `curl | bash`):
Docker, kubectl, Helm 3, make, openssl, and [k3d](https://k3d.io) v5.9+ (not needed with an existing
k3s). About 6 GB of free memory.

**Hostnames:** the API and Grafana use `.test` hostnames (a reserved domain that never resolves on
the internet). Add them to `/etc/hosts` once:
```
127.0.0.1  api.sre.test grafana.sre.test
```
If k3s runs on another machine, use that machine's IP instead of `127.0.0.1`.

```bash
make up                  # k3s in Docker via k3d: cluster, secrets, observability, build, deploy, alerts
make up RUNTIME=k3s      # or use an existing k3s on this machine (see below)
make status              # pods and URLs
```

| What | How |
|------|-----|
| Call the API | `curl "http://api.sre.test:8080/venues/venue-001/availability?date=$(date -u -d tomorrow +%F)"` (port 80 with `RUNTIME=k3s`) |
| Grafana | http://grafana.sre.test:8080, user `admin`, password from `make grafana-password`. Dashboards → **availability-api · SLO** |
| Start the SLO from clean | Chaos and storm runs are most of a lab cluster's traffic, so the 30-day budget goes far below 0 after them (correct maths, not a bug). `make down && make up` starts a fresh 30 days |
| Break the dependency | `make chaos MODE=outage` (also `slow`, `error`, `unauthorized`; back with `MODE=normal`) |
| Load test | `make load SCENARIO=steady`, `peak` or `storm` (add `RATE_SCALE=0.5` on a small machine) |
| Tests | `make test` (service, against a throwaway PostgreSQL), `make slo-test`, `make slo-verify`, `make policy` |
| Tear down | `make down` |

### Using an existing k3s install

```bash
# Let kubectl and Helm talk to k3s without sudo
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown $USER ~/.kube/config
export KUBECONFIG=~/.kube/config

make up RUNTIME=k3s
```
Images are built with Docker and copied into k3s's own containerd (`k3s ctr images import`); URLs
use port 80 (k3s's Traefik). `make down RUNTIME=k3s` removes only the namespaces and Helm releases
this repo created, never the k3s installation.

## Repository layout

```
services/availability-api     the API (FastAPI, PostgreSQL): the onboarded service
services/venue-capacity-fake  test double for the capacity dependency, with a chaos switch
kit/telemetry                 golden path: telemetry standard and Python package
kit/slo                       golden path: SLO spec -> recording rules and burn-rate alerts
kit/runbook                   golden path: one-page runbook template
kit/pipeline                  golden path: policy gate (the workflow is in .github/workflows)
platform/                     cluster config, observability stack, generated and cause-based alerts
deploy/                       Kubernetes manifests in the bookings team's namespace
load/                         k6 load script (steady, peak, retry storm) and its in-cluster Job
docs/                         runbook, retry-storm exercise, build plan, evidence
```
