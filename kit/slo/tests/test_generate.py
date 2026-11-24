import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generate import generate


SPEC = {
    "service": "availability-api",
    "namespace": "observability",
    "prometheus_job": "bookings/availability-api",
    "slo": {
        "name": "availability-availability",
        "target": 0.9985,
        "window": "30d",
    },
    "sli": {
        "histogram": "http_server_request_duration_seconds",
        "latency_threshold_seconds": 0.3,
        "bad_status_regex": "5..",
    },
    "burn_rate_alerts": [
        {"short_window": "5m", "long_window": "1h", "burn_rate": 14.4},
        {"short_window": "30m", "long_window": "6h", "burn_rate": 6},
        {"short_window": "2h", "long_window": "24h", "burn_rate": 3},
        {"short_window": "6h", "long_window": "3d", "burn_rate": 1},
    ],
}


def all_rules(spec=SPEC):
    groups = generate(spec)["spec"]["groups"]
    return [rule for group in groups for rule in group["rules"]]


def record_names(spec=SPEC):
    return {r["record"] for r in all_rules(spec) if "record" in r}


class SloGeneratorTests(unittest.TestCase):

    def test_generates_30_day_slo_window(self):
        names = record_names()

        self.assertIn("slo:availability_api:requests:rate30d", names)
        self.assertIn("slo:availability_api:good_requests:rate30d", names)
        self.assertIn("slo:availability_api:error_ratio:rate30d", names)
        self.assertIn("slo:availability_api:burn_rate:rate30d", names)

    def test_generates_latency_and_status_aware_sli(self):
        expressions = [r.get("expr", "") for r in all_rules()]

        self.assertTrue(any('le="0.3"' in e for e in expressions))
        self.assertTrue(any('http_response_status_code!~"5.."' in e for e in expressions))

    def test_generates_all_burn_rate_windows(self):
        names = record_names()

        for window in ("5m", "30m", "1h", "6h", "2h", "24h", "3d"):
            self.assertIn(f"slo:availability_api:burn_rate:rate{window}", names)

    def test_generates_four_multi_window_alerts(self):
        alerts = [r for r in all_rules() if "alert" in r]

        self.assertEqual(len(alerts), 4)
        self.assertTrue(all(" and " in a["expr"] for a in alerts))

    def test_record_names_are_valid_metric_names(self):
        # hyphens are illegal in Prometheus metric names; Prometheus rejects the whole rule group
        for name in record_names():
            self.assertRegex(name, r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

    def test_every_referenced_slo_series_is_actually_recorded(self):
        names = record_names()
        for rule in all_rules():
            for ref in re.findall(r"slo:[A-Za-z0-9_:]+", rule.get("expr", "")):
                self.assertIn(ref, names, f"{ref} is used but never recorded")

    def test_error_budget_is_exact(self):
        # 1 - 0.9985 must be 0.0015, not 0.0015000000000000568
        exprs = [r["expr"] for r in all_rules() if r.get("record", "").endswith("burn_rate:rate5m")]
        self.assertEqual(len(exprs), 1)
        self.assertTrue(exprs[0].endswith("/ 0.0015"), exprs[0])

    def test_alert_thresholds_have_no_float_noise(self):
        exprs = [r["expr"] for r in all_rules() if "alert" in r]
        self.assertIn("> 14.4)", exprs[0])
        self.assertIn("> 6)", exprs[1])          # 6, not 6.0

    def test_long_windows_are_evaluated_less_often(self):
        groups = {g["name"]: g for g in generate(SPEC)["spec"]["groups"]}
        long_group = groups["availability-api-slo-recording-long"]

        self.assertEqual(long_group["interval"], "5m")
        long_names = {r["record"] for r in long_group["rules"]}
        self.assertIn("slo:availability_api:burn_rate:rate30d", long_names)
        self.assertNotIn("slo:availability_api:burn_rate:rate5m", long_names)

    def test_for_is_optional(self):
        self.assertTrue(all("for" not in r for r in all_rules() if "alert" in r))
        spec = {**SPEC, "burn_rate_alerts": [
            {"short_window": "5m", "long_window": "1h", "burn_rate": 14.4, "for": "2m"}]}
        alert = [r for r in all_rules(spec) if "alert" in r][0]
        self.assertEqual(alert["for"], "2m")

    def test_custom_status_label(self):
        spec = {**SPEC, "sli": {**SPEC["sli"], "status_label": "http_response_status_class"}}
        self.assertTrue(any('http_response_status_class!~"5.."' in r.get("expr", "") for r in all_rules(spec)))

    def test_rejects_invalid_target(self):
        for bad in (1.0, 0, 1.5, -0.1):
            invalid = {**SPEC, "slo": {**SPEC["slo"], "target": bad}}
            with self.assertRaises(ValueError):
                generate(invalid)

    def test_rejects_invalid_window(self):
        invalid = {**SPEC, "burn_rate_alerts": [{"short_window": "5x", "long_window": "1h", "burn_rate": 14.4}]}
        with self.assertRaises(ValueError):
            generate(invalid)


if __name__ == "__main__":
    unittest.main()

    def test_runbook_url_is_added_to_every_alert_when_given(self):
        spec = {**SPEC, "runbook_url": "https://example.test/runbook"}
        alerts = [r for r in all_rules(spec) if "alert" in r]
        self.assertTrue(all(a["annotations"]["runbook_url"] == "https://example.test/runbook" for a in alerts))

    def test_fast_burn_alerts_page_and_slow_burn_alerts_ticket(self):
        severities = {a["expr"].split(" > ")[1].split(")")[0]: a["labels"]["severity"]
                      for a in all_rules() if "alert" in a}
        self.assertEqual(severities, {"14.4": "critical", "6": "critical", "3": "warning", "1": "warning"})
