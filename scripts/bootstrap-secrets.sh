#!/usr/bin/env bash
# Creates the local cluster's secrets. Values are generated once and kept in
# .env.local, which is git-ignored, so nothing secret is ever committed.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=.env.local
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  {
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
    echo "CAPACITY_TOKEN=$(openssl rand -hex 16)"
    echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 12)"
  } > "$ENV_FILE"
  echo "Generated $ENV_FILE (git-ignored)"
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

apply_secret() { kubectl create secret generic "$@" --dry-run=client -o yaml | kubectl apply -f - >/dev/null; }

apply_secret postgres-credentials -n bookings \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD"
apply_secret availability-api -n bookings \
  --from-literal=DATABASE_URL="postgresql://availability:${POSTGRES_PASSWORD}@postgres:5432/availability" \
  --from-literal=CAPACITY_TOKEN="$CAPACITY_TOKEN"
apply_secret venue-capacity-fake -n bookings \
  --from-literal=CAPACITY_TOKEN="$CAPACITY_TOKEN"
apply_secret grafana-admin -n observability \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$GRAFANA_ADMIN_PASSWORD"
echo "Secrets applied."
