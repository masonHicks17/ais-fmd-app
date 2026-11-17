-- ===========================================================================
-- Migration 019 -- sponsorships expected in a semester, for the Planner.
-- NOT YET RUN. Treasurer decision.
--
-- One row per sponsor: amount, Pledged or Paid, the date it was paid and a
-- note (`views/Planner.py`). Keyed by the semester's NAME, like 018's planned
-- events, so a term can be planned before it is defined. A plan only: it never
-- books money; the payment itself is the bank transaction.
--
-- Until this runs, the Planner still adds typed sponsors into its totals but
-- says they can't be saved here. Locked down the way 008 locks every table:
-- RLS on, no policies, only service_role.
--
-- Undo: DROP TABLE public.planned_sponsorships;
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.planned_sponsorships (
    sponsorship_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    semester       text NOT NULL,
    sponsor        text NOT NULL CHECK (char_length(sponsor) BETWEEN 1 AND 120),
    amount         numeric(12, 2) NOT NULL DEFAULT 0 CHECK (amount >= 0),
    status         text NOT NULL DEFAULT 'Pledged' CHECK (status IN ('Pledged', 'Paid')),
    paid_on        date,
    notes          text CHECK (notes IS NULL OR char_length(notes) <= 300),
    updated_by     text,
    updated_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS planned_sponsorships_semester
    ON public.planned_sponsorships (semester);

ALTER TABLE public.planned_sponsorships ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.planned_sponsorships FROM anon, authenticated;
GRANT ALL ON public.planned_sponsorships TO service_role;

COMMIT;
