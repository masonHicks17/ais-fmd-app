-- ===========================================================================
-- Migration 016 -- events from the club calendar, and which spending paid for
-- which event. NOT YET RUN. The Supabase backend does not read these yet
-- (backend.py stubs); this file is the shape to build it against.
--
-- Mirrors the SQLite tables in sqlite_backend.SCHEMA. See domain/events.py.
--
--   Undo:  DROP TABLE IF EXISTS public.event_links; DROP TABLE IF EXISTS public.events;
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.events (
    event_id          bigserial PRIMARY KEY,
    external_key      text NOT NULL UNIQUE,
    term_id           text REFERENCES public.terms ("TermID"),
    event_date        date NOT NULL,
    title             text NOT NULL,
    start_time        text,
    end_time          text,
    location          text,
    owner             text,
    audience          text,
    status            text,
    committee_id      integer,
    attendance        integer,
    attendance_source text,
    description       text,
    source            text,
    calendar_id       text,
    synced_by         text,
    synced_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_term ON public.events (term_id, event_date);

CREATE TABLE IF NOT EXISTS public.event_links (
    transaction_id bigint PRIMARY KEY,
    event_id       bigint NOT NULL REFERENCES public.events (event_id) ON DELETE CASCADE,
    linked_by      text,
    linked_at      timestamptz DEFAULT now()
);

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_links ENABLE ROW LEVEL SECURITY;

COMMIT;
