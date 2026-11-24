#!/usr/bin/env python3
"""Generate Prometheus SLI recording rules and multi-window burn alerts (PrometheusRule CRD)."""

import argparse
import re
from decimal import Decimal
from pathlib import Path

import yaml

METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
SLOW_GROUP_FROM_SECONDS = 24 * 3600   # windows >= 24h are evaluated less often (they are expensive)


def window_seconds(window: str) -> int:
    match = re.fullmatch(r"(\d+)([smhdw])", window)
    if not match:
        raise ValueError(f"invalid window '{window}' (use e.g. 5m, 1h, 3d)")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def fmt(number) -> str:
    """Number -> clean string for PromQL (no 0.0015000000000000568, no 6.0)."""
    return format(Decimal(str(number)).normalize(), "f")


def generate(spec: dict) -> dict:
    service = spec["service"]
    rec = re.sub(r"[^a-zA-Z0-9_]", "_", service)   # metric-safe name: hyphens are illegal in recorded series
    job = spec["prometheus_job"]
    slo = spec["slo"]
    sli = spec["sli"]

    target = Decimal(str(slo["target"]))
    if not Decimal("0") < target < Decimal("1"):
        raise ValueError("SLO target must be between 0 and 1")
    budget = fmt(Decimal("1") - target)             # exact: 0.9985 -> 0.0015

    threshold = fmt(sli["latency_threshold_seconds"])
    histogram = sli["histogram"]
    bad_status = sli["bad_status_regex"]
    status_label = sli.get("status_label", "http_response_status_code")

    selector = f'job="{job}"'
    total_metric = f"{histogram}_count"
    bucket_metric = f"{histogram}_bucket"

    windows = sorted(
        {
            slo["window"],
            *(
                window
                for alert in spec["burn_rate_alerts"]
                for window in (alert["short_window"], alert["long_window"])
            ),
        },
        key=window_seconds,
    )

    fast_rules, slow_rules = [], []
    for window in windows:
        total = f"sum(rate({total_metric}{{{selector}}}[{window}]))"
        good = (
            f'sum(rate({bucket_metric}{{{selector},le="{threshold}",'
            f'{status_label}!~"{bad_status}"}}[{window}]))'
        )
        # the four rules of one window stay in ONE group: rules in a group run in order, so dependencies resolve
        window_rules = [
            {"record": f"slo:{rec}:requests:rate{window}", "expr": total},
            {"record": f"slo:{rec}:good_requests:rate{window}", "expr": good},
            {
                "record": f"slo:{rec}:error_ratio:rate{window}",
                "expr": (
                    f"1 - (slo:{rec}:good_requests:rate{window} / "
                    f"slo:{rec}:requests:rate{window})"
                ),
            },
            {
                "record": f"slo:{rec}:burn_rate:rate{window}",
                "expr": f"slo:{rec}:error_ratio:rate{window} / {budget}",
            },
        ]
        target_list = slow_rules if window_seconds(window) >= SLOW_GROUP_FROM_SECONDS else fast_rules
        target_list.extend(window_rules)

    alerts = []
    for alert in spec["burn_rate_alerts"]:
        short, long_, burn = alert["short_window"], alert["long_window"], alert["burn_rate"]
        short_name = f"slo:{rec}:burn_rate:rate{short}"
        long_name = f"slo:{rec}:burn_rate:rate{long_}"
        rule = {
            "alert": f"{service.title().replace('-', '')}SLOBurnRate{short.title()}",
            "expr": f"({short_name} > {fmt(burn)}) and ({long_name} > {fmt(burn)})",
        }
        if alert.get("for"):                         # optional; the two windows already stop flapping
            rule["for"] = alert["for"]
        rule["labels"] = {
            "severity": "critical" if float(burn) >= 6 else "warning",
            "service": service,
            "slo": slo["name"],
        }
        rule["annotations"] = {
            "summary": f"{service} SLO error budget burn is elevated",
            "description": (
                f"Error budget burn rate is above {fmt(burn)}x over both "
                f"{short} and {long_} windows. SLO target: {float(target):.3%}."
            ),
        }
        if spec.get("runbook_url"):                  # every page should say what to do next
            rule["annotations"]["runbook_url"] = spec["runbook_url"]
        alerts.append(rule)

    # fail at generation time, not when Prometheus silently rejects the rule group
    for rule in fast_rules + slow_rules:
        if not METRIC_NAME_RE.match(rule["record"]):
            raise ValueError(f"invalid recording rule name: {rule['record']}")

    groups = [{"name": f"{service}-slo-recording", "interval": "30s", "rules": fast_rules}]
    if slow_rules:
        groups.append({"name": f"{service}-slo-recording-long", "interval": "5m", "rules": slow_rules})
    groups.append({"name": f"{service}-slo-alerts", "interval": "30s", "rules": alerts})

    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": f"{service}-slo",
            "namespace": spec["namespace"],
            "labels": {"release": "kps"},
        },
        "spec": {"groups": groups},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    spec_path = Path(args.spec)
    output_path = Path(args.output)

    with spec_path.open() as stream:
        spec = yaml.safe_load(stream)

    generated = generate(spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as stream:
        yaml.safe_dump(generated, stream, sort_keys=False, width=10000)

    print(f"Generated {output_path}")


if __name__ == "__main__":
    main()
