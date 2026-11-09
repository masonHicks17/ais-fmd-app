-- ===========================================================================
-- Migration 017 -- seven committees. NOT YET RUN. Review the mapping first.
--
-- Treasurer, 2026-11-09: the only committees are Treasury, Marketing,
-- Professional Development, Consulting, Membership, Operations and President.
-- The other budget lines fold into one of them; the old line's name is kept as
-- the transaction's purpose where none was set. Mirrors
-- config/categories.RETIRED_LINES -- change both together:
--
--    8 Meeting Food -> 5 Membership     13 Merch     -> 9 Marketing
--   12 Overhead     -> 2 Treasury       14 Road Trip -> 5 Membership
--   15 Technology   -> 6 Operations     16 Passport  -> 5 Membership
--   18 Formal       -> 5 Membership
--
-- Budgets for a term are ADDED into the receiving committee. Committee rows are
-- deleted last, once nothing references them.
--
-- Undo: restore from the backup taken before running. Not reversible in SQL --
-- once Meeting Food and Membership spending share committee 5, only the
-- purpose column tells them apart.
-- ===========================================================================

BEGIN;

CREATE TEMP TABLE retired (old_id integer PRIMARY KEY, new_id integer, purpose text) ON COMMIT DROP;
INSERT INTO retired VALUES
  (8, 5, 'Meeting Food'), (12, 2, 'Misc.'), (13, 9, 'Merch'), (14, 5, 'Road Trip'),
  (15, 6, 'Technology'), (16, 5, 'ISOM Passport'), (18, 5, 'Formal');

UPDATE public.transactions t
   SET purpose = COALESCE(NULLIF(t.purpose, ''), r.purpose),
       budget_category = r.new_id
  FROM retired r WHERE t.budget_category = r.old_id;

INSERT INTO public.committeebudgets (termid, committeeid, budget_amount)
SELECT b.termid, r.new_id, SUM(b.budget_amount)
  FROM public.committeebudgets b JOIN retired r ON b.committeeid = r.old_id
 GROUP BY b.termid, r.new_id
ON CONFLICT (termid, committeeid)
  DO UPDATE SET budget_amount = public.committeebudgets.budget_amount + EXCLUDED.budget_amount;
DELETE FROM public.committeebudgets WHERE committeeid IN (SELECT old_id FROM retired);

UPDATE public.merchants m SET committee_id = r.new_id FROM retired r WHERE m.committee_id = r.old_id;
UPDATE public.profiles p SET committee_id = r.new_id FROM retired r WHERE p.committee_id = r.old_id;

-- Tables added by later migrations, when present.
DO $$
BEGIN
  IF to_regclass('public.committee_plan_lines') IS NOT NULL THEN
    UPDATE public.committee_plan_lines c SET committee_id = r.new_id FROM retired r WHERE c.committee_id = r.old_id;
  END IF;
  IF to_regclass('public.budget_requests') IS NOT NULL THEN
    UPDATE public.budget_requests c SET committee_id = r.new_id FROM retired r WHERE c.committee_id = r.old_id;
  END IF;
  IF to_regclass('public.reimbursements') IS NOT NULL THEN
    UPDATE public.reimbursements c SET committee_id = r.new_id FROM retired r WHERE c.committee_id = r.old_id;
  END IF;
  IF to_regclass('public.events') IS NOT NULL THEN
    UPDATE public.events c SET committee_id = r.new_id FROM retired r WHERE c.committee_id = r.old_id;
  END IF;
END $$;

DELETE FROM public.committees WHERE "CommitteeID" IN (SELECT old_id FROM retired);

COMMIT;
