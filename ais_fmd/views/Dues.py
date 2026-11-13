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
from ais_fmd.ui import charts, reconcile_tab, shell, theme

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
# have paid comes from the stored roster (synced, or checked on Treasury →
# Reconcile), matched by name
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

# One summary card since 2026-11-13: who has paid (with the rate as a bar),
# then what was collected, who is left and what people paid, side by side.
# It was three tiles plus a "Payment amounts" table at the foot of the page.


def _stat(label: str, value: str, sub: str = "", *, help: str = "", first: bool = False) -> str:
    edge = " first" if first else ""
    small = f"<small>{sub}</small>" if sub else ""
    return f'<div class="ais-stat{edge}"><span>{label}{shell.tip(help)}</span><b>{value}</b>{small}</div>'


rates = sorted(summary.rates.items(), key=lambda item: -item[1])
amounts = " · ".join(f"{count:,} × {format_currency(amount)}" for amount, count in rates[:3])
if len(rates) > 3:
    amounts += f" · {len(rates) - 3} more"
span = (f"{summary.first_payment:%b %d} – {summary.last_payment:%b %d, %Y}"
        if summary.first_payment is not None else "")

with shell.card("dues-summary", order=1):
    if counts:
        rate = counts["rate"]
        shell.html(
            f'<div class="ais-dues-head"><b>{counts["paid"]:,} of {counts["expected"]:,} members paid</b>'
            f'<span>{rate:.0f}%</span></div>'
            f'<div class="ais-dues-bar"><i style="width:{min(rate, 100):.0f}%"></i></div>'
        )
    else:
        shell.html(
            f'<div class="ais-dues-head"><b>{summary.unique_payers:,} members paid</b>'
            f'<span class="muted">No {selected} roster to compare against</span></div>'
        )
    collected_cell, left_cell, amounts_cell = st.columns(3, gap="medium")
    with collected_cell:
        shell.html(_stat("Collected", format_currency(summary.total_collected), span, first=True))
    with left_cell:
        if counts:
            unmatched = (
                f"{counts['unmatched_payments']} payment(s) totalling {format_currency(counts['unmatched_amount'])} "
                "could not be matched to anyone on the roster."
                if counts["unmatched_payments"] else ""
            )
            shell.html(_stat("Not paid yet", f"{counts['unpaid']:,}",
                             f"{counts['unmatched_payments']} payment(s) unmatched" if unmatched else "",
                             help=unmatched))
        else:
            shell.html(_stat("Not paid yet", "—", f"Needs the {selected} roster"))
        if identity.can(auth.Role.TREASURER):
            if has_roster:
                st.button("See who hasn't paid", icon=":material/arrow_forward:", key="dues_open_roster",
                          on_click=_open_roster)
            elif st.button("Upload roster", icon=":material/upload:", key="dues_open_roster",
                           help="Opens Treasury → Reconcile with the membership roster selected."):
                # A plain click, not a callback: st.switch_page is a no-op inside one.
                reconcile_tab.open_treasury(reconcile_tab.TAB_LABEL, reconcile_tab.ROSTER, semester=selected)
                st.switch_page("ais_fmd/views/Treasury.py")
    with amounts_cell:
        shell.html(_stat(
            "Payment amounts", amounts or "—", f"{summary.payment_count:,} payments",
            help="How many payments came in at each amount. More than one amount usually means an "
                 "early and a late price, or an instalment rate alongside the full rate.",
        ))

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
    cutoff = f", late after {prices.late_cutoff:%b %d}" if prices.late_cutoff else ""
    shell.heading(
        "Payments to check",
        shell.pill(str(len(to_check)), "over" if len(to_check) else "on track"),
        help=(
            f"Each payment is checked against the price for its date: ${prices.early:,.2f} through "
            f"{prices.increase_date:%b %d}, then ${prices.late:,.2f}{cutoff}. "
            f"{len(flagged) - len(to_check)} of {len(flagged)} payments match."
        ),
    )
    if not to_check.empty:
        shell.dataframe(
            to_check.drop(columns=["transaction_id"]),
            column_config={
                "Date": st.column_config.DateColumn(format="MMM D"),
                "Amount": st.column_config.NumberColumn(format="dollar"),
                "Detail": st.column_config.TextColumn(width="large"),
            },
        )

# --- Who has paid ---------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
roster = dues_domain.payer_roster(bundle.transactions, bundle.terms, selected)
shell.heading(
    "Who has paid",
    shell.pill(f"{len(roster):,}", "muted") if not roster.empty else "",
    help="Names are read from each payment's transfer description. Repeat payments are added up per person.",
)
if roster.empty:
    shell.empty_state(
        "No payer names could be recovered",
        "Names are read from the transfer description, which not every payment carries.",
    )
else:
    search = st.text_input("Search members", placeholder="name…", key="dues_search",
                           label_visibility="collapsed")
    if search.strip():
        roster = roster[roster["Payer"].str.contains(search.strip(), case=False, na=False)]
    shell.dataframe(
        roster,
        column_config={
            "Paid": st.column_config.NumberColumn(format="dollar"),
            "First payment": st.column_config.DateColumn(format="MMM D, YYYY"),
        },
        height=340,
    )

# --- Across semesters --------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading("Across semesters", help="Dues collected in each term, with the number of payers and the average payment.")

comparison = dues_domain.compare_semesters(bundle.transactions, bundle.terms, semesters)
table_col, chart_col = st.columns([2, 3])
with table_col:
    shell.dataframe(
        comparison,
        column_config={
            "Collected": st.column_config.NumberColumn(format="dollar"),
            "Average": st.column_config.NumberColumn(format="dollar"),
        },
    )
with chart_col:
    shell.chart(
        charts.ranked_bar(
            comparison.rename(columns={"Collected": "Amount"}),
            label_column="Semester",
            value_column="Amount",
            color=theme.active().income,
            sort=False,  # terms in time order, not by amount
        ),
        key="dues_by_semester",
    )
