-- ===========================================================================
-- Migration 018 -- planned events for My Plan. NOT YET RUN.
-- Treasurer decision.
--
-- The events a VP plans to hold in a semester: a title, roughly when, what it
-- should cost and who should come (`views/MyPlan.py`). Like 014's plan lines,
-- keyed by the semester's NAME, and only an estimate: it never books, moves or
-- reserves money. `kind` is the past-event idea it was added from ("Tailgate").
--
-- Until this runs, My Plan shows the past-event ideas but says events can't be
-- saved yet. Locked down the way 008 locks every table: RLS on, no policies,
-- only service_role. Authorization stays application-level: the backend refuses
-- a write for any committee but the caller's own.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.planned_events (
    planned_event_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    committee_id        integer NOT NULL,
    semester            text NOT NULL,
    event_date          date,
    title               text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    kind                text,
    estimated_cost      numeric(12, 2) NOT NULL DEFAULT 0 CHECK (estimated_cost >= 0),
    expected_attendance integer CHECK (expected_attendance IS NULL OR expected_attendance >= 0),
    note                text CHECK (note IS NULL OR char_length(note) <= 200),
    updated_by          text,
    updated_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS planned_events_committee_semester
    ON public.planned_events (committee_id, semester);

ALTER TABLE public.planned_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.planned_events FROM anon, authenticated;
GRANT ALL ON public.planned_events TO service_role;

COMMIT;
