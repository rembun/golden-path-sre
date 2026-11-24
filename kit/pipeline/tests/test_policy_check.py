"""The policy gate must pass good manifests and catch each rule it claims to enforce."""
import copy
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from policy_check import run  # noqa: E402

GOOD_DEPLOYMENT = {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "metadata": {"name": "api", "namespace": "team", "labels": {"app.kubernetes.io/name": "api"}},
    "spec": {
        "replicas": 3,
        "selector": {"matchLabels": {"app.kubernetes.io/name": "api"}},
        "template": {
            "metadata": {"labels": {"app.kubernetes.io/name": "api"}},
            "spec": {
                "securityContext": {"runAsNonRoot": True},
                "containers": [{
                    "name": "api", "image": "api:1.0.0",
                    "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8080}},
                    "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}},
                    "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "128Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False},
                }],
            },
        },
    },
}
PDB = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
       "metadata": {"name": "api", "namespace": "team"},
       "spec": {"minAvailable": 2, "selector": {"matchLabels": {"app.kubernetes.io/name": "api"}}}}
SERVICE = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "team"},
           "spec": {"selector": {"app.kubernetes.io/name": "api"}, "ports": [{"port": 80}]}}
INGRESS = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "api", "namespace": "team"},
           "spec": {"rules": [{"http": {"paths": [{"path": "/", "pathType": "Prefix",
                    "backend": {"service": {"name": "api", "port": {"number": 80}}}}]}}]}}
SLO_SPEC = textwrap.dedent("""
    service: api
    namespace: team
    prometheus_job: team/api
    slo: {name: api-availability, target: 0.999, window: 30d}
    sli: {histogram: http_server_request_duration_seconds, latency_threshold_seconds: 0.3, bad_status_regex: "5.."}
    burn_rate_alerts:
      - {short_window: 5m, long_window: 1h, burn_rate: 14.4}
""")


class PolicyGateTests(unittest.TestCase):
    def gate(self, docs, slo_spec=SLO_SPEC):
        with TemporaryDirectory() as tmp:
            manifests, slos = Path(tmp, "deploy"), Path(tmp, "slo")
            manifests.mkdir(), slos.mkdir()
            Path(manifests, "all.yaml").write_text(yaml.safe_dump_all(docs))
            if slo_spec is not None:
                Path(slos, "api.yaml").write_text(slo_spec)
            return run(manifests, slos)

    def broken(self, mutate):
        d = copy.deepcopy(GOOD_DEPLOYMENT)
        mutate(d)
        return self.gate([d, PDB, SERVICE, INGRESS])

    def container(self, d):
        return d["spec"]["template"]["spec"]["containers"][0]

    def test_good_service_passes(self):
        self.assertEqual(self.gate([GOOD_DEPLOYMENT, PDB, SERVICE, INGRESS]), [])

    def test_latest_or_missing_tag_fails(self):
        for image in ("api:latest", "api", "registry.example:5000/api"):
            errors = self.broken(lambda d: self.container(d).update(image=image))
            self.assertTrue(any("W1" in e for e in errors), image)

    def test_missing_resources_fail(self):
        errors = self.broken(lambda d: self.container(d).pop("resources"))
        self.assertTrue(any("W2" in e for e in errors))

    def test_root_or_privilege_escalation_fails(self):
        errors = self.broken(lambda d: d["spec"]["template"]["spec"].pop("securityContext"))
        self.assertTrue(any("W3" in e and "non-root" in e for e in errors))
        errors = self.broken(lambda d: self.container(d).pop("securityContext"))
        self.assertTrue(any("W3" in e and "allowPrivilegeEscalation" in e for e in errors))

    def test_missing_probes_fail(self):
        errors = self.broken(lambda d: self.container(d).pop("readinessProbe"))
        self.assertTrue(any("D1" in e and "readinessProbe" in e for e in errors))

    def test_replicated_deployment_needs_pdb(self):
        errors = self.gate([GOOD_DEPLOYMENT, SERVICE, INGRESS])
        self.assertTrue(any("D2" in e for e in errors))

    def hpa(self, min_replicas=3):
        return {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
                "metadata": {"name": "api", "namespace": "team"},
                "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api"},
                         "minReplicas": min_replicas, "maxReplicas": 10}}

    def autoscaled(self):
        d = copy.deepcopy(GOOD_DEPLOYMENT)
        d["spec"].pop("replicas")
        return d

    def test_autoscaled_deployment_passes(self):
        self.assertEqual(self.gate([self.autoscaled(), self.hpa(), PDB, SERVICE, INGRESS]), [])

    def test_hpa_min_replicas_counts_for_the_pdb_rule(self):
        errors = self.gate([self.autoscaled(), self.hpa(min_replicas=3), SERVICE, INGRESS])
        self.assertTrue(any("D2" in e for e in errors))

    def test_replicas_with_hpa_fails(self):
        errors = self.gate([GOOD_DEPLOYMENT, self.hpa(), PDB, SERVICE, INGRESS])
        self.assertTrue(any("D3" in e for e in errors))

    def test_cronjob_containers_are_checked(self):
        cron = {"apiVersion": "batch/v1", "kind": "CronJob",
                "metadata": {"name": "job", "namespace": "team", "labels": {"app.kubernetes.io/name": "job"}},
                "spec": {"schedule": "0 9 * * 4", "jobTemplate": {"spec": {"template": {"spec": {
                    "containers": [{"name": "c", "image": "python:latest"}]}}}}}}
        errors = self.gate([GOOD_DEPLOYMENT, PDB, SERVICE, INGRESS, cron])
        self.assertTrue(any("CronJob/job" in e and "W1" in e for e in errors))
        self.assertTrue(any("CronJob/job" in e and "W2" in e for e in errors))

    def test_customer_facing_service_needs_slo(self):
        errors = self.gate([GOOD_DEPLOYMENT, PDB, SERVICE, INGRESS], slo_spec=None)
        self.assertTrue(any("S1" in e for e in errors))

    def test_internal_service_does_not_need_slo(self):
        self.assertEqual(self.gate([GOOD_DEPLOYMENT, PDB, SERVICE], slo_spec=None), [])

    def test_invalid_slo_spec_fails(self):
        errors = self.gate([GOOD_DEPLOYMENT, PDB, SERVICE, INGRESS], slo_spec=SLO_SPEC.replace("0.999", "1.5"))
        self.assertTrue(any("S2" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
