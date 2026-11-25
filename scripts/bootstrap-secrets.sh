#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname -- "${BASH_SOURCE[0]}")/.."

ENV_FILE=".env.local"

command -v openssl >/dev/null 2>&1 || {
  echo "ERROR: openssl is required."
  exit 1
}

command -v kubectl >/dev/null 2>&1 || {
  echo "ERROR: kubectl is required."
  exit 1
}

POSTGRES_PASSWORD=""
CAPACITY_TOKEN=""
GRAFANA_ADMIN_PASSWORD=""

if [[ -f "$ENV_FILE" ]]; then
  source "$ENV_FILE"
fi

# Repair missing or empty credentials without replacing valid ones.
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"
CAPACITY_TOKEN="${CAPACITY_TOKEN:-$(openssl rand -hex 16)}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(openssl rand -hex 12)}"

# Persist credentials securely.
umask 077
TEMP_FILE="$(mktemp "${ENV_FILE}.XXXXXX")"
trap 'rm -f "$TEMP_FILE"' EXIT

{
  printf 'POSTGRES_PASSWORD=%s\n' "$POSTGRES_PASSWORD"
  printf 'CAPACITY_TOKEN=%s\n' "$CAPACITY_TOKEN"
  printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$GRAFANA_ADMIN_PASSWORD"
} > "$TEMP_FILE"

chmod 600 "$TEMP_FILE"
mv "$TEMP_FILE" "$ENV_FILE"
trap - EXIT

apply_secret() {
  kubectl create secret generic "$@" \
    --dry-run=client -o yaml |
    kubectl apply -f -
}

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

echo "Secrets validated and applied successfully."
