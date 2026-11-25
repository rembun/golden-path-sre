#!/usr/bin/env python3
"""Generate a Grafana dashboard (as a ConfigMap) from the same SLO spec as the alerts.

    python kit/slo/generate_dashboard.py --spec kit/slo/<service>.yaml \
        --output platform/dashboards/generated/<service>-dashboard.yaml

Grafana (kube-prometheus-stack) loads every ConfigMap labelled grafana_dashboard: "1",
from any namespace, so the dashboard ships next to the team's SLO rules.

Every service gets the same SLO panels, built on the recording rules from generate.py
(error budget left, SLI, burn rate against the paging thresholds, alerts firing, traffic,
latency against the SLO threshold). Service-specific panels come from the spec's optional
`dashboard.extra_panels`.
"""
import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate import fmt, window_seconds  # noqa: E402  (same helpers as the rules)

DATASOURCE = {"type": "prometheus", "uid": "prometheus"}  # kube-prometheus-stack default uid
PAGE_BURN_RATE = 6  # matches generate.py: burn >= 6 pages (critical)


class Layout:
    """Places panels left to right on Grafana's 24-column grid, wrapping rows."""

    def __init__(self) -> None:
        self.x = self.y = self.row_height = 0
        self.next_id = 1
        self.panels: list[dict] = []

    def row(self, title: str) -> None:
        self._newline()
        self._add({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)

    def add(self, panel: dict, w: int, h: int) -> None:
        if self.x + w > 24:
            self._newline()
        self._add(panel, w, h)

    def _add(self, panel: dict, w: int, h: int) -> None:
        panel["id"] = self.next_id
        panel["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.next_id += 1
        self.panels.append(panel)
        self.x += w
        self.row_height = max(self.row_height, h)
        if self.x >= 24:
            self._newline()

    def _newline(self) -> None:
        if self.x:
            self.y += self.row_height
        self.x, self.row_height = 0, 0


def _target(expr: str, legend: str = "", instant: bool = False) -> dict:
    t = {"refId": "A", "datasource": DATASOURCE, "expr": expr}
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"], t["range"] = True, False
    return t


def no_traffic_is_zero(expr: str) -> str:
    """Rate ratios are 0/0 = NaN when there is no traffic. No traffic means no budget spent,
    so show 0 instead of a blank panel. `x >= 0` drops NaN and `or vector(0)` fills the gap.
    `sum()` drops the metric name, so busy and idle moments carry the same (empty) label set;
    otherwise Grafana's range query draws them as two series with the same legend."""
    return f"sum({expr} >= 0) or vector(0)"


def _thresholds(*steps) -> dict:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def stat(title, expr, unit, thresholds, description="", decimals=None) -> dict:
    defaults = {"unit": unit, "thresholds": thresholds, "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description, "datasource": DATASOURCE,
        "targets": [_target(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "background", "graphMode": "none", "textMode": "value",
                    "justifyMode": "center", "orientation": "auto"},
    }


def timeseries(title, queries, unit, description="", threshold_lines=None, overrides=None) -> dict:
    """queries: list of (expr, legend). threshold_lines: list of (value, color) drawn as lines."""
    targets = []
    for i, (expr, legend) in enumerate(queries):
        t = _target(expr, legend)
        t["refId"] = chr(ord("A") + i)
        targets.append(t)
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "never",
              "spanNulls": False, "thresholdsStyle": {"mode": "off"}}
    thresholds = _thresholds(("green", None))
    if threshold_lines:
        custom["thresholdsStyle"] = {"mode": "dashed"}   # a line, not a red-filled area
        thresholds = _thresholds(("transparent", None), *[(c, v) for v, c in threshold_lines])
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": DATASOURCE,
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom, "thresholds": thresholds,
                                     "color": {"mode": "palette-classic"}, "noValue": "No traffic"},
                        "overrides": overrides or []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def _colour_by_regex(pattern: str, colour: str) -> dict:
    return {"matcher": {"id": "byRegexp", "options": pattern},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}


def state_timeline(title, expr, legend, states, description="") -> dict:
    mapping = {str(s["value"]): {"text": s["text"], "color": s.get("color", "blue"), "index": i}
               for i, s in enumerate(states)}
    return {
        "type": "state-timeline", "title": title, "description": description, "datasource": DATASOURCE,
        "targets": [_target(expr, legend)],
        "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": mapping}],
                                     # Not "thresholds": with mergeValues Grafana ignores the value mappings.
                                     "color": {"mode": "fixed", "fixedColor": "blue"},
                                     "thresholds": _thresholds(("blue", None)),
                                     "custom": {"lineWidth": 0, "fillOpacity": 80}},
                        "overrides": []},
        "options": {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8, "alignValue": "center",
                    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                    "tooltip": {"mode": "single"}},
    }


