-- Availability API schema.
-- Kept deliberately small: slots and holds only. Bookings are out of scope (see DECISIONS.md).

CREATE TABLE IF NOT EXISTS slots (
    id         BIGSERIAL PRIMARY KEY,
    venue_id   TEXT        NOT NULL,
    starts_at  TIMESTAMPTZ NOT NULL,
    ends_at    TIMESTAMPTZ NOT NULL,
    CONSTRAINT slots_venue_start_uniq UNIQUE (venue_id, starts_at),
    CONSTRAINT slots_end_after_start CHECK (ends_at > starts_at)
);
-- The unique constraint's index also serves the availability query
-- (venue_id = ? AND starts_at in [day, day+1)).

CREATE TABLE IF NOT EXISTS holds (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    slot_id    BIGINT      NOT NULL REFERENCES slots (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
-- A hold is "active" while expires_at > now(). No background job is needed to
-- release holds; expired rows are simply ignored (and can be purged later).
CREATE INDEX IF NOT EXISTS holds_slot_expires_idx ON holds (slot_id, expires_at);
