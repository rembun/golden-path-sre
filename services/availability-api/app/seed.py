"""Apply the schema and seed demo slots. Idempotent: safe to run on every deploy.

Usage: python -m app.seed   (reads DATABASE_URL; SEED_VENUES and SEED_DAYS optional)
"""
import os
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

SEED_SQL = """
INSERT INTO slots (venue_id, starts_at, ends_at)
SELECT v.venue_id, t, t + interval '30 minutes'
FROM (
    SELECT format('venue-%%s', lpad(i::text, 3, '0')) AS venue_id
    FROM generate_series(1, %(venues)s) AS i
) AS v
CROSS JOIN generate_series(
    date_trunc('day', now()),
    date_trunc('day', now()) + make_interval(days => %(days)s),
    interval '1 day'
) AS d
CROSS JOIN LATERAL generate_series(
    d + interval '9 hours', d + interval '20 hours 30 minutes', interval '30 minutes'
) AS t
ON CONFLICT (venue_id, starts_at) DO NOTHING
"""


def main() -> None:
    venues = int(os.environ.get("SEED_VENUES", "50"))
    days = int(os.environ.get("SEED_DAYS", "60"))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        conn.execute(SCHEMA.read_text())
        cur = conn.execute(SEED_SQL, {"venues": venues, "days": days})
        print(f"schema applied; {cur.rowcount} new slots for {venues} venues x {days + 1} days")


if __name__ == "__main__":
    main()
