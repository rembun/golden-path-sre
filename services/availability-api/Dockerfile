# Build from the repo root so the golden-path telemetry kit can be installed:
#   docker build -f services/availability-api/Dockerfile -t availability-api:local .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY kit/telemetry/python /tmp/golden-telemetry
COPY services/availability-api/requirements.txt .
RUN pip install --no-cache-dir /tmp/golden-telemetry -r requirements.txt \
    && rm -rf /tmp/golden-telemetry

COPY services/availability-api/schema.sql .
COPY services/availability-api/app ./app

RUN useradd --system --uid 10001 app
USER 10001

EXPOSE 8080
# One worker per container: scale with replicas, not workers, so per-process
# state (the circuit breaker) and resource limits stay simple to reason about.
# No access log: at 1,000 req/s it is the biggest log cost and adds nothing that
# the request metrics and traces don't already give us.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
