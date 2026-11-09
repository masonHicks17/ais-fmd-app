"""Module M7 -- dues and membership revenue."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues as dues_domain
from ais_fmd.domain import dues_checks
from ais_fmd.domain import roster as roster_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester_index, ordered_semesters, term_id_for_semester
from ais_fmd.ui import charts, shell, theme

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Dues",
    "The organisation's most predictable income line: who has paid, how much is "
    "outstanding, and whether collection is tracking previous terms.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined", "Add an academic term first.")
    st.stop()

selected = st.selectbox(
    "Semester",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="dues_semester",
)
summary = dues_domain.summarize(bundle.transactions, bundle.terms, selected)

if summary.payment_count == 0:
    shell.empty_state(
        f"No dues payments found in {selected}",
        "Dues are identified by committee assignment or by the standard payment "
        "amounts arriving via Venmo or Zelle.",
    )
    st.stop()

# --- Headline: collected, and who has paid against the roster -------------
#
# One row since 2026-11-08 (it was two sections and eight tiles). Who should
# have paid comes from the roster uploaded on Dues → Roster, matched by name
# against the dues payments in the bank statements -- the same matching the
# Roster tab uses. An "Expected members" slider used to stand in for a missing
# roster; it was a guess with no source and is gone.


def _open_roster() -> None:
    # A callback, because the hub's switcher has already drawn by the time the
    # button's own rerun reaches this page, and its value cannot change then.
    st.session_state["dues_hub"] = "Roster"


identity = auth.current_user()
term_id = term_id_for_semester(bundle.terms, selected)
stored = repo.load_members(term_id) if term_id else pd.DataFrame()
has_roster = stored is not None and not stored.empty

counts = None
if has_roster:
    members = roster_domain.members_from_rows(stored)
    result = roster_domain.reconcile(
        roster_domain.semester_dues_rows(bundle.transactions, bundle.terms, selected), members
    )
    counts = result.summary()

# Each tile in a card, so the collection bar sits inside its tile rather than
# hanging below the row.
collected_column, paid_column, rate_column = st.columns(3, gap="medium")
with collected_column, shell.card("dues-collected", order=1):
    shell.metric(st, "Collected", summary.total_collected)
    if summary.first_payment is not None:
        shell.say(
            f"{summary.first_payment:%b %d} – {summary.last_payment:%b %d, %Y}", caption=True
        )
with paid_column, shell.card("dues-paid", order=1):
    if counts:
        shell.metric(st, "Paid", f"{counts['paid']:,} of {counts['expected']:,}", currency=False)
        shell.say(f"{counts['unpaid']:,} not paid yet", caption=True)
    else:
        shell.metric(st, "Members paid", f"{summary.unique_payers:,}", currency=False)
        shell.say("No roster to compare against", caption=True)
with rate_column, shell.card("dues-rate", order=1):
    if counts:
        shell.metric(st, "Collection rate", f"{counts['rate']:.0f}%", currency=False)
        st.progress(min(counts["rate"] / 100, 1.0))
    else:
        shell.metric(st, "Collection rate", "—", currency=False)
        shell.say(f"Needs the {selected} roster", caption=True)

notes = []
if counts and counts["unmatched_payments"]:
    notes.append(
        f"{counts['unmatched_payments']} payment(s) totalling "
        f"{format_currency(counts['unmatched_amount'])} could not be matched to anyone on the roster."
    )
if notes:
    shell.say(" ".join(notes), caption=True)

if identity.can(auth.Role.TREASURER):
    if has_roster:
        st.button("See who hasn't paid", icon=":material/arrow_forward:", key="dues_open_roster", on_click=_open_roster)
    else:
        st.button("Upload roster", icon=":material/upload:", key="dues_open_roster", on_click=_open_roster)

# A day-by-day "Collection curve" against past terms sat here until 2026-11-08;
# removed as more than a treasurer needs. Home's Dues card keeps a small one.

# --- Payments to check (domain/dues_checks.py) -------------------------------
#
# Each payment against the price for its date: early price after the increase,
# late price before it, two prices added up (paying for someone else), or after
# the late cutoff. Prices and dates are set on Treasury -> Terms.

flagged = dues_checks.check_payments(bundle.transactions, bundle.terms, selected)
prices = dues_checks.prices_for(bundle.terms, selected)
st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
if prices is None or not prices.configured:
    st.markdown("#### Payments to check")
    shell.say(
        f"No price dates set for {selected}. Set the early price, the day it ends and the "
        "late price on Treasury → Terms, and every payment is checked against the price "
        "for its date.",
        caption=True,
    )
else:
    to_check = flagged[flagged["Check"] != dues_checks.OK]
    shell.heading(
        "Payments to check",
        shell.pill(str(len(to_check)), "over" if len(to_check) else "on track"),
    )
    cutoff = f", late after {prices.late_cutoff:%b %d}" if prices.late_cutoff else ""
    shell.say(
        f"${prices.early:,.2f} through {prices.increase_date:%b %d}, then ${prices.late:,.2f}"
        f"{cutoff}. {len(flagged) - len(to_check)} of {len(flagged)} payments match the price "
        "for their date.",
        caption=True,
    )
    if not to_check.empty:
        shell.dataframe(
            to_check.drop(columns=["transaction_id"]),
            column_config={
                "Date": st.column_config.DateColumn(format="MMM D"),
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
                "Detail": st.column_config.TextColumn(width="large"),
            },
        )

# --- Rates and roster --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
rate_column, roster_col = st.columns([1, 2])

with rate_column:
    st.markdown("#### Payment amounts")
    rates = pd.DataFrame(
        [{"Amount": amount, "Payments": count} for amount, count in sorted(summary.rates.items())]
    )
    shell.dataframe(
        rates,
        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
    )
    st.caption(
        "Multiple amounts usually mean an instalment rate alongside the full rate."
    )

with roster_col:
    st.markdown("#### Who has paid")
    roster = dues_domain.payer_roster(bundle.transactions, bundle.terms, selected)
    if roster.empty:
        shell.empty_state(
            "No payer names could be recovered",
            "Names are read from the transfer description, which not every payment carries.",
        )
    else:
        search = st.text_input("Search members", placeholder="name…", key="dues_search")
        if search.strip():
            roster = roster[roster["Payer"].str.contains(search.strip(), case=False, na=False)]
        shell.dataframe(
            roster,
            column_config={
                "Paid": st.column_config.NumberColumn(format="$%.2f"),
                "First payment": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
            height=340,
        )
        st.caption(f"{len(roster):,} member(s). Repeat payments are aggregated per person.")

# --- Across semesters --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Across semesters")

comparison = dues_domain.compare_semesters(bundle.transactions, bundle.terms, semesters)
table_col, chart_col = st.columns([2, 3])
with table_col:
    shell.dataframe(
        comparison,
        column_config={
            "Collected": st.column_config.NumberColumn(format="$%.2f"),
            "Average": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
with chart_col:
    shell.chart(
        charts.ranked_bar(
            comparison.rename(columns={"Collected": "Amount"}),
            label_column="Semester",
            value_column="Amount",
            color=theme.active().income,
        ),
        key="dues_by_semester",
    )
