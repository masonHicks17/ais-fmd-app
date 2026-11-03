# UF AIS Financial Management System — sandbox rebuild

A rebuilt version of the AIS treasury app, running against a **local SQLite
database with fake data**. It is a place to try the changes without any risk to
the real system.

---

## Safety

This is a test environment. Three independent guarantees, layered so that no
single mistake is enough to reach anything real.

**1. It cannot reach your database or an LLM API — the libraries are not installed.**

The sandbox virtualenv deliberately omits `supabase` and `anthropic`. There is
no code path that can contact Supabase or the Claude API because the clients do
not exist on disk. Verify it yourself:

```bash
.venv/Scripts/python -c "import importlib.util as u; print('anthropic:', u.find_spec('anthropic')); print('supabase:', u.find_spec('supabase'))"
```

Both print `None`.

**2. Sandbox mode is the default and fails closed.**

`AIS_FMD_ENV` must equal exactly `production` to leave sandbox mode. Unset,
empty, misspelled, `prod`, `1`, `true` — all resolve to sandbox. In sandbox mode
`assert_external_call_allowed()` raises on any attempted outbound call, and
constructing the Supabase backend raises before it reads a single credential.

**3. It is a separate directory with its own git repository and no remote.**

Nothing here shares a `.git` directory with `ais-fmd-app`. `git remote -v` is
empty, so `git push` has nowhere to push. Your GitHub repository cannot be
affected by anything in this folder.

Additionally: even a stray `ANTHROPIC_API_KEY` in your environment will not cause
spending. `settings.llm_enabled()` returns `False` in sandbox mode regardless of
what keys are present. There is a test asserting exactly that.

All of the above is enforced by `tests/test_sandbox_safety.py`. If any of it
stops being true, those tests fail.

---

## Running it

First-time setup (clone, virtual environment, install) is in
[CONTRIBUTING.md](CONTRIBUTING.md). Once installed:

```bash
# Windows: .venv\Scripts\python   macOS/Linux: .venv/bin/python
.venv/Scripts/python -m streamlit run app.py
```

Then open <http://localhost:8501>. The database seeds itself on first run.

Run the tests:

```bash
.venv/Scripts/python -m pytest
```

**Reset the data** at any time from the sidebar ("Reset & reseed data"), or
delete the `sandbox_data/` folder — it is regenerated on next launch.

---

## What the seed data contains

Four semesters of plausible activity, plus deliberate imperfections so the
diagnostic pages have genuine findings rather than an artificially clean
database:

| Planted problem | Surfaces on |
| --- | --- |
| One statement period out by $124.80 | Reconciliation |
| Four rows with the legacy `Wells` account label | Data Quality |
| Two $0.00 rows (fingerprint of a parse failure) | Data Quality |
| ~14% of transactions uncategorized | Review Queue |
| Two genuine same-day, same-amount purchases | Exercises the dedupe fix |

---

## Structure

```
ais_fmd/
  settings.py              environment detection and the sandbox guard
  auth.py                  roles (replaces the shared treasury password)
  config/categories.py     committees, purposes, accounts — one source of truth
  data/
    backend.py             the interface both backends implement
    sqlite_backend.py      sandbox: local file, real transactions, audit trail
    supabase_backend.py    production: per-session auth, batched writes
    repositories.py        the single caching layer
    schema_postgres.sql    production migration, including the atomic import RPC
    seed.py                fake data generator
  domain/                  business logic — no Streamlit imports below this line
    money.py               Decimal at the boundary
    terms.py               vectorised date → semester
    budgets.py             budget vs actual, burn rate
    dedupe.py              natural keys with occurrence ordinals
    reconcile.py           M1
    quality.py             M13
    assistant.py           M15 — tools, not a keyword router
    categorize/            card roster, predicates, scoring, LLM, pipeline
    parsers/               validated Venmo and Wells Fargo parsers
  ui/                      theme, chart factories, page shell
  views/                   Streamlit pages — layout and wiring only
tests/                     104 tests
```

The rule that keeps it honest: **nothing under `domain/` imports Streamlit.**
That is what makes the business logic testable without a browser.

---

## Where the token savings come from

The original pipeline sent every transaction to GPT-4.1, then overrode most of
the model's answers with Python rules that ran afterwards — paying for answers
it discarded. The order is now inverted:

1. **The card roster** — a purchase on a card confirmed for the current officer
   cohort is that card's committee (`config/card_roster.json`)
2. **Deterministic rules** — free, exact, covers the well-understood cases
3. **Scoring** — weekday, merchant-type and amount evidence, gated on confidence
4. **The model** — only what genuinely remains

Merchant memory used to sit between the rules and scoring. Treasury took it out
on 2026-09-23: too many merchants serve more than one committee for a
merchant's history to predict the next purchase, and it could override the
card. On the real Fall 2026 statement the card roster plus the rules resolve
166 of 171 rows without a model.

---

## Notes on production

`supabase_backend.py` and `schema_postgres.sql` contain the production
implementations of the fixes, but they are **not exercised by the sandbox**.
Before running against real data:

1. Apply `schema_postgres.sql` to a *non-production* Supabase project first.
2. Backfill `natural_key` on existing transactions (see the note at the end of
   the schema file) — deduplication does not work on rows that lack one.
3. Canonicalise the `Wells` → `Wells Fargo` account values.
4. Decide the two disputed purpose mappings flagged on the Data Quality page.
   The original behaviour was preserved deliberately so that historical figures
   did not shift; changing them is a decision, not a bug fix.
5. Only then set `AIS_FMD_ENV=production`.
