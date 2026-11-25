#!/usr/bin/env bash
# Waits until an alert is firing in Prometheus, then saves Prometheus's and Alertmanager's
# alert state to docs/evidence/, as machine-readable proof behind the Grafana screenshot.
# It only reads state: it changes no rules, data or configuration.
#
#   make load SCENARIO=storm                   # in one terminal: the existing outage simulation
#   make evidence                              # in another: waits for the page, saves the evidence
#
# Options (environment):
#   ALERT      alert to wait for              (default AvailabilityApiSLOBurnRate5M)
#   SERVICE    service label on the alerts    (default availability-api)
#   TIMEOUT_S  how long to wait               (default 900)
#   PROM_URL / AM_URL  use these instead of port-forwarding into the cluster
set -euo pipefail
cd "$(dirname "$0")/.."

ALERT=${ALERT:-AvailabilityApiSLOBurnRate5M}
SERVICE=${SERVICE:-availability-api}
TIMEOUT_S=${TIMEOUT_S:-900}
NS_OBS=${NS_OBS:-observability}
OUT=docs/evidence
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
PIDS=()
trap 'for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done' EXIT

if [ -z "${PROM_URL:-}" ]; then
  kubectl -n "$NS_OBS" port-forward svc/kps-kube-prometheus-stack-prometheus 19090:9090 >/dev/null 2>&1 &
  PIDS+=($!); PROM_URL=http://127.0.0.1:19090
fi
if [ -z "${AM_URL:-}" ]; then
  kubectl -n "$NS_OBS" port-forward svc/kps-kube-prometheus-stack-alertmanager 19093:9093 >/dev/null 2>&1 &
  PIDS+=($!); AM_URL=http://127.0.0.1:19093
fi
for _ in $(seq 20); do curl -sf "$PROM_URL/-/ready" >/dev/null && break; sleep 1; done

firing() {
  curl -sf "$PROM_URL/api/v1/alerts" | python3 -c '
import json, sys
alerts = json.load(sys.stdin)["data"]["alerts"]
sys.exit(0 if any(a["labels"].get("alertname") == sys.argv[1] and a["state"] == "firing" for a in alerts) else 1)
' "$ALERT"
}

echo "Waiting up to ${TIMEOUT_S}s for $ALERT to fire (start the outage with: make load SCENARIO=storm)..."
deadline=$(( $(date +%s) + TIMEOUT_S ))
until firing; do
  [ "$(date +%s)" -ge "$deadline" ] && { echo "Timed out: $ALERT did not fire." >&2; exit 1; }
  sleep 10
done
echo "$ALERT is firing. Saving evidence."

mkdir -p "$OUT"
# 1. Prometheus's alert state for this service (the source the dashboard's alert panels read)
curl -sf "$PROM_URL/api/v1/alerts" | python3 -c '
import json, sys
data = json.load(sys.stdin)["data"]["alerts"]
mine = [a for a in data if a["labels"].get("service") == sys.argv[1]]
json.dump({"source": "Prometheus /api/v1/alerts", "service": sys.argv[1], "alerts": mine}, sys.stdout, indent=2)
' "$SERVICE" > "$OUT/prometheus-alerts-$STAMP.json"

# 2. The burn rates at the same moment (why it fired: both windows above 14.4x)
python3 - "$PROM_URL" > "$OUT/burn-rates-$STAMP.json" <<'EOF'
import json, sys, urllib.parse, urllib.request
prom = sys.argv[1]
result = {"source": "Prometheus /api/v1/query", "page_threshold": 14.4}
for w in ("5m", "1h", "30m", "6h"):
    q = f"slo:availability_api:burn_rate:rate{w}"
    r = json.load(urllib.request.urlopen(f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": q})))
    v = r["data"]["result"]
    result[f"burn_rate_{w}"] = round(float(v[0]["value"][1]), 2) if v else None
json.dump(result, sys.stdout, indent=2)
EOF

# 3. Alertmanager's copy: the alert was delivered for routing, not just evaluated
if curl -sf "$AM_URL/-/ready" >/dev/null 2>&1; then
  curl -sf "$AM_URL/api/v2/alerts?filter=alertname%3D%22$ALERT%22" | python3 -c '
import json, sys
json.dump({"source": "Alertmanager /api/v2/alerts", "alerts": json.load(sys.stdin)}, sys.stdout, indent=2)
' > "$OUT/alertmanager-alerts-$STAMP.json"
else
  echo "Alertmanager not reachable; skipped its copy." >&2
fi

python3 - "$OUT/prometheus-alerts-$STAMP.json" "$OUT/burn-rates-$STAMP.json" "$ALERT" <<'EOF'
import json, sys
alerts = json.load(open(sys.argv[1]))["alerts"]
burn = json.load(open(sys.argv[2]))
a = next(x for x in alerts if x["labels"]["alertname"] == sys.argv[3])
print(f'{a["labels"]["alertname"]}: {a["state"]} since {a["activeAt"]} (severity {a["labels"].get("severity")})')
print(f'burn rate 5m {burn["burn_rate_5m"]}x, 1h {burn["burn_rate_1h"]}x (page threshold {burn["page_threshold"]}x on both)')
others = sorted({x["labels"]["alertname"] + " (" + x["state"] + ")" for x in alerts} - {a["labels"]["alertname"] + " (" + a["state"] + ")"})
if others: print("also active for this service:", ", ".join(others))
EOF
echo "Saved to $OUT/*-$STAMP.json. Now screenshot Grafana: availability-api · SLO, Last 15 minutes, with Alerts now in view."
