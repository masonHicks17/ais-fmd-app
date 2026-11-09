-- ===========================================================================
-- Migration 015 -- dues price change and late cutoff per term. NOT YET RUN.
--
-- Backs the dues payment checks (domain/dues_checks.py) on the Dues page and
-- the "Dues price dates" form on Treasury -> Terms. Mirrors the four columns
-- `sqlite_backend._migrate` adds:
--
--   terms.dues_early_price    price through dues_increase_date (e.g. 50.00)
--   terms.dues_late_price     price after it (e.g. 65.00)
--   terms.dues_increase_date  last day the early price applies, inclusive
--   terms.dues_late_cutoff    payments after this date are flagged for a look
--
-- Additive and nullable: NULL means "no checks for this term", and the app
-- reads all four with `.get()`, so it works before and after this runs.
--
--   Undo:  ALTER TABLE public.terms DROP COLUMN IF EXISTS dues_early_price,
--            DROP COLUMN IF EXISTS dues_late_price,
--            DROP COLUMN IF EXISTS dues_increase_date,
--            DROP COLUMN IF EXISTS dues_late_cutoff;
-- ===========================================================================

BEGIN;

ALTER TABLE public.terms
  ADD COLUMN IF NOT EXISTS dues_early_price   numeric(8, 2),
  ADD COLUMN IF NOT EXISTS dues_late_price    numeric(8, 2),
  ADD COLUMN IF NOT EXISTS dues_increase_date date,
  ADD COLUMN IF NOT EXISTS dues_late_cutoff   date;

COMMIT;
