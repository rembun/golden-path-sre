"""The generated dashboard must load in Grafana and agree with the generated rules."""
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import generate  # noqa: E402
from generate_dashboard import build_dashboard, dashboard_queries, generate_configmap  # noqa: E402

SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "availability-api.yaml").read_text())


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = build_dashboard(SPEC)
        self.panels = self.dashboard["panels"]

    def test_configmap_is_discovered_by_grafana_in_the_team_namespace(self):
        cm = generate_configmap(SPEC)
        self.assertEqual(cm["metadata"]["labels"]["grafana_dashboard"], "1")
        self.assertEqual(cm["metadata"]["namespace"], SPEC["namespace"])
        json.loads(next(iter(cm["data"].values())))  # valid JSON

    def test_uid_is_stable_and_within_grafana_limit(self):
        self.assertEqual(self.dashboard["uid"], "slo-availability-api")
        self.assertLessEqual(len(self.dashboard["uid"]), 40)

    def test_panels_have_unique_ids_and_fit_the_grid(self):
        ids = [p["id"] for p in self.panels]
        self.assertEqual(len(ids), len(set(ids)))
        for p in self.panels:
            g = p["gridPos"]
            self.assertLessEqual(g["x"] + g["w"], 24, p["title"])

    def test_panels_do_not_overlap(self):
        cells = set()
        for p in self.panels:
            g = p["gridPos"]
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    self.assertNotIn((x, y), cells, p["title"])
                    cells.add((x, y))

    def test_every_slo_series_used_is_actually_recorded(self):
        recorded = {r["record"] for g in generate(SPEC)["spec"]["groups"] for r in g["rules"] if "record" in r}
        for q in dashboard_queries(self.dashboard):
            for series in re.findall(r"slo:[A-Za-z0-9_:]+", q):
                self.assertIn(series, recorded, q)

    def test_burn_rate_panel_shows_the_paging_thresholds(self):
        panel = next(p for p in self.panels if p["title"] == "Error budget burn rate")
        lines = {s["value"] for s in panel["fieldConfig"]["defaults"]["thresholds"]["steps"] if s["value"]}
        self.assertEqual(lines, {14.4, 6.0})

    def test_latency_panel_shows_the_slo_threshold(self):
        panel = next(p for p in self.panels if p["title"] == "Latency")
        lines = {s["value"] for s in panel["fieldConfig"]["defaults"]["thresholds"]["steps"] if s["value"]}
        self.assertEqual(lines, {SPEC["sli"]["latency_threshold_seconds"]})

    def test_extra_panels_from_spec_are_added(self):
        titles = [p["title"] for p in self.panels]
        self.assertIn("Venue-capacity calls by outcome", titles)
        breaker = next(p for p in self.panels if p["type"] == "state-timeline")
        mapping = breaker["fieldConfig"]["defaults"]["mappings"][0]["options"]
        self.assertEqual({v["text"].split(":")[0] for v in mapping.values()}, {"closed", "half-open", "open"})

    def test_state_timeline_does_not_colour_by_thresholds(self):
        breaker = next(p for p in self.panels if p["type"] == "state-timeline")
        self.assertNotEqual(breaker["fieldConfig"]["defaults"]["color"]["mode"], "thresholds")

    def test_burn_rate_shows_zero_not_blank_without_traffic(self):
        for p in self.panels:
            for t in p.get("targets", []):
                if ":burn_rate:" in t["expr"]:
                    self.assertIn("or vector(0)", t["expr"], p["title"])

    def test_threshold_lines_do_not_shade_the_panel(self):
        for p in self.panels:
            style = p.get("fieldConfig", {}).get("defaults", {}).get("custom", {}).get("thresholdsStyle", {}).get("mode", "off")
            self.assertNotIn("area", style, p["title"])

    def test_server_errors_are_red(self):
        panel = next(p for p in self.panels if p["title"] == "Requests per second by status")
        colours = {o["matcher"]["options"]: o["properties"][0]["value"]["fixedColor"] for o in panel["fieldConfig"]["overrides"]}
        self.assertEqual(colours["^5..$"], "red")

    def test_runbook_is_linked(self):
        self.assertEqual(self.dashboard["links"][0]["url"], SPEC["runbook_url"])

    def test_a_new_service_needs_only_the_required_spec_fields(self):
        minimal = {k: SPEC[k] for k in ("service", "namespace", "prometheus_job", "slo", "sli", "burn_rate_alerts")}
        minimal["service"] = "payments-api"
        d = build_dashboard(minimal)
        self.assertEqual(d["uid"], "slo-payments-api")
        self.assertTrue(all("slo:payments_api:" in q or "slo:" not in q for q in dashboard_queries(d)))


if __name__ == "__main__":
    unittest.main()