def alerts_table(title, service, description="") -> dict:
    """Prometheus's own alert state (the ALERTS series) for this service, one row per alert.
    Shows the alert NAME, which a count can't, and is the same state Alertmanager receives."""
    state_colours = {"firing": {"text": "firing", "color": "red", "index": 0},
                     "pending": {"text": "pending", "color": "orange", "index": 1}}
    return {
        "type": "table", "title": title, "description": description, "datasource": DATASOURCE,
        "targets": [{**_target(f'ALERTS{{service="{service}"}}', instant=True), "format": "table"}],
        "fieldConfig": {
            "defaults": {"noValue": "No alerts pending or firing",
                         "custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
            "overrides": [{"matcher": {"id": "byName", "options": "alertstate"},
                           "properties": [{"id": "custom.cellOptions", "value": {"type": "color-background"}},
                                          {"id": "mappings", "value": [{"type": "value", "options": state_colours}]}]}],
        },
        "options": {"showHeader": True, "cellHeight": "sm",
                    "sortBy": [{"displayName": "alertstate", "desc": False}]},
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value": True, "__name__": True, "service": True, "job": True},
            "indexByName": {"alertname": 0, "alertstate": 1, "severity": 2, "slo": 3, "instance": 4},
            "renameByName": {}}}],
    }


