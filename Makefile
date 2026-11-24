# Golden path: local platform on k3s.
#   make up                 k3s in Docker via k3d (default; what reviewers run)
#   make up RUNTIME=k3s     an existing k3s install on this machine
#   make down               remove everything this repo created
# Prerequisites: docker, kubectl, helm, openssl, plus k3d for the default runtime (see README).

RUNTIME   ?= k3d
CLUSTER   := golden-path
NS_APP    := bookings
NS_OBS    := observability

KPS_VERSION   := 93.9.0
TEMPO_VERSION := 3.2.1
OTEL_VERSION  := 0.178.2

.PHONY: up cluster namespaces secrets platform build deploy alerts dashboards status test chaos load prescale-up prescale-down grafana-password down

up: cluster secrets platform build deploy alerts dashboards status

ifeq ($(RUNTIME),k3s)
INGRESS_PORT := 80
else
INGRESS_PORT := 8080
endif

cluster:
ifeq ($(RUNTIME),k3s)
	@kubectl get nodes >/dev/null 2>&1 || { echo "Cannot reach k3s. Point kubectl at it, e.g. export KUBECONFIG=~/.kube/config (see README)"; exit 1; }
	@echo "Using existing k3s cluster:"; kubectl get nodes
else
	@k3d cluster list $(CLUSTER) >/dev/null 2>&1 || k3d cluster create --config platform/k3d-cluster.yaml
	kubectl config use-context k3d-$(CLUSTER)
endif

namespaces:
	kubectl apply -f platform/namespaces.yaml

secrets: namespaces
	./scripts/bootstrap-secrets.sh

platform: namespaces
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	helm repo add grafana-community https://grafana-community.github.io/helm-charts --force-update
	helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts --force-update
	helm upgrade --install kps prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) \
	  -n $(NS_OBS) -f platform/helm/kube-prometheus-stack.values.yaml --wait --timeout 10m
	helm upgrade --install tempo grafana-community/tempo --version $(TEMPO_VERSION) \
	  -n $(NS_OBS) -f platform/helm/tempo.values.yaml --wait --timeout 5m
	helm upgrade --install otel-collector open-telemetry/opentelemetry-collector --version $(OTEL_VERSION) \
	  -n $(NS_OBS) -f platform/helm/otel-collector.values.yaml --wait --timeout 5m
	kubectl apply -f platform/otel-collector-servicemonitor.yaml
	kubectl apply -f platform/dashboards/alertmanager-overview.yaml

build:
	docker build -f services/availability-api/Dockerfile -t availability-api:local .
	docker build -t venue-capacity-fake:local services/venue-capacity-fake
ifeq ($(RUNTIME),k3s)
	# k3s uses its own containerd, not Docker's image store: copy the images in.
	docker save availability-api:local venue-capacity-fake:local | /usr/local/bin/k3s ctr images import -
else
	k3d image import -c $(CLUSTER) availability-api:local venue-capacity-fake:local
endif

deploy:
	kubectl apply -f deploy/postgres/
	kubectl -n $(NS_APP) rollout status statefulset/postgres --timeout=180s
	kubectl -n $(NS_APP) delete job availability-api-seed --ignore-not-found
	kubectl apply -f deploy/availability-api/seed-job.yaml
	kubectl -n $(NS_APP) wait --for=condition=complete job/availability-api-seed --timeout=180s
	kubectl apply -f deploy/venue-capacity-fake/ -f deploy/availability-api/availability-api.yaml
	kubectl apply -f deploy/availability-api/hpa.yaml
	kubectl -n $(NS_APP) create configmap availability-api-prescale-script \
	  --from-file=scripts/prescale_hpa.py --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply -f deploy/availability-api/prescale.yaml
	kubectl -n $(NS_APP) rollout restart deployment/availability-api deployment/venue-capacity-fake
	kubectl -n $(NS_APP) rollout status deployment/venue-capacity-fake --timeout=180s
	kubectl -n $(NS_APP) rollout status deployment/availability-api --timeout=180s

# SLO rules (generated from kit/slo/<service>.yaml) and cause-based alerts.
alerts:
	kubectl apply -f platform/alerts/generated/ -f platform/alerts/availability-alerts.yaml

# Grafana dashboards (generated from kit/slo/<service>.yaml). Grafana picks them up within
# about a minute: Dashboards -> "availability-api · SLO".
dashboards:
	kubectl apply -f platform/dashboards/generated/

status:
	@kubectl get pods -n $(NS_APP) -o wide
	@echo
	@echo "API:     curl 'http://api.sre.test:$(INGRESS_PORT)/venues/venue-001/availability?date=$$(date -u +%F)'"
	@echo "Grafana: http://grafana.sre.test:$(INGRESS_PORT)  (user admin, password: make grafana-password)"
	@echo "(needs '127.0.0.1 api.sre.test grafana.sre.test' in /etc/hosts; see README)"

