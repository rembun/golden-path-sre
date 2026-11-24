"""Database access. Plain SQL: the queries are small and worth reading directly."""
from datetime import date, datetime, time, timedelta, timezone

from psycopg_pool import AsyncConnectionPool


class SlotNotFound(Exception):
    pass


class SlotUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    # Assumption (see DECISIONS.md): "a day" is a UTC day. Venue time zones are
    # a question for the product team.
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def list_open_slots(pool: AsyncConnectionPool, venue_id: str, day: date) -> list[dict]:
    start, end = _day_bounds_utc(day)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT s.id, s.starts_at, s.ends_at
            FROM slots s
            WHERE s.venue_id = %(venue_id)s
              AND s.starts_at >= %(start)s
              AND s.starts_at <  %(end)s
              AND s.starts_at >  now()
              AND NOT EXISTS (
                  SELECT 1 FROM holds h
                  WHERE h.slot_id = s.id AND h.expires_at > now()
              )
            ORDER BY s.starts_at
            """,
            {"venue_id": venue_id, "start": start, "end": end},
        )
        return await cur.fetchall()


async def create_hold(
    pool: AsyncConnectionPool, venue_id: str, slot_id: int, hold_minutes: int
) -> dict:
    """Place a hold on a slot, or raise.

    Concurrency: SELECT ... FOR UPDATE locks the slot row, so two requests for
    the same slot run one after the other. The second one sees the first hold
    and gets a 409 instead of a double hold.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, starts_at <= now() AS in_past FROM slots "
                "WHERE id = %s AND venue_id = %s FOR UPDATE",
                (slot_id, venue_id),
            )
            slot = await cur.fetchone()
            if slot is None:
                raise SlotNotFound()
            if slot["in_past"]:
                raise SlotUnavailable("slot_in_past")

            cur = await conn.execute(
                "SELECT 1 FROM holds WHERE slot_id = %s AND expires_at > now()",
                (slot_id,),
            )
            if await cur.fetchone() is not None:
                raise SlotUnavailable("already_held")

            cur = await conn.execute(
                "INSERT INTO holds (slot_id, expires_at) "
                "VALUES (%s, now() + make_interval(mins => %s)) "
                "RETURNING id, slot_id, expires_at",
                (slot_id, hold_minutes),
            )
            return await cur.fetchone()


async def ping(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute("SELECT 1")
