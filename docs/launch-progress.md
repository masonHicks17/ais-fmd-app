# Launch progress: moving the app onto live Supabase

**LAUNCHED 2026-10-19.** The new app runs on Streamlit Community Cloud (treasuryufais workspace, branch `sandbox/mvp-rebuild`), reading the live Supabase project with the secret key. Every migration, including the `008` lock-down, is applied to live. All existing data is verified identical to the 2026-10-15 backup.

## After launch: what's left

1. **Upload the bank statements since 2026-01-22** in the new app. Until then, Home opens on Spring 2026, the latest term with transactions. Re-imported rows add nothing.
2. **Google sign-in** (optional): create a Google OAuth client with redirect `https://ais-fmd-app-bvyhbbycmpeuwbxwomb7au.streamlit.app/oauth2callback`, fix the `[auth]` secrets (the Cloud log shows "misconfigured origin for `redirect_uri`"), and add the `profiles` table (migration 002's part plus 011) with a treasurer row. Rehearse on the test project first. Keep the password as a fallback.
3. **Block budget edits in locked terms.** The lock covers transactions only; a side task is proposed.
4. **Retire the two old Streamlit apps** (`ais-fmd`, `ais-fmd-app` on `main`). Their public key is disabled on live, so they can't read data.
5. **Supabase account items** flagged by the advisor, all unrelated to the app: upgrade Postgres (15.8.1.085 has security patches; take a backup first, since it restarts the database), and the email-OTP expiry and leaked-password-protection settings for Supabase Auth, which the app doesn't use.
6. **Housekeeping:**
   - delete `C:\Users\durpy\fmd-cloud-secrets.toml`
   - delete the duplicate `full.dump` in the Drive backup folder
   - move the handbook's plain-text passwords into a password manager and rotate them
   - delete the test project and `C:\Users\durpy\fmd-test-run\` when no longer needed
7. **The Cloud app runs Python 3.14.** The Pipfile asks for 3.12, and pipenv only warns. To match CI (3.11–3.13), redeploy with Python 3.12, or drop `[requires]` from the Pipfile.
8. **AI features are off in production.** Categorization itself works without AI (card roster, rules and scoring: 124 of 200 rows in the test upload).
   - **Model help for leftover rows:** add `OPENAI_API_KEY = "sk-..."` as a **top-level** Cloud secret. The local `.streamlit/secrets.toml` has an `[openai]` section, which the code does not read. The model defaults to `gpt-4.1-mini` (`AIS_FMD_LLM_MODEL`) and it costs money.
   - **Spend review / "AI overview"** (`llm.review_spend`, PR #1): it flags booked transactions that don't fit their committee and never changes a category. It's in the code, but no page runs it. A side task is proposed: a treasurer-only "Check this term's spending" button, flag-only, with cached results.
   - **Assistant:** it runs on its offline keyword router. The model-backed version (`assistant_model.py`) isn't in the codebase.

## Running it

- **Live app:** https://ais-fmd-app-bvyhbbycmpeuwbxwomb7au.streamlit.app (treasury password from the Cloud secrets). After inactivity Streamlit Cloud puts it to sleep; the first visit wakes it in about a minute.
- **On this laptop (demo data, no live access):** `.venv/Scripts/python -m streamlit run app.py`. Sandbox mode is the default, with no password and generated data.
- **On this laptop against the test project:** `C:\Users\durpy\fmd-test-run\run-test-app.ps1`. It needs a venv with `requirements-production.txt`; its path inside the script points at a temporary folder, so recreate the venv if it's gone.

## Projects

| | Supabase project | ID |
|---|---|---|
| Live, don't touch without the treasurer's OK | FMD Database | `tcdzceodgsftvfdczjpt` |
| Test, safe to break or delete | FMD Test (rehearsal - safe to delete) | `zmexnfoyembdbuowvctb` |

Both are in the "UF Association of Information Systems" org, us-east-2, Free plan (no automatic backups).

## Ground rules from the treasurer

- **Categorizations already in Supabase are the record and are never overwritten.** Categorization work is for new imports only. The Review Queue enforces this: only rows with no committee, and by default only rows imported by this app (`source_file` set). Regression test: `test_review_queue_never_offers_a_row_that_already_has_a_committee`.
- **No change to live without the treasurer's explicit OK**, given for that specific step.

## Done

1. **Backup of live, 2026-10-15 at 2:57 PM.** Stored in Treasury Drive → *FMD Database Backups* → *2026-10-15 (before migrations)*, with a README. Figures: 2,802 transactions, net $18,050.43, income $130,526.17, expenses −$112,475.74, dated 2022-05-09 to 2026-01-22. Live was re-checked 2026-10-19 and is unchanged (same totals and category fingerprint).
2. **Restore proven:** the backup was loaded into the test project, and every count and total matched.
3. **Migrations rehearsed on test**, in this order: `001 → 003 → 005 → 006 → 007 → 008 → 009 → 010 → 012`. Skip `002` and `011` (deferred; they're for VP sign-in, which needs the `profiles` table) and `004` (no longer needed). Every one ran cleanly, and the old rows' categories are unchanged after each.
4. **natural_key backfill dry-run:** 0 collisions across 2,802 rows.
5. **Security lock-down (008, plus the app reading with the secret key).** Before it, the publishable key could read every transaction on live. After 001–007 it could also have called the write RPCs, and `members` would have been readable. After 008/010 on test, the publishable key gets 401 everywhere, and Supabase's security advisor shows only the intended "RLS enabled, no policy" note.
6. **Budget-save bug fixed (009).** On the live schema every budget save failed (42P10) for lack of a unique `(termid, committeeid)` constraint.
7. **The real app was run against the test project with only the secret key** (separate venv; run folder `C:\Users\durpy\fmd-test-run\`), on the team's combined code:
   - All **11 pages render** with no exceptions.
   - **18/18 core save paths pass:** term, upload, re-upload adds 0, edit, budget save and re-save, dues rates, lock, closed-term import/edit blocked, unlock, roster, alias, statement balance, merchant rule, label, audit trail.
   - **5/5 new-feature checks pass:** sponsorship goal, sponsor name, "This isn't ours" flag, duplicate open flag refused, flag resolved.
   - All rehearsal rows were removed afterwards.
8. **Fixes found by the rehearsal**, all merged or in this branch:
   - Review Queue could overwrite stored committees (fixed, test added)
   - sign-in ignores stray spaces around a pasted password
   - the Streamlit 1.66 form-submission change in the auth tests
9. **Local test-run gotcha:** `streamlit run` also loads the `.streamlit/secrets.toml` *next to app.py* (the repo's real one, pointing at live) and lets it override the working folder's. Test runs must pass `--secrets.files`. `run-test-app.ps1` does, and refuses to start unless the file points at the test project.

## Test-copy state (not pristine)

The treasurer used the test app on 2026-10-16 to 10-19. It now holds:
- a 200-row upload (`Checking (1).csv`, 2026-07-20 to 2026-10-14)
- the Fall 2026 term with 14 budgets
- **edited Spring 2026 budgets.** These look accidental: before Fall 2026 existed, Spring 2026 was the "current" term the budget editor opened on.

None of this goes to live except the Fall 2026 term below. To rehearse again from clean, restore the backup into the test project.

## Fall 2026 term for live (decided 2026-10-19)

Copied from the treasurer's test-copy setup. Dues rates are treasury's confirmed $35 / $50 / $65 (`domain/dues.CONFIRMED_DUES_RATES`).

```sql
INSERT INTO public.terms ("TermID", "Semester", start_date, end_date, dues_rates, dues_rates_verified)
VALUES ('FA26', 'Fall 2026', '2026-08-01', '2026-12-12', '35.00,50.00,65.00', true);

INSERT INTO public.committeebudgets (termid, committeeid, budget_amount) VALUES
  ('FA26', 2, 300), ('FA26', 4, 500), ('FA26', 5, 4000), ('FA26', 6, 100),
  ('FA26', 7, 3500), ('FA26', 8, 3000), ('FA26', 9, 300), ('FA26', 10, 100),
  ('FA26', 12, 0), ('FA26', 13, 0), ('FA26', 14, 0), ('FA26', 15, 0),
  ('FA26', 16, 1300), ('FA26', 18, 0);
-- 14 rows, $13,100: Treasury 300, President 500, Membership 4,000, Operations 100,
-- Consulting 3,500, Meeting Food 3,000, Marketing 300, Prof. Dev. 100, Passport 1,300.
```

May 6 to Jul 31, 2026 falls in no term, and so do 25 transactions from May 6 to Aug 17, 2025.

## Live progress, 2026-10-19

- Treasurer chose not to take a fresh backup. Live was verified identical to the 2026-10-15 Drive backup: all 2,802 transactions (category fingerprint `1f4382fd…`) and all 125 budgets, row for row.
- Edge logs show no API traffic to live in the previous 24 hours. The legacy anon key is already disabled on live.
- Decision: **hold `008_lock_down` until the new app is deployed and signed into**, so nothing that works today breaks before the switch.
- **Applied to live, with the treasurer's permission:** `001, 003, 005, 006, 007, 009, 010, 012`; natural_key backfill (2,802 keys, all distinct); the Fall 2026 term and 14 budgets ($13,100; dues 35/50/65); Fall 2022 through Spring 2026 locked (`locked_by = 'treasurer (launch 2026-10-19)'`).
- **Verified after every step and at the end:** 2,802 transactions; net $18,050.43, income $130,526.17, expenses −$112,475.74; content fingerprint `7e7e67a8…`, category fingerprint `1f4382fd…`, and the original 125 budgets' fingerprint `7a24f0f2…` all identical to the 2026-10-15 backup.
- **PR #6 merged** (Pipfile, Review Queue protection). The Cloud app was rebooted, installed from the Pipfile (supabase included), and the treasurer signed in. Live's edge logs show the app reading every table through `python-httpx` with HTTP 200.
- **`008_lock_down` applied to live** after that. The publishable key now gets HTTP 401 on all 11 tables and the save RPC. Every public table has RLS with no policies. The app kept reading successfully after 008, and the data fingerprints still match the backup.
- **Known gap:** a locked term blocks transaction imports and edits, but **not budget edits**. The budget editor doesn't check the lock yet.

## Next: live switch-over (each step needs the treasurer's OK)

1. Take a fresh backup into a new dated Drive subfolder, and record its totals.
2. Apply `001, 003, 005, 006, 007, 008, 009, 010, 012` to live, checking the totals and category fingerprint after.
3. Run `scripts/prod/backfill_natural_keys.py` with the live secret key: dry run, then `--apply`.
4. Add the Fall 2026 term and budgets (SQL above).
5. Lock the finished terms, Fall 2022 through Spring 2026, so their transactions and budgets can't be changed by accident. Approved by the treasurer 2026-10-19.
6. Re-check totals against the fresh backup. They must match exactly.
7. Deploy to Streamlit Community Cloud with secrets `AIS_FMD_ENV = "production"`, `[supabase] url` + `secret_key`, and `[treasury] password`. The treasurer enters these in the Cloud settings. **The original app stops working once 008 is on live**, so switch over in one go, then sign in once in a normal browser.
   - **Dependencies on Cloud:** Cloud uses the first dependency file it finds, in the order `uv.lock` → `Pipfile` → `environment.yml` → `requirements.txt` → `pyproject.toml` (Streamlit docs, app-dependencies page). `requirements.txt` deliberately omits `supabase`, because its absence is a sandbox safety layer. A root `Pipfile` (added 2026-10-19, approved by the treasurer) lists `requirements-production.txt`'s packages, so Cloud installs the production set while local installs stay on `requirements.txt`. `tests/test_deploy_dependencies.py` keeps the two in step. In the Cloud app settings, pick Python 3.12.
8. Upload the bank statements since 2026-01-22 in the new app. Overlaps are safe, because re-imported rows add 0.

## Open items

- The VP Treasury Handbook stores plain-text passwords for PayPal, Venmo, GitHub, PythonAnywhere, Supabase and the Apple ID. Move them to a password manager and rotate them.
- Delete the duplicate `full.dump` in the Drive backup folder.
- `C:\Users\durpy\fmd-test-run\.streamlit\secrets.toml` holds the test project's secret key. Delete the folder when the test project is deleted.
- Run the test app: `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\durpy\fmd-test-run\run-test-app.ps1` (port 8503). The password is in that folder's secrets file.
