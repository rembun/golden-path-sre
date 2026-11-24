"""Booking availability API.

GET  /venues/{venue_id}/availability?date=YYYY-MM-DD  -> open slots for that day
POST /venues/{venue_id}/holds  {"slot_id": 123}       -> 10-minute hold on a slot

Run with:  uvicorn --factory app.main:create_app
"""
from contextlib import asynccontextmanager
from datetime import date

import golden_telemetry
import httpx
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from . import config, repository
from .breaker import CircuitBreaker
from .capacity import CapacityClient, CapacityUnavailable

VENUE_ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"


class HoldRequest(BaseModel):
    slot_id: int


def create_app(
    settings: config.Settings | None = None,
    capacity_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = AsyncConnectionPool(
            settings.database_url,
            min_size=2,
            max_size=settings.db_pool_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=10)
        breaker = CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_reset_s)
        capacity = CapacityClient(
            settings.capacity_url,
            settings.capacity_token,
            settings.capacity_timeout_s,
            breaker,
            transport=capacity_transport,
        )
        app.state.pool = pool
        app.state.capacity = capacity
        try:
            yield
        finally:
            await capacity.aclose()
            await pool.close()

    app = FastAPI(title="availability-api", lifespan=lifespan)
    # Onboarding step from the golden path: one line gives traces, metrics and
    # logs that follow the platform telemetry standard.
    golden_telemetry.instrument(app)

    @app.get("/healthz")
    async def healthz():
        # Liveness: the process is up. Deliberately checks no dependencies,
        # so a dependency outage never makes Kubernetes restart healthy pods.
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        # Readiness: we can do useful work, which needs the database.
        # The capacity service is NOT checked here: if it were, a capacity outage
        # would mark every pod unready and turn a partial failure into a total one.
        try:
            await repository.ping(app.state.pool)
        except Exception:
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        return {"status": "ok"}

    @app.get("/venues/{venue_id}/availability")
    async def availability(
        venue_id: str = Path(pattern=VENUE_ID_PATTERN),
        day: date = Query(alias="date"),
    ):
        try:
            capacity = await app.state.capacity.check(venue_id)
        except CapacityUnavailable as exc:
            # Fail closed: we will not show slots we cannot confirm.
            # Retry-After tells well-behaved clients to back off instead of hammering us.
            return JSONResponse(
                status_code=503,
                content={"error": "capacity_unavailable", "reason": exc.reason},
                headers={"Retry-After": str(int(settings.breaker_reset_s))},
            )
        slots = []
        if capacity.accepting_bookings:
            slots = await repository.list_open_slots(app.state.pool, venue_id, day)
        return {
            "venue_id": venue_id,
            "date": day.isoformat(),
            "slots": [
                {
                    "slot_id": s["id"],
                    "starts_at": s["starts_at"].isoformat(),
                    "ends_at": s["ends_at"].isoformat(),
                }
                for s in slots
            ],
        }

    @app.post("/venues/{venue_id}/holds", status_code=201)
    async def create_hold(body: HoldRequest, venue_id: str = Path(pattern=VENUE_ID_PATTERN)):
        try:
            hold = await repository.create_hold(
                app.state.pool, venue_id, body.slot_id, settings.hold_minutes
            )
        except repository.SlotNotFound:
            raise HTTPException(status_code=404, detail="slot_not_found")
        except repository.SlotUnavailable as exc:
            raise HTTPException(status_code=409, detail=exc.reason)
        return {
            "hold_id": str(hold["id"]),
            "slot_id": hold["slot_id"],
            "expires_at": hold["expires_at"].isoformat(),
        }

    return app