def build_dashboard(spec: dict) -> dict:
    service = spec["service"]
    rec = re.sub(r"[^a-zA-Z0-9_]", "_", service)          # same naming as generate.py
    job = spec["prometheus_job"]
    slo, sli = spec["slo"], spec["sli"]
    target = float(slo["target"])
    budget = fmt(Decimal("1") - Decimal(str(slo["target"])))   # exact, as in generate.py
    threshold = float(sli["latency_threshold_seconds"])
    histogram = sli["histogram"]
    window = slo["window"]
    selector = f'job="{job}"'

    alerts = spec.get("burn_rate_alerts", [])
    page_alerts = [a for a in alerts if float(a["burn_rate"]) >= PAGE_BURN_RATE]
    burn_windows = sorted({w for a in page_alerts for w in (a["short_window"], a["long_window"])},
                          key=window_seconds)
    burn_lines = sorted({(float(a["burn_rate"]), "red" if float(a["burn_rate"]) >= 14 else "orange")
                         for a in page_alerts})
    current_burn_window = page_alerts[0]["long_window"] if page_alerts else window

    L = Layout()
    L.row(f"SLO: {slo.get('description', slo['name'])}")
    L.add(stat("Error budget left", f"1 - (slo:{rec}:error_ratio:rate{window} / {budget})", "percentunit",
               _thresholds(("red", None), ("orange", 0), ("green", 0.25)), decimals=1,
               description=f"Share of the {window} error budget ({float(budget):.2%} of requests) not yet used. "
                           "Below 0: budget exhausted, the error-budget policy applies. "
                           "Long windows are evaluated every 5 minutes, so this fills in within 5 minutes of startup. "
                           "In the local lab, chaos and storm runs make up most of the traffic, so expect "
                           "this to be far below 0 after them: that is the maths working, not a bug."), 6, 5)
    L.add(stat(f"SLI over {window}", f"1 - slo:{rec}:error_ratio:rate{window}", "percentunit",
               _thresholds(("red", None), ("green", target)), decimals=3,
               description=f"Share of good requests (not 5xx and under {threshold * 1000:g} ms). "
                           f"Target {target:.2%}. Fills in within 5 minutes of startup."), 6, 5)
    L.add(stat(f"Burn rate ({current_burn_window})", no_traffic_is_zero(f"slo:{rec}:burn_rate:rate{current_burn_window}"),
               "suffix:×", _thresholds(("green", None), ("orange", 1), ("red", PAGE_BURN_RATE)), decimals=1,
               description="1× spends the budget exactly over the SLO window. Higher is faster. "
                           "0 when there is no traffic (nothing is being spent)."), 6, 5)
    L.add(stat("Alerts firing", f'count(ALERTS{{alertstate="firing", service="{service}"}}) or vector(0)',
               "none", _thresholds(("green", None), ("red", 1)),
               description="SLO burn-rate pages and cause-based warnings for this service."), 6, 5)
    L.add(alerts_table("Alerts now (Prometheus alert state)", service,
                       description="Every alert for this service that Prometheus is evaluating as pending or "
                                   "firing right now, from its ALERTS series: the same state Alertmanager "
                                   "receives. Burn-rate alerts page; cause alerts are warnings."), 12, 7)
    L.add(state_timeline("Alerts firing over time",
                         f'max by (alertname) (ALERTS{{service="{service}", alertstate="firing"}})',
                         "{{alertname}}", [{"value": 1, "text": "firing", "color": "red"}],
                         description="When each alert was firing, on the same time axis as the panels below. "
                                     "Empty means not firing."), 12, 7)
    L.add(timeseries("Error budget burn rate",
                     [(no_traffic_is_zero(f"slo:{rec}:burn_rate:rate{w}"), w) for w in burn_windows], "suffix:×",
                     description="Pages fire when both windows of a pair cross their line: "
                                 + ", ".join(f"{a['short_window']}/{a['long_window']} above {fmt(a['burn_rate'])}×"
                                             for a in page_alerts) + ".",
                     threshold_lines=[(v, c) for v, c in burn_lines]), 24, 8)

    L.row("Traffic and latency")
    L.add(timeseries("Requests per second by status",
                     [(f"sum by (http_response_status_code) (rate({histogram}_count{{{selector}}}[1m]))",
                       "{{http_response_status_code}}")], "reqps",
                     description="A sudden jump with 5xx usually means clients are retrying.",
                     overrides=[_colour_by_regex("^2..$", "green"), _colour_by_regex("^4..$", "orange"),
                                _colour_by_regex("^5..$", "red")]), 12, 8)
    L.add(timeseries("Latency",
                     [(f"histogram_quantile(0.99, sum by (le) (rate({histogram}_bucket{{{selector}}}[5m]))) >= 0", "p99"),
                      (f"histogram_quantile(0.5, sum by (le) (rate({histogram}_bucket{{{selector}}}[5m]))) >= 0", "p50")],
                     "s", description=f"The line is the SLO threshold ({threshold * 1000:g} ms).",
                     threshold_lines=[(threshold, "red")]), 12, 8)

    extras = spec.get("dashboard", {}).get("extra_panels", [])
    if extras:
        L.row("Dependencies and service-specific")
        for p in extras:
            if p.get("type", "timeseries") == "state":
                panel = state_timeline(p["title"], p["query"], p.get("legend", ""), p["states"], p.get("description", ""))
            else:
                panel = timeseries(p["title"], [(p["query"], p.get("legend", ""))], p.get("unit", "short"),
                                   p.get("description", ""))
            L.add(panel, 12, 8)

    dashboard = {
        "uid": f"slo-{service}"[:40],
        "title": f"{service} · SLO",
        "description": f"Generated from kit/slo/{service}.yaml by kit/slo/generate_dashboard.py. Don't edit in Grafana.",
        "tags": ["golden-path", "slo", service],
        "timezone": "utc",
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "schemaVersion": 39,
        "version": 1,
        "annotations": {"list": []},
        "templating": {"list": []},
        "links": [],
        "panels": L.panels,
    }
    if spec.get("runbook_url"):
        dashboard["links"].append({"title": "Runbook", "type": "link", "url": spec["runbook_url"],
                                   "targetBlank": True, "icon": "doc"})
    return dashboard


def generate_configmap(spec: dict) -> dict:
    dashboard = build_dashboard(spec)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{spec['service']}-slo-dashboard",
            "namespace": spec["namespace"],
            "labels": {"grafana_dashboard": "1", "app.kubernetes.io/name": spec["service"]},
        },
        "data": {f"{spec['service']}-slo.json": json.dumps(dashboard, indent=2)},
    }


def dashboard_queries(dashboard: dict) -> list[str]:
    return [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--queries-as-rules", help="also write every panel query as a recording rule, "
                                                   "so `promtool check rules` can validate the PromQL")
    args = parser.parse_args()

    spec = yaml.safe_load(Path(args.spec).read_text())
    configmap = generate_configmap(spec)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(configmap, sort_keys=False, width=10000))
    print(f"Generated {out}")

    if args.queries_as_rules:
        dashboard = json.loads(next(iter(configmap["data"].values())))
        rules = [{"record": f"dashboard_query_{i}", "expr": q} for i, q in enumerate(dashboard_queries(dashboard))]
        Path(args.queries_as_rules).write_text(yaml.safe_dump({"groups": [{"name": "dashboard-queries", "rules": rules}]},
                                                              sort_keys=False, width=10000))


if __name__ == "__main__":
    main()
