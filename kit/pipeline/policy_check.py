#!/usr/bin/env python3
"""Golden-path policy gate.

Runs in the pipeline before deploy (and locally with `make policy`). It checks the
things that make a service safe to run and cheap to observe, so reviewers don't
have to remember them:

  Workloads (Deployment, StatefulSet, Job, CronJob)
    W1  every container image has an explicit tag, and it isn't "latest"
    W2  CPU and memory requests, and a memory limit
    W3  runs as non-root, with privilege escalation disabled
    W4  has the app.kubernetes.io/name label
  Deployments
    D1  readiness and liveness probes on every container
    D2  more than one replica (counting an HPA's minReplicas) => a PodDisruptionBudget covers it
    D3  managed by an HPA => no spec.replicas (every apply would reset the HPA's scaling)
  Customer-facing services (targeted by an Ingress)
    S1  an SLO spec exists: <slo-dir>/<deployment-name>.yaml
    S2  the spec generates valid rules (target, windows, names)

Exit code 1 if any rule fails. Each failure says what to change.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "slo"))
from generate import generate  # noqa: E402  (the SLO kit's own generator)

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job", "CronJob"}


def load_documents(manifest_dir: Path) -> list[dict]:
    docs = []
    for path in sorted(manifest_dir.rglob("*.y*ml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind"):
                doc["_file"] = str(path)
                docs.append(doc)
    return docs


def _pod_template(doc: dict) -> dict:
    spec = doc.get("spec", {})
    if doc["kind"] == "CronJob":
        spec = spec.get("jobTemplate", {}).get("spec", {})
    return spec.get("template", {})


def _pod_spec(doc: dict) -> dict:
    return _pod_template(doc).get("spec", {})


def _pod_labels(doc: dict) -> dict:
    return _pod_template(doc).get("metadata", {}).get("labels", {})


def _non_root(pod: dict, container: dict) -> bool:
    for ctx in (container.get("securityContext", {}), pod.get("securityContext", {})):
        if ctx.get("runAsNonRoot") is True:
            return True
        if isinstance(ctx.get("runAsUser"), int) and ctx["runAsUser"] > 0:
            return True
    return False


def check_workload(doc: dict) -> list[str]:
    name = f'{doc["kind"]}/{doc["metadata"]["name"]}'
    errors = []
    if "app.kubernetes.io/name" not in doc["metadata"].get("labels", {}):
        errors.append(f"{name}: W4 add the label app.kubernetes.io/name")
    pod = _pod_spec(doc)
    for c in pod.get("containers", []):
        cname = f'{name} container "{c["name"]}"'
        image = c.get("image", "")
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        if not tag or tag == "latest":
            errors.append(f"{cname}: W1 pin the image to an explicit tag (got '{image}')")
        res = c.get("resources", {})
        if not res.get("requests", {}).get("cpu") or not res.get("requests", {}).get("memory"):
            errors.append(f"{cname}: W2 set resources.requests.cpu and resources.requests.memory")
        if not res.get("limits", {}).get("memory"):
            errors.append(f"{cname}: W2 set resources.limits.memory")
        if not _non_root(pod, c):
            errors.append(f"{cname}: W3 run as non-root (runAsNonRoot: true or a non-zero runAsUser)")
        if c.get("securityContext", {}).get("allowPrivilegeEscalation") is not False:
            errors.append(f"{cname}: W3 set securityContext.allowPrivilegeEscalation: false")
        if doc["kind"] == "Deployment":
            for probe in ("readinessProbe", "livenessProbe"):
                if probe not in c:
                    errors.append(f"{cname}: D1 add a {probe}")
    return errors


def _selects(pdb: dict, labels: dict) -> bool:
    wanted = pdb.get("spec", {}).get("selector", {}).get("matchLabels", {})
    return bool(wanted) and all(labels.get(k) == v for k, v in wanted.items())


def _hpa_for(docs: list[dict], deployment: dict) -> dict | None:
    ns, name = deployment["metadata"].get("namespace"), deployment["metadata"]["name"]
    for h in docs:
        ref = h.get("spec", {}).get("scaleTargetRef", {})
        if (h["kind"] == "HorizontalPodAutoscaler" and h["metadata"].get("namespace") == ns
                and ref.get("kind") == "Deployment" and ref.get("name") == name):
            return h
    return None


def check_scaling(docs: list[dict]) -> list[str]:
    errors = []
    for d in docs:
        if d["kind"] == "Deployment" and _hpa_for(docs, d) and "replicas" in d.get("spec", {}):
            errors.append(f'Deployment/{d["metadata"]["name"]}: D3 remove spec.replicas; its '
                          "HorizontalPodAutoscaler sets the replica count")
    return errors


def check_disruption_budgets(docs: list[dict]) -> list[str]:
    pdbs = [d for d in docs if d["kind"] == "PodDisruptionBudget"]
    errors = []
    for d in docs:
        if d["kind"] != "Deployment":
            continue
        hpa = _hpa_for(docs, d)
        replicas = hpa["spec"].get("minReplicas", 1) if hpa else d.get("spec", {}).get("replicas", 1)
        if replicas < 2:
            continue
        ns = d["metadata"].get("namespace")
        if not any(p["metadata"].get("namespace") == ns and _selects(p, _pod_labels(d)) for p in pdbs):
            errors.append(f'Deployment/{d["metadata"]["name"]}: D2 add a PodDisruptionBudget for its pods')
    return errors


def customer_facing(docs: list[dict]) -> set[str]:
    """Deployments behind an Ingress: Ingress -> Service -> Deployment (by selector)."""
    services = {
        (s["metadata"].get("namespace"), s["metadata"]["name"]): s.get("spec", {}).get("selector", {})
        for s in docs if s["kind"] == "Service"
    }
    exposed = set()
    for ing in (d for d in docs if d["kind"] == "Ingress"):
        ns = ing["metadata"].get("namespace")
        for rule in ing.get("spec", {}).get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                svc = path.get("backend", {}).get("service", {}).get("name")
                selector = services.get((ns, svc), {})
                for d in docs:
                    if (d["kind"] == "Deployment" and d["metadata"].get("namespace") == ns
                            and selector and all(_pod_labels(d).get(k) == v for k, v in selector.items())):
                        exposed.add(d["metadata"]["name"])
    return exposed


def check_slos(docs: list[dict], slo_dir: Path) -> list[str]:
    errors = []
    for name in sorted(customer_facing(docs)):
        spec_path = slo_dir / f"{name}.yaml"
        if not spec_path.exists():
            errors.append(f"Deployment/{name}: S1 is customer-facing, add an SLO spec at {spec_path}")
            continue
        try:
            generate(yaml.safe_load(spec_path.read_text()))
        except Exception as exc:  # any error in the spec blocks the deploy
            errors.append(f"{spec_path}: S2 SLO spec is invalid: {exc}")
    return errors


def run(manifest_dir: Path, slo_dir: Path) -> list[str]:
    docs = load_documents(manifest_dir)
    errors = []
    for d in docs:
        if d["kind"] in WORKLOAD_KINDS:
            errors += check_workload(d)
    errors += check_disruption_budgets(docs)
    errors += check_scaling(docs)
    errors += check_slos(docs, slo_dir)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifests", required=True, type=Path)
    parser.add_argument("--slo-dir", required=True, type=Path)
    args = parser.parse_args()

    errors = run(args.manifests, args.slo_dir)
    if errors:
        print(f"Policy gate FAILED ({len(errors)} problem(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("Policy gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
