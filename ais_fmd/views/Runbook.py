"""
Module M16 -- handoff runbook.

Bus-factor insurance. A student treasury committee turns over every year or two,
and the knowledge that usually leaves with the outgoing treasurer is not how the
code works — it is the *operational* knowledge: what order to do things in, what
to do when a bank changes its export, where the secrets live.

Parts of it read live state, so it describes the system as it is rather than as
it was when someone last edited a wiki page.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth, settings
from ais_fmd.data import repositories as repo
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import ordered_semesters
from ais_fmd.ui import shell

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Runbook",
    "How to operate this system, written for whoever inherits it.",
)

bundle = repo.load_bundle()
files = repo.load_uploaded_files()
locked = repo.locked_semesters()
semesters = ordered_semesters(bundle.terms)

# --- Live state --------------------------------------------------------------

st.markdown("#### Where things stand right now")

state = st.columns(4)
state[0].metric("Transactions", f"{len(bundle.transactions):,}")
state[1].metric("Terms defined", f"{len(semesters)}")
state[2].metric("Closed terms", f"{len(locked)}")
uncategorized = (
    int(
        (
            bundle.transactions["budget_category"].isna()
            | bundle.transactions["purpose"].isna()
        ).sum()
    )
    if not bundle.transactions.empty
    else 0
)
state[3].metric("Awaiting review", f"{uncategorized:,}")

if not files.empty:
    latest = files.iloc[0]
    st.caption(
        f"Most recent statement imported: **{latest['file_name']}** "
        f"({latest.get('row_count', 0)} rows) on {latest.get('uploaded_at', 'unknown date')}."
    )
else:
    st.caption("No statements imported yet.")

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Procedures --------------------------------------------------------------

monthly, close, problems, reference = st.tabs(
    ["Monthly routine", "Closing a term", "When something breaks", "Reference"]
)

with monthly:
    st.markdown(
        """
#### The monthly routine

Roughly 20 minutes if nothing is unusual.

**1. Export the statements.** Wells Fargo checking as CSV; Venmo as CSV. Export
the full statement period, not a filtered view — the importer rejects duplicates,
so overlapping ranges are safe and gaps are not.

**2. Import them.** Treasury → Upload statement. The parser validates the file's
shape before reading it and will refuse a file whose layout it does not
recognise, rather than importing nonsense. Read any warning it shows.

**3. Record the statement balances.** Reconciliation → Record a statement period.
Take the opening and closing balance straight off the statement header. This is
the step that catches a missing import, and it is the one most often skipped.

**4. Work the review queue.** Review Queue, largest amounts first. Leave
"Remember this merchant" ticked — each correction teaches the merchant table, so
the queue shrinks every month instead of regenerating at the same size.

**5. Check alerts.** Alerts & Reports → Alerts. Anything critical should be
resolved or explained before you stop.

**6. Attach receipts.** Reimbursements → Receipt coverage, for anything large
that lacks one. Far easier now than at audit time.
"""
    )

with close:
    st.markdown(
        """
#### Closing a term

Do these in order. Locking is last because it makes the period read-only.

1. **Import every statement** covering the term. Check Reconciliation shows no
   activity gaps.
2. **Reconcile every period.** Every statement period should balance. An
   unexplained gap means a transaction is missing, not that the maths is wrong.
3. **Clear the review queue** for that term, or accept what remains knowingly —
   uncategorized spending is excluded from budget-vs-actual, so committee figures
   understate reality while anything is outstanding.
4. **Resolve outstanding reimbursements.** Approved-but-unpaid requests are
   money committed but not yet reflected.
5. **Generate the board pack** (Alerts & Reports → Board pack) and file it.
6. **Lock the term** (Treasury → Terms). After this, transactions dated inside it
   cannot be edited or imported until someone reopens it — enforced in the data
   layer, so no page can bypass it.
"""
    )
    if locked:
        st.success(f"Currently closed: {', '.join(sorted(locked))}.")
    else:
        st.info("No terms are closed yet.")

with problems:
    # Raw string: the literal dollar figures below must reach the page as text.
    # Two bare '$' in one markdown block make Streamlit read everything between
    # them as LaTeX, which silently ate this whole troubleshooting section.
    st.markdown(
        r"""
