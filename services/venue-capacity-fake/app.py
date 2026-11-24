"""Fake venue-capacity service with a switch to misbehave on demand.

GET /venues/{venue_id}/capacity   -> {"venue_id": ..., "accepting_bookings": true}
GET /admin/mode                   -> current behaviour
PUT /admin/mode                   -> change behaviour, e.g. {"mode": "outage"}

Modes:
  normal        5-20 ms responses
  slow          responds after SLOW_MS (default 400 ms, past the caller's timeout)
  error         HTTP 500 for a fraction ERROR_RATE of requests (default all)
  unauthorized  HTTP 401, as if our credentials had been revoked
  outage        HTTP 503 immediately

Overload model: in every mode, when more than MAX_INFLIGHT requests are in flight,
the service answers 503 and each extra in-flight request adds latency. This is what
makes a retry storm realistic: after an outage ends, a flood of retries keeps the
service saturated, so it "struggles to recover" unless callers back off.

The admin endpoint is unauthenticated on purpose: this is a local test double.
"""
import asyncio
import os
import random

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

MODES = {"normal", "slow", "error", "unauthorized", "outage"}
TOKEN = os.environ.get("CAPACITY_TOKEN", "")

state = {
    "mode": os.environ.get("MODE", "normal"),
    "slow_ms": int(os.environ.get("SLOW_MS", "400")),
    "error_rate": float(os.environ.get("ERROR_RATE", "1.0")),
    "max_inflight": int(os.environ.get("MAX_INFLIGHT", "40")),
}
inflight = 0

app = FastAPI(title="venue-capacity-fake")


class ModeUpdate(BaseModel):
    mode: str | None = None
    slow_ms: int | None = None
    error_rate: float | None = None
    max_inflight: int | None = None


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/admin/mode")
async def get_mode():
    return {**state, "inflight": inflight}


@app.put("/admin/mode")
async def set_mode(update: ModeUpdate):
    if update.mode is not None and update.mode not in MODES:
        return JSONResponse(status_code=400, content={"error": f"mode must be one of {sorted(MODES)}"})
    for key, value in update.model_dump(exclude_none=True).items():
        state[key] = value
    return await get_mode()


@app.get("/venues/{venue_id}/capacity")
async def capacity(venue_id: str, authorization: str | None = Header(default=None)):
    global inflight
    inflight += 1
    try:
        mode = state["mode"]
        if inflight > state["max_inflight"]:
            await asyncio.sleep(0.05 + 0.002 * inflight)  # saturated: slow, then shed
            return JSONResponse(status_code=503, content={"error": "overloaded"})
        if mode == "outage":
            return JSONResponse(status_code=503, content={"error": "outage"})
        if mode == "unauthorized" or (TOKEN and authorization != f"Bearer {TOKEN}"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        if mode == "error" and random.random() < state["error_rate"]:
            return JSONResponse(status_code=500, content={"error": "internal"})

        latency_ms = state["slow_ms"] if mode == "slow" else random.uniform(5, 20)
        await asyncio.sleep(latency_ms / 1000)
        return {"venue_id": venue_id, "accepting_bookings": True}
    finally:
        inflight -= 1