# Unit and integration tests against a throwaway PostgreSQL container.
test:
	docker rm -f gp-test-pg >/dev/null 2>&1 || true
	docker run -d --name gp-test-pg -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test \
	  -e POSTGRES_DB=test -p 55432:5432 postgres:16 >/dev/null
	@until docker exec gp-test-pg pg_isready -U test >/dev/null 2>&1; do sleep 1; done
	python3 -m venv .venv
	$(PYTHON) -m pip install -q ./kit/telemetry/python -r services/availability-api/requirements-dev.txt
	$(PYTHON) -m pytest -q kit/telemetry/python/tests
	TEST_DATABASE_URL=postgresql://test:test@localhost:55432/test $(PYTHON) -m pytest -q services/availability-api
	docker rm -f gp-test-pg >/dev/null

# Make the fake capacity service misbehave:  make chaos MODE=outage|slow|error|unauthorized|normal
MODE ?= normal
chaos:
	kubectl -n $(NS_APP) exec deploy/venue-capacity-fake -- python -c \
	  "import urllib.request as u; r=u.Request('http://localhost:8081/admin/mode', data=b'{\"mode\": \"$(MODE)\"}', method='PUT', headers={'content-type':'application/json'}); print(u.urlopen(r).read().decode())"

# Load test inside the cluster:  make load SCENARIO=steady|peak|storm [RATE_SCALE=0.3]
SCENARIO     ?= steady
RATE_SCALE   ?= 1
PEAK_MINUTES ?= 25
load:
	kubectl -n $(NS_APP) create configmap k6-scripts --from-file=load/availability.js \
	  --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n $(NS_APP) delete job k6-load --ignore-not-found
	sed -e 's/__SCENARIO__/$(SCENARIO)/' -e 's/__RATE_SCALE__/$(RATE_SCALE)/' \
	  -e 's/__PEAK_MINUTES__/$(PEAK_MINUTES)/' load/k6-job.yaml | kubectl apply -f -
	kubectl -n $(NS_APP) wait --for=condition=Ready pod -l app.kubernetes.io/name=k6-load --timeout=120s
	@echo "Following k6 output (Ctrl+C stops following; the test keeps running):"
	kubectl -n $(NS_APP) logs -f job/k6-load

# Try the pre-scaling now instead of waiting for Thursday:  make prescale-up / make prescale-down
prescale-up prescale-down:
	kubectl -n $(NS_APP) create job --from=cronjob/availability-api-$@ $@-$$(date +%s)
	@sleep 8; kubectl -n $(NS_APP) get hpa availability-api

grafana-password:
	@grep GRAFANA_ADMIN_PASSWORD .env.local | cut -d= -f2

down:
ifeq ($(RUNTIME),k3s)
	# Never uninstall k3s itself: only remove what this repo created.
	-helm uninstall otel-collector tempo kps -n $(NS_OBS)
	-kubectl delete namespace $(NS_APP) $(NS_OBS)
else
	k3d cluster delete $(CLUSTER)
endif

.PHONY: slo-setup slo-generate slo-test slo-verify slo-deploy policy

PYTHON := .venv/bin/python

slo-setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -q -r kit/slo/requirements.txt

slo-generate: slo-setup
	$(PYTHON) kit/slo/generate.py \
	  --spec kit/slo/availability-api.yaml \
	  --output platform/alerts/generated/availability-api-slo.yaml
	$(PYTHON) kit/slo/generate_dashboard.py \
	  --spec kit/slo/availability-api.yaml \
	  --output platform/dashboards/generated/availability-api-dashboard.yaml

slo-test: slo-setup
	$(PYTHON) -m unittest discover -s kit/slo/tests -v

# promtool checks the generated rules and replays the retry storm against them.
# Uses the official Prometheus image, so there is nothing extra to install.
PROMETHEUS_IMAGE := docker.io/prom/prometheus:v3.16.0
slo-verify: slo-generate
	mkdir -p out
	$(PYTHON) kit/slo/extract_rules.py platform/alerts/generated/availability-api-slo.yaml out/availability-api-slo.rules.yaml
	$(PYTHON) kit/slo/extract_rules.py platform/alerts/availability-alerts.yaml out/availability-api-alerts.rules.yaml
	$(PYTHON) kit/slo/generate_dashboard.py --spec kit/slo/availability-api.yaml \
	  --output out/dashboard.yaml --queries-as-rules out/dashboard-queries.rules.yaml
	docker run --rm -v "$(CURDIR):/work" -w /work --entrypoint promtool $(PROMETHEUS_IMAGE) \
	  check rules out/availability-api-slo.rules.yaml out/availability-api-alerts.rules.yaml out/dashboard-queries.rules.yaml
	docker run --rm -v "$(CURDIR):/work" -w /work --entrypoint promtool $(PROMETHEUS_IMAGE) \
	  test rules kit/slo/tests/promtool/*.test.yaml

slo-deploy: slo-generate
	kubectl apply -f platform/alerts/generated/availability-api-slo.yaml

# The golden-path policy gate: the same check the pipeline runs.
policy: slo-setup
	$(PYTHON) kit/pipeline/policy_check.py --manifests deploy --slo-dir kit/slo
