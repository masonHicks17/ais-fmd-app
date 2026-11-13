"""
Term analysis: the Dashboard's charts, as one function.

Home shows this under its "today" cards since 2026-11-08, when Home and
Dashboard were merged; Dashboard.py still renders it as a page of its own for
links and tests. Looks backward -- how a term was built and how it compares
with past terms:

  Income − Expenses = Net   one card, each against the equivalent term
  Committee budgets         % used against % of term elapsed, all committees
  Where the money went      income by source -> spending by committee -> net

2026-11-13: the four KPI tiles (with mini bars for the last five terms) became
one Income − Expenses = Net card. Budget used left it (Committee budgets shows
it per committee), and so did the bars and "Net by week": they repeated what
the deltas and the waterfall already say.

FINDING (comparisons). Deltas used to compare a part-finished term with the
whole of the previous term. They now compare against the equivalent term at
the same day (domain.pace).

Fixes kept from the rebuild:
  F11  expenses use inverse delta colouring
  F12  % spent guards division by zero
  F14  deltas are preformatted currency
"""

from __future__ import annotations

import streamlit as st

from ..config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ..domain import pace
from ..domain import sponsorship as sponsorship_domain
from ..domain.terms import default_semester_index
from . import charts, shell, theme

# Where Home's "All committees" link and budget alerts jump to.
ANCHOR = "term-detail"


def render(bundle, semesters: list[str], *, as_page: bool = False) -> None:
    """The analysis for `bundle`. `as_page` titles it "Dashboard"; otherwise it is a Home section."""
    t = theme.tokens()
    title_column, scope_column, term_column = st.columns([2.4, 1.2, 1], vertical_alignment="bottom")

    with term_column:
        selected_semester = st.selectbox(
            "Semester",
            semesters,
            index=default_semester_index(bundle.transactions, bundle.terms),
            key="dash_semester",
            label_visibility="collapsed",
        )

    committee_options = ["Organization"] + [committee_name(cid) for cid in BUDGETED_COMMITTEE_IDS]
    with scope_column:
        selected_committee = st.selectbox(
            "Scope", committee_options, key="dash_committee", label_visibility="collapsed"
        )
    committee_filter = None if selected_committee == "Organization" else selected_committee
    committee_id = next(
        (cid for cid in BUDGETED_COMMITTEE_IDS if committee_name(cid) == committee_filter), None
    )

    clock = pace.term_clock(bundle.terms, selected_semester)
    prior = pace.equivalent_prior(bundle.terms, selected_semester)

    with title_column:
        if clock is None:
            subtitle = selected_semester
        elif clock.finished:
            subtitle = f"{selected_semester}, whole term" + (f", against {prior}" if prior else "")
        else:
            subtitle = f"{selected_semester} at day {clock.day}" + (f", against {prior} at the same day" if prior else "")
        if as_page:
            shell.page_header("Dashboard", context=subtitle)
        else:
            shell.section_header("This term in detail", subtitle, anchor=ANCHOR)

    if clock is None:
        shell.empty_state(f"{selected_semester} has no dates", "Set them on Treasury → Manage Terms.")
        return

    # Committee scope narrows every figure on the page to that committee's rows.
    transactions = bundle.transactions
    budgets = bundle.budgets
    if committee_id is not None:
        transactions = transactions[transactions["budget_category"] == committee_id]
        budgets = budgets[budgets["committeeid"] == committee_id] if not budgets.empty else budgets

    # --- Income − Expenses = Net ------------------------------------------------

    day = None if clock.finished else clock.day
    history = pace.same_day_history(
        transactions, bundle.terms, [selected_semester] + ([prior] if prior else []), day, budgets
    )
    now = history[selected_semester]
    then = history.get(prior) if prior else None

    def _delta(field: str):
        return None if then is None else now[field] - then[field]

    against = f" Change against {prior} at the same day of the term." if prior else ""
    with shell.card("sum", order=1):
        cells = st.columns([1, 0.12, 1, 0.12, 1], vertical_alignment="center")
        shell.metric(cells[0], "Income", now["income"], delta=_delta("income"),
                     help="Money in this term: dues, sponsorship, transfers." + against)
        cells[1].markdown('<div class="ais-op">−</div>', unsafe_allow_html=True)
        shell.metric(cells[2], "Expenses", now["expenses"], delta=_delta("expenses"), inverse=True,  # F11
                     help="Money out this term, every committee." + against)
        cells[3].markdown('<div class="ais-op">=</div>', unsafe_allow_html=True)
        shell.metric(cells[4].container(key="net-figure"), "Net this term", now["net"], delta=_delta("net"),
                     help="Income minus expenses." + against)

    # --- Sponsorship goal (M20), condensed ---------------------------------------
    #
    # One committee's ledger line, not an org-wide figure like the KPIs above, so
    # it is a caption-plus-bar note rather than a card of its own. The goal-vs-raised
    # history per semester lives on the Treasury page's "Sponsorship history".

    sponsorship_summary = sponsorship_domain.summarize(
        bundle.transactions, bundle.terms, selected_semester
    )
    if sponsorship_summary.goal is None:
        shell.say(
            f"Sponsorship raised this term: **${sponsorship_summary.raised:,.2f}** "
            "— no goal set. Set one on the Treasury page (Terms tab).",
            caption=True,
        )
    else:
        shell.say(
            f"Sponsorship: **${sponsorship_summary.raised:,.2f}** of "
            f"${sponsorship_summary.goal:,.2f} goal — "
            f"**{sponsorship_summary.percent_of_goal:.0f}%**",
            caption=True,
        )
        st.progress(min(sponsorship_summary.percent_of_goal / 100, 1.0))

    # --- Committee budgets -------------------------------------------------------

    budget_column, flow_column = st.columns([1.35, 1], gap="medium")

    with budget_column, shell.card("budgets", order=2):
        committees = pace.committee_pace(transactions, budgets, bundle.terms, clock)
        shell.heading(
            "Committee budgets",
            shell.legend([("Over", t.OVER), ("85%+", t.APPROACHING), ("On track", t.ON_TRACK)]),
            help=(
                f"Each bar is the share of a committee's budget used. The line marks how much of the "
                f"term has passed ({clock.fraction:.0%}); a bar past it is spending ahead of the calendar."
            ),
        )
        if committees.empty:
            shell.empty_state(
                "No budget data for this selection",
                "Set committee budgets for this term on the Treasury page.",
            )
        else:
            shell.chart(charts.budget_health(committees, clock), key="dash_budgets", reveal="left")
            with st.expander("Table"):
                table = committees.rename(
                    columns={"Committee_Name": "Committee", "Remaining": "Left"}
                )[["Committee", "Used", "Budget", "Spent", "Left", "Runs out"]]
                shell.dataframe(
                    table,
                    column_config={
                        "Used": st.column_config.ProgressColumn("Used", format="%.0f%%", min_value=0, max_value=100),
                        "Budget": st.column_config.NumberColumn(format="dollar"),
                        "Spent": st.column_config.NumberColumn(format="dollar"),
                        "Left": st.column_config.NumberColumn(format="dollar"),
                        "Runs out": st.column_config.DateColumn(format="MMM D"),
                    },
                )

    # --- Where the money went ----------------------------------------------------

    with flow_column:
        with shell.card("flow", order=3):
            steps = pace.flow_steps(bundle.transactions, bundle.terms, selected_semester, committee_id=committee_id)
            shell.heading(
                "Where the money went",
                help="Income by source builds up, spending by committee comes off, and the last bar is the net for the term.",
            )
            shell.chart(charts.flow_waterfall(steps), key="dash_flow", reveal="up")
