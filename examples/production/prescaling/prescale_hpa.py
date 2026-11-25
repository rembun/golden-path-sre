#!/usr/bin/env python3
"""Optionally adjust an HPA's minReplicas from inside the cluster.

This helper is used by the optional production pre-scaling example. A scheduled
replica floor may help with a measured, predictable demand peak, but it does not
guarantee capacity or replace representative load testing.

Env: HPA_NAME, MIN_REPLICAS. Uses the pod's service account (RBAC: patch that one HPA).
KUBERNETES_API and SA_DIR can be overridden for testing.
"""
import json
import os
import ssl
import sys
import urllib.request

SA_DIR = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
API = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")


def main() -> int:
    hpa = os.environ["HPA_NAME"]
    min_replicas = int(os.environ["MIN_REPLICAS"])
    namespace = open(f"{SA_DIR}/namespace").read().strip()
    token = open(f"{SA_DIR}/token").read().strip()

    url = f"{API}/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers/{hpa}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"spec": {"minReplicas": min_replicas}}).encode(),
        method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/merge-patch+json"},
    )
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt") if url.startswith("https") else None
    try:
        with urllib.request.urlopen(request, context=context, timeout=10) as response:
            spec = json.load(response)["spec"]
    except urllib.error.HTTPError as exc:
        print(f"failed to patch {namespace}/{hpa}: HTTP {exc.code} {exc.read().decode()[:300]}", file=sys.stderr)
        return 1
    print(f"{namespace}/{hpa}: minReplicas={spec['minReplicas']} maxReplicas={spec['maxReplicas']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
