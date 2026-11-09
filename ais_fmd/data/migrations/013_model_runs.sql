-- ===========================================================================
-- Migration 013 -- model spending ledger and spend reviews. NOT YET RUN.
--
-- Backs the Haiku spend review on the Review Queue and the metered categorizer
-- pass on the Treasury import (domain/categorize/metering.py). Mirrors the two
-- tables in sqlite_backend.py's SCHEMA.
--
--   llm_runs       one row per model run, written BEFORE its first call and
--                  finished after its last. The monthly cap is counted from
--                  here; a run that never finished counts at its full
--                  reservation. The partial unique index allows only one
--                  running run at a time -- that is the app's run lock.
--   spend_reviews  one verdict per charge per committee it was booked to. The
--                  unique key is what stops a charge being sent twice; a
--                  charge re-booked to another committee is eligible once more.
--
-- Until this runs the app cannot read the ledger, so it refuses every model
-- call -- the feature is off, never unmetered.
--
-- Runs after 012. Like 010: RLS on, no policies, nothing granted to anon or
-- authenticated. The app reads and writes with the secret key.
--
--   Undo:  DROP TABLE IF EXISTS public.spend_reviews;
--          DROP TABLE IF EXISTS public.llm_runs;
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.llm_runs (
    run_id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    purpose            text NOT NULL CHECK (purpose IN ('categorize', 'spend_review')),
    model              text NOT NULL,
    status             text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'done', 'failed', 'abandoned')),
    started_by         text NOT NULL,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    rows_planned       integer NOT NULL DEFAULT 0 CHECK (rows_planned >= 0),
    calls_planned      integer NOT NULL DEFAULT 0 CHECK (calls_planned >= 0),
    calls_made         integer NOT NULL DEFAULT 0 CHECK (calls_made >= 0),
    input_tokens       integer NOT NULL DEFAULT 0,
    output_tokens      integer NOT NULL DEFAULT 0,
    cache_read_tokens  integer NOT NULL DEFAULT 0,
    cache_write_tokens integer NOT NULL DEFAULT 0,
    reserved_usd       numeric(10, 6) NOT NULL CHECK (reserved_usd >= 0),
    cost_usd           numeric(10, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    error              text
);

-- One run at a time. Every running row has the same status value, so a second
-- one violates the index and the insert is refused.
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_runs_one_running
    ON public.llm_runs (status) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS ix_llm_runs_started_at
    ON public.llm_runs (started_at);

CREATE TABLE IF NOT EXISTS public.spend_reviews (
    review_id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id         integer NOT NULL REFERENCES public.transactions (transactionid),
    booked_committee_id    integer NOT NULL,
    verdict                text NOT NULL CHECK (verdict IN ('fits', 'question', 'other_committee')),
    suggested_committee_id integer,
    confidence             numeric(4, 3) CHECK (confidence BETWEEN 0 AND 1),
    reason                 text CHECK (char_length(reason) <= 300),
    message_to_vp          text CHECK (char_length(message_to_vp) <= 500),
    -- 'cleared' for fits; 'open' until the treasurer resolves or dismisses a flag.
    status                 text NOT NULL
        CHECK (status IN ('cleared', 'open', 'resolved', 'dismissed')),
    run_id                 integer REFERENCES public.llm_runs (run_id),
    model                  text,
    prompt_version         text,
    reviewed_at            timestamptz DEFAULT now(),
    resolved_at            timestamptz,
    resolved_by            text,
    resolution_note        text,
    UNIQUE (transaction_id, booked_committee_id)
);

CREATE INDEX IF NOT EXISTS ix_spend_reviews_status
    ON public.spend_reviews (status);

ALTER TABLE public.llm_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.llm_runs FROM anon, authenticated;
GRANT ALL ON public.llm_runs TO service_role;

ALTER TABLE public.spend_reviews ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.spend_reviews FROM anon, authenticated;
GRANT ALL ON public.spend_reviews TO service_role;

COMMIT;
