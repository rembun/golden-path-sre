# Pipeline template

A reusable GitHub Actions workflow, [golden-path-service.yml](../../.github/workflows/golden-path-service.yml).
Reference it from your repository; you don't copy it, so fixes reach every team at once.

```yaml
# .github/workflows/ci.yml in your repo
name: ci
on:
  push: { branches: [main] }
  pull_request: { branches: [main] }
permissions:
  contents: read
jobs:
  pipeline:
    uses: rembun/golden-path-sre/.github/workflows/golden-path-service.yml@v1
    with:
      kit-ref: v1                         # same version as the line above
      service-name: my-service            # also the SLO spec name: <slo-dir>/my-service.yaml
      service-path: services/my-service   # code, requirements-dev.txt, tests
      dockerfile: services/my-service/Dockerfile
      manifests-path: deploy
      slo-dir: slo                        # your SLO spec
      generated-rules-dir: slo/generated  # your generated rules and dashboard, committed
      generated-dashboards-dir: slo/generated
      deploy-script: scripts/ci-deploy.sh
```

Your repository doesn't need a copy of the kit: the workflow checks out your code and the kit
(into `.golden-path`) and runs the policy gate, SLO generators and telemetry package from there.

**Versions.** The platform team tags kit releases (`v1.0.0`, `v1.1.0`) and moves a `v1` tag along
with non-breaking ones. Pin `@v1` and `kit-ref: v1` to get fixes automatically; a breaking change
becomes `v2`, adopted when you're ready. For stricter control, pin a full commit SHA in both places.

## Stages

| Stage | What it does | Fails when |
|-------|--------------|------------|
| **Test** | Unit and integration tests against a real PostgreSQL service container | any test fails, **or any test is skipped** (a missing database must not look like a pass) |
| **Policy gate** | [policy_check.py](policy_check.py), strict Kubernetes schema validation, SLO rules up to date, `promtool check rules` | any rule below is broken |
| **Build and scan** | Builds the image and scans it with Trivy | a fixable `CRITICAL` vulnerability is found |
| **Deploy** | Creates an ephemeral kind cluster, deploys with your `deploy-script`, runs its smoke test | the rollout or smoke test fails |

Deploy only runs when the other three pass. Tools are pinned to versions at least two weeks old, and
downloaded binaries are checked against their published SHA-256 checksums.

## Policy rules

| Rule | Requirement | Why |
|------|-------------|-----|
| W1 | Image tag is explicit, not `latest` | you know exactly what is running, and rollbacks work |
| W2 | CPU and memory requests, memory limit | scheduling works; a leak is contained (no CPU limit: throttling becomes latency) |
| W3 | Non-root, no privilege escalation | limits the damage of a compromised container |
| W4 | `app.kubernetes.io/name` label | dashboards, alerts and cost reports can find the workload |
| D1 | Readiness and liveness probes | traffic only goes to ready pods; stuck pods get restarted |
| D2 | PodDisruptionBudget when replicas > 1 (counting an HPA's `minReplicas`) | node maintenance can't take the whole service down |
| D3 | No `replicas` in a Deployment that an HPA manages | otherwise every deploy resets the autoscaler's scaling |
| S1 | SLO spec for every service behind an Ingress | customer-facing means someone gets paged when customers hurt |
| S2 | The SLO spec generates valid rules | a broken spec is caught before it silently stops alerting |

Run it locally: `make policy`. When this gate was first run against this repo's own manifests it
found three real gaps (no resources on the seed Job, no liveness probe on the fake service), which
were then fixed.

## Deploying to a real environment

The deploy stage here targets an ephemeral cluster, which proves the manifests, image and smoke
test work together. For a shared environment, add a job that `needs: deploy` and uses a GitHub
**environment** with required reviewers, authenticating to AKS with OIDC workload identity (no
stored cloud credentials). See DECISIONS.md, "One more sprint".
