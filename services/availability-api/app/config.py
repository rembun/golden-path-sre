"""Configuration, read from environment variables only.

Credentials (DATABASE_URL, CAPACITY_TOKEN) have no defaults on purpose: they come
from a Kubernetes Secret in the cluster, or from a git-ignored .env file locally.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    capacity_url: str
    capacity_token: str
    # Must sit well inside the 300 ms SLO threshold, leaving room for the DB query.
    capacity_timeout_s: float = 0.15
    hold_minutes: int = 10
    db_pool_size: int = 10
    breaker_failure_threshold: int = 5
    breaker_reset_s: float = 5.0


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load() -> Settings:
    return Settings(
        database_url=_required("DATABASE_URL"),
        capacity_url=_required("CAPACITY_URL"),
        capacity_token=_required("CAPACITY_TOKEN"),
        capacity_timeout_s=float(os.environ.get("CAPACITY_TIMEOUT_S", "0.15")),
        hold_minutes=int(os.environ.get("HOLD_MINUTES", "10")),
        db_pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        breaker_failure_threshold=int(os.environ.get("BREAKER_FAILURE_THRESHOLD", "5")),
        breaker_reset_s=float(os.environ.get("BREAKER_RESET_S", "5")),
    )