#### When something breaks

**A statement is rejected on import.**
The bank changed its export layout. The error names which columns it could not
identify. Open the CSV and check the header row against
`ais_fmd/domain/parsers/wells_fargo.py`, which documents both layouts it accepts.
This has happened before: the file that prompted the current parser had
`DESCRIPTION` and `AMOUNT` in the opposite positions from what the old code
assumed, and the old code imported every transaction as \$0.00 without complaint.

**Transactions import but the amounts look wrong.**
Stop and do not categorize them. Check for \$0.00 rows on the Data Quality page —
a cluster of them is the fingerprint of a misread column. Delete the import and
fix the parser before continuing.

**A period will not reconcile.**
The ledger and the bank disagree. In order of likelihood: a statement was never
imported; a statement was imported twice under different filenames; the opening
balance was typed wrong. The Reconciliation page shows activity gaps, which point
at the first case.

**Everything is uncategorized after an import.**
The rules did not fire. Most often this means the description format changed —
the categorizer reads the purchase date out of the description, and if that
pattern moves, the meeting-food rule stops matching.

**The app will not start.**
Check the Python version (3.13 is what it was built against) and that the venv is
activated. `python -m pytest` will localise a real breakage quickly.
"""
    )

with reference:
    st.markdown("#### Where things live")
    st.markdown(
        f"""
| Thing | Where |
| --- | --- |
| Categories, committee IDs, purposes | `ais_fmd/config/categories.py` — the only place they are defined |
| Categorization rules | `ais_fmd/domain/categorize/predicates.py` |
| Statement parsers | `ais_fmd/domain/parsers/` |
| Database schema (production) | `ais_fmd/data/schema_postgres.sql` |
| Tests | `tests/` — run with `python -m pytest` |
| Current mode | **{'Sandbox' if settings.is_sandbox() else 'Production'}** |
| Sandbox database | `{settings.sandbox_db_path()}` |
"""
    )

    st.markdown("#### Secrets")
    st.markdown(
        """
Nothing secret is stored in the repository, and the sandbox needs none.

For production, set as environment variables (never commit them):

- `AIS_FMD_ENV=production` — the *only* value that leaves sandbox mode
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (the secret key; the publishable key is not used)
- `ANTHROPIC_API_KEY` — optional; the Claude key behind the spend review and the
  categorizer's model pass. Put it in its own Anthropic workspace with a spend
  limit and prepaid credit (no auto-reload). Absent = both passes stay off.
- `AIS_FMD_LLM_ENABLED=0` — the off switch for every model call, key or not
- `AIS_FMD_LLM_MONTHLY_CAP_USD` — the app's own monthly cap (default `2.00`),
  counted from the `llm_runs` ledger (migration 013)

If a key is ever exposed, rotate it in the Supabase dashboard first and update
the environment second.
"""
    )

    st.markdown("#### Decisions someone made on purpose")
    st.markdown(
        """
Written down because they look like bugs otherwise:

- **Two purpose mappings contradict the reference table** — "Professional
  Development" books to Consulting, "Food & Drink" to Membership. The original
  behaviour was preserved so historical figures would not shift. Data Quality
  shows how many transactions each affects. Decide, then edit
  `PURPOSE_TO_COMMITTEE`.
- **Duplicate detection asks "do we already have this many of it"**, not "does
  this row exist" — so two genuine identical same-day purchases both import,
  while re-uploading a statement imports nothing.
- **Venmo and Zelle transfers never create merchant rules.** They are
  person-specific; learning them would make one useless rule per member.
- **Uncategorized dues still count** toward collection figures, so the total is
  not understated purely because nobody has worked the review queue.
"""
    )

    if semesters:
        st.markdown("#### Terms on record")
        terms_view = bundle.terms.copy()
        if "locked" in terms_view.columns:
            terms_view["Status"] = terms_view["locked"].fillna(0).astype(int).map(
                {1: "closed", 0: "open"}
            )
        else:
            terms_view["Status"] = "open"
        shell.dataframe(
            terms_view[["TermID", "Semester", "start_date", "end_date", "Status"]].rename(
                columns={"TermID": "Term", "start_date": "Start", "end_date": "End"}
            )
        )
