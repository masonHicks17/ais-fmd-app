"""
Module M3 -- categorization review queue.

Replaces "categorize everything, then eyeball a 200-row grid". Uncategorized
transactions are worked one at a time, highest-value first, with the reason the
categorizer could not resolve them shown alongside.

Every decision is logged as a label (M19), with what the categorizer proposed
beside what the treasurer chose, so spot-checks and evaluation can measure how
often the categorizer is right. Decisions no longer become merchant rules:
treasury ruled merchant memory out of categorization on 2026-09-23 (see
`pipeline.py`), so the way to shrink this queue is an accurate card roster.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    committee_dropdown_options,
    committee_name,
    parse_committee_label,
    purpose_dropdown_options,
    purpose_to_committee,
)
from ais_fmd.data import repositories as repo
from ais_fmd.data.backend import TransactionChange
from ais_fmd.domain import dues
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.scoring import CardRegistry, current_era
from ais_fmd.ui import charts, shell
from ais_fmd.ui import flags as flag_ui
from ais_fmd.ui import spend_review as spend_review_ui

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Review Queue",
    "Work through what the categorizer could not resolve. A purchase on a card "
    "in the card roster only reaches this queue when the rest of its evidence "
    "disagrees with the card.",
)

transactions = repo.load_transactions()

if transactions.empty:
    shell.empty_state("No transactions yet", "Upload a statement from the Treasury page.")
    st.stop()

# Charges a VP has disputed ("This isn't ours"). One call; the panel lives in
# ui/flags.py so this shared page stays untouched otherwise.
flag_ui.treasurer_panel(transactions, identity.email)

# The Haiku spend review: a metered run the treasurer starts, and its flags.
spend_review_ui.treasurer_panel(transactions, identity)

# Stored categorizations are the record (treasury, 2026-10-02, restated
# 2026-10-16). Much of the history carried over from the original app has a
# committee but no purpose, and this queue used to list those rows too -- with
# the committee box pre-set to the categorizer's suggestion, so one "Apply
# decisions" would have replaced a stored committee. The queue now works only
# rows with NO committee. Correcting a stored committee is a deliberate,
# one-row edit on the Transactions page.
#
# History from the original app has no `source_file` (the column arrived with
# migration 001); anything imported by this app has one. At this stage only new
# imports are worked here -- the old blanks stay as Supabase has them unless the
# treasurer chooses to include them.
needs_committee = transactions["budget_category"].isna()
if "source_file" in transactions.columns:
    from_original_app = transactions["source_file"].isna()
else:
    from_original_app = pd.Series(False, index=transactions.index)

history_blanks = int((needs_committee & from_original_app).sum())
include_history = st.toggle(
    "Include blank rows from before this app",
    value=False,
    key="queue_include_history",
    disabled=not history_blanks,
    help=(
        f"{history_blanks:,} transaction(s) carried over from the original app have "
        "no committee. They are left exactly as stored unless you choose to work "
        "them here. Rows that already have a committee are never shown."
    ),
)

pending = transactions[needs_committee & (include_history | ~from_original_app)].copy()

# Headline coverage is a property of the whole ledger, not of whatever period is
# being worked. Measuring it after the period filter reported "784 categorized,
# 87.9% coverage" when the real figures were 566 and 63.5% -- the filtered-out
# rows were being counted as resolved.
total_pending = int(needs_committee.sum())
total_resolved = len(transactions) - total_pending

# --- Period ------------------------------------------------------------------
# The queue spans every statement ever imported, but review is almost always
# done against the recent past -- older periods are usually settled, and in a
# closed term they cannot be edited at all (M10).

PERIOD_CHOICES = {
    "Past year": 365,
    "Past 6 months": 182,
    "Past 90 days": 90,
    "Everything": None,
}
period_choice = st.selectbox(
    "Period",
    list(PERIOD_CHOICES),
    key="queue_period",
    help="Limits the queue to recently-dated transactions.",
)
window_days = PERIOD_CHOICES[period_choice]

if window_days is not None and not pending.empty:
    dated = pd.to_datetime(pending["transaction_date"], errors="coerce")
    # Anchor on the newest transaction rather than today: a statement imported
    # after a gap would otherwise show an empty queue and read as "all clear".
    latest = dated.max()
    if pd.notna(latest):
        cutoff = latest - pd.Timedelta(days=window_days)
        before = len(pending)
        pending = pending[dated >= cutoff]
        hidden = before - len(pending)
        if hidden:
            st.caption(
                f"Showing {len(pending):,} of {before:,} unresolved rows — "
                f"{hidden:,} older than {period_choice.lower()} are hidden."
            )

if pending.empty:
    shell.empty_state(
        "Nothing unresolved in this period",
        "Widen the period above to see older transactions.",
    )
    st.stop()

# --- Overview ----------------------------------------------------------------

overview = st.columns(4)
overview[0].metric(
    "Awaiting review",
    f"{len(pending):,}",
    help=(
        f"In the selected period. {total_pending:,} unresolved across the whole ledger."
    ),
)
overview[1].metric("Already categorized", f"{total_resolved:,}")
overview[2].metric(
    "Coverage",
    f"{(total_resolved / len(transactions) * 100) if len(transactions) else 0:.1f}%",
    help="Across every imported statement, not just the selected period.",
)
overview[3].metric(
    "Cards on the roster",
    f"{sum(1 for _card, a in CardRegistry().items() if a.verified and a.era == current_era()):,}",
    help="Confirmed cards for the current officer cohort, from config/card_roster.json.",
)

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Suggestions -------------------------------------------------------------
# Re-run the pipeline over the pending rows so the queue shows what the current
# card roster and rules would do now, which may differ from when they were
# first imported. Rules only: there is no gate here, so this page -- which
# reruns on every click -- never calls the model.

records = pending.to_dict("records")
run = categorize_records(records, dues=dues.schedule_from_terms(repo.load_terms()))

# A row the confidence gate held back has no classification, but it does have a
# scored proposal — the committee the evidence favoured, how sure it was, and
# what contested it. Falling back to that is the difference between the queue
# saying "I could not resolve this" and "probably Meeting Food, 62%, but
# Membership is close" — the second is what a treasurer can actually act on.
held = run.held_for_review

committees: list[int | None] = []
purposes: list[str | None] = []
rules: list[str] = []
sources: list[str] = []
confidences: list[float] = []
contested: list[bool] = []

for position, item in enumerate(run.classifications):
    proposal = held.get(position)
    if item.is_assigned or proposal is None:
        committees.append(item.committee_id)
        purposes.append(item.purpose)
        rules.append(item.rule)
        sources.append(item.source)
        confidences.append(item.confidence)
        contested.append(False)
        continue

    suggestion = proposal.as_classification()
    committees.append(suggestion.committee_id)
    purposes.append(suggestion.purpose)
    rules.append(proposal.explain())
    sources.append("proposed")
    confidences.append(proposal.confidence)
    contested.append(bool(proposal.conflicting()))

pending = pending.reset_index(drop=True)
pending["suggested_committee"] = committees
pending["suggested_purpose"] = purposes
pending["match_rule"] = rules
pending["match_source"] = sources
pending["confidence"] = confidences
pending["contested"] = contested
pending["magnitude"] = pending["amount"].abs()

resolvable = int((pending["match_source"].isin({"rule", "scored", "llm"})).sum())
proposed = int((pending["match_source"] == "proposed").sum())

if resolvable:
    st.info(
        f"The current card roster and rules can now resolve **{resolvable}** of these "
        f"**{len(pending)}** without any model call."
    )
if proposed:
    st.warning(
        f"**{proposed}** were scored but held back as too uncertain to book "
        f"automatically. Each shows the committee the evidence favoured, its "
        f"confidence, and what contested it — confirm or correct below."
    )

# --- Queue -------------------------------------------------------------------

# The held-back proposals are the highest-value rows in the queue -- they are
# the ones carrying reasoning a treasurer can accept or overturn in seconds.
# Without this filter they were unreachable: confidence-descending buries them
# under every high-confidence row, and there are far more rows than any batch
# size can show.
FILTER_ALL = "Everything"
FILTER_PROPOSED = "Flagged: scored but uncertain"
FILTER_RESOLVABLE = "Resolvable now"
FILTER_NO_SIGNAL = "No signal at all"

filter_choice = st.radio(
    "Show",
    [FILTER_ALL, FILTER_PROPOSED, FILTER_RESOLVABLE, FILTER_NO_SIGNAL],
    horizontal=True,
    key="queue_filter",
    help=(
        "Flagged rows were scored but fell below the auto-apply threshold — "
        "each carries a proposed committee and the reason it is uncertain."
    ),
)

if filter_choice == FILTER_PROPOSED:
    pending = pending[pending["match_source"] == "proposed"]
elif filter_choice == FILTER_RESOLVABLE:
    pending = pending[pending["match_source"].isin({"rule", "scored", "llm"})]
elif filter_choice == FILTER_NO_SIGNAL:
    pending = pending[pending["match_source"] == "none"]

if pending.empty:
    shell.empty_state("Nothing in this view", "Choose a different filter above.")
    st.stop()

sort_choice = st.radio(
    "Work order",
    ["Largest amount first", "Most confident suggestion first", "Oldest first"],
    horizontal=True,
    key="queue_sort",
)

if sort_choice == "Largest amount first":
    pending = pending.sort_values("magnitude", ascending=False)
elif sort_choice == "Most confident suggestion first":
    pending = pending.sort_values(["confidence", "magnitude"], ascending=[False, False])
else:
    pending = pending.sort_values("transaction_date")

pending = pending.reset_index(drop=True)

batch_size = st.slider("How many to show", 5, 50, 12, step=1, key="queue_batch")
batch = pending.head(batch_size)

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

with st.form("review_queue_form"):
    decisions: list[dict] = []

    for position, row in batch.iterrows():
        transaction_id = int(row["transactionid"])
        suggestion = row["suggested_committee"]
        suggested_label = (
            f"{int(suggestion)} - {committee_name(suggestion)}" if pd.notna(suggestion) else ""
        )

        detail_column, choice_column = st.columns([3, 2])

        with detail_column:
            date_text = pd.to_datetime(row["transaction_date"]).strftime("%Y-%m-%d")
            amount_text = f"${abs(row['amount']):,.2f}"
            direction = "in" if row["amount"] > 0 else "out"
            # pd.isna guard: a NaN account is a float and is truthy, so `or` alone
            # would print "nan" rather than falling back to the dash.
            account_text = "—" if pd.isna(row["account"]) else str(row["account"])
            st.markdown(
                shell.money_safe(
                    f"**{amount_text}** {direction} &nbsp;·&nbsp; {date_text} "
                    f"&nbsp;·&nbsp; {account_text}"
                ),
                unsafe_allow_html=True,
            )
            st.caption(str(row["details"])[:220])
            if row["match_source"] == "proposed":
                # Held back by the confidence gate. Lead with the caveat so the
                # figure is never mistaken for a settled answer.
                badge = "contested" if row["contested"] else "weak evidence"
                confidence_text = f"{row['confidence']:.0%} — {badge}"
                reasoning = str(row["match_rule"])
                st.markdown(
                    shell.money_safe(
                        f"{shell.pill(confidence_text, 'approaching')} &nbsp; {reasoning}"
                    ),
                    unsafe_allow_html=True,
                )
            elif row["match_rule"]:
                st.caption(
                    f"Suggestion: {row['match_rule']} "
                    f"(confidence {row['confidence']:.0%}, via {row['match_source']})"
                )
            else:
                st.caption("No card on the roster or rule matched this one.")

        with choice_column:
            committee_choice = st.selectbox(
                "Committee",
                committee_dropdown_options(),
                index=(
                    committee_dropdown_options().index(suggested_label)
                    if suggested_label in committee_dropdown_options()
                    else 0
                ),
                key=f"queue_committee_{transaction_id}",
                label_visibility="collapsed",
            )
            purpose_options = purpose_dropdown_options()
            suggested_purpose = row["suggested_purpose"] or ""
            purpose_choice = st.selectbox(
                "Purpose",
                purpose_options,
                index=(
                    purpose_options.index(suggested_purpose)
                    if suggested_purpose in purpose_options
                    else 0
                ),
                key=f"queue_purpose_{transaction_id}",
                label_visibility="collapsed",
            )

        decisions.append(
            {
                "transaction_id": transaction_id,
                "details": row["details"],
                "transaction_date": row["transaction_date"],
                "amount": row["amount"],
                "account": row["account"],
                # What the model thought, captured *before* the human answers.
                # Without this the disagreement is lost, and disagreements are
                # the highest-information events the queue produces (M19).
                "model_committee": (
                    int(suggestion) if pd.notna(suggestion) else None
                ),
                "model_confidence": float(row["confidence"]),
                "model_source": str(row["match_source"]),
                "committee_key": f"queue_committee_{transaction_id}",
                "purpose_key": f"queue_purpose_{transaction_id}",
            }
        )
        st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    submitted = st.form_submit_button("Apply decisions", type="primary", disabled=identity.read_only)

if submitted:
    changes: list[TransactionChange] = []
    labels: list[dict] = []
    era = current_era()

    for decision in decisions:
        committee_id = parse_committee_label(st.session_state.get(decision["committee_key"]))
        purpose = (st.session_state.get(decision["purpose_key"]) or "").strip() or None

        if committee_id is None and purpose:
            committee_id = purpose_to_committee(purpose)
        if committee_id is None and purpose is None:
            continue  # left blank -- stays in the queue

        changes.append(
            TransactionChange(
                transaction_id=decision["transaction_id"],
                purpose=purpose,
                budget_category=committee_id,
            )
        )

        if committee_id is not None:
            labels.append(
                {
                    "source": "review",
                    "source_ref": "review-queue",
                    "era": era,
                    "transaction_id": decision["transaction_id"],
                    "transaction_date": str(decision["transaction_date"]),
                    "amount": float(decision["amount"]),
                    "details": str(decision["details"]),
                    "account": (
                        None if pd.isna(decision["account"]) else str(decision["account"])
                    ),
                    "committee_id": committee_id,
                    "purpose": purpose,
                    "model_committee": decision["model_committee"],
                    "model_confidence": decision["model_confidence"],
                    "model_source": decision["model_source"],
                    "labeled_by": identity.email,
                    # Keyed on the transaction, so re-deciding a row updates
                    # nothing rather than logging the same example twice.
                    "natural_key": f"review:{decision['transaction_id']}",
                }
            )

    if not changes:
        st.info("Nothing selected — the queue is unchanged.")
    else:
        result = repo.update_transactions(changes, identity.email)
        if result.error:
            shell.error_state("Could not apply decisions", result.error)
        else:
            message = f"Categorized {result.updated} transaction(s)."

            # Record the labels regardless of whether the human agreed with the
            # model. Overrides are the most informative rows in the set, so a
            # failure here is surfaced rather than swallowed.
            if labels:
                label_result = repo.record_labels(labels, identity.email)
                if label_result.error:
                    st.warning(
                        f"Decisions saved, but the training log failed: {label_result.error}"
                    )
                else:
                    overrides = sum(
                        1
                        for item in labels
                        if item["model_committee"] is not None
                        and item["model_committee"] != item["committee_id"]
                    )
                    message += f" Logged {label_result.updated} for training"
                    message += f" ({overrides} overrode the suggestion)." if overrides else "."
            st.success(message)
            st.rerun()

# --- Confidence distribution -------------------------------------------------

with st.expander("Suggestion confidence across the queue"):
    shell.chart(
        charts.confidence_histogram(
            [value for value in pending["confidence"].tolist() if value > 0]
        ),
        key="queue_confidence",
    )
    st.caption(
        "Cards on the roster and exact rules score 1.0, heuristic rules 0.85–0.9, "
        "model suggestions carry the model's own confidence. Zero means nothing matched."
    )
