#!/usr/bin/env bash
# Deploys the availability API to the pipeline's ephemeral kind cluster and smoke-tests it.
# Called by the golden-path pipeline's deploy stage with IMAGE and KIND_CLUSTER set.
# Uses the same manifests as `make deploy`, so CI exercises what we actually run.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${IMAGE:?IMAGE must be set}"
: "${KIND_CLUSTER:=ci}"
NS=bookings

echo "--- images"
# The manifests use availability-api:local with imagePullPolicy: Never, so give the
# scanned image that name inside the cluster.
docker tag "$IMAGE" docker.io/library/availability-api:local
docker build -q -t docker.io/library/venue-capacity-fake:local services/venue-capacity-fake
kind load docker-image --name "$KIND_CLUSTER" \
  docker.io/library/availability-api:local docker.io/library/venue-capacity-fake:local

echo "--- namespaces and secrets"
kubectl apply -f platform/namespaces.yaml
./scripts/bootstrap-secrets.sh

echo "--- deploy (same order as make deploy)"
kubectl apply -f deploy/postgres/
kubectl -n "$NS" rollout status statefulset/postgres --timeout=240s
kubectl apply -f deploy/availability-api/seed-job.yaml
kubectl -n "$NS" wait --for=condition=complete job/availability-api-seed --timeout=240s
kubectl apply -f deploy/venue-capacity-fake/ -f deploy/availability-api/availability-api.yaml
kubectl -n "$NS" rollout status deployment/venue-capacity-fake --timeout=240s
kubectl -n "$NS" rollout status deployment/availability-api --timeout=240s

echo "--- smoke test"
kubectl -n "$NS" port-forward svc/availability-api 18080:80 >/dev/null 2>&1 &
PF=$!
trap 'kill $PF 2>/dev/null || true' EXIT
for _ in $(seq 20); do curl -sf http://127.0.0.1:18080/venues/venue-001/availability?date=2000-01-01 >/dev/null && break; sleep 1; done

TOMORROW=$(date -u -d tomorrow +%F)
BODY=$(curl -sf "http://127.0.0.1:18080/venues/venue-001/availability?date=${TOMORROW}")
SLOT=$(echo "$BODY" | python3 -c 'import json,sys; s=json.load(sys.stdin)["slots"]; print(s[0]["slot_id"] if s else "")')
[ -n "$SLOT" ] || { echo "FAIL: no slots returned"; exit 1; }
echo "ok: availability returns slots"

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
  -d "{\"slot_id\": ${SLOT}}" http://127.0.0.1:18080/venues/venue-001/holds)
[ "$CODE" = 201 ] || { echo "FAIL: hold returned $CODE"; exit 1; }
echo "ok: hold placed"

# Fail-closed check: with the dependency down, the API must answer 503 + Retry-After, fast.
kubectl -n "$NS" exec deploy/venue-capacity-fake -- python -c \
  "import urllib.request as u; u.urlopen(u.Request('http://localhost:8081/admin/mode', data=b'{\"mode\": \"outage\"}', method='PUT', headers={'content-type':'application/json'}))"
HEADERS=$(curl -s -D - -o /dev/null "http://127.0.0.1:18080/venues/venue-001/availability?date=${TOMORROW}")
echo "$HEADERS" | grep -q "^HTTP/1.1 503" || { echo "FAIL: expected 503 during outage"; echo "$HEADERS"; exit 1; }
echo "$HEADERS" | grep -qi "^retry-after:" || { echo "FAIL: 503 without Retry-After"; exit 1; }
echo "ok: fails closed with 503 + Retry-After during a dependency outage"
echo "Smoke test passed."
