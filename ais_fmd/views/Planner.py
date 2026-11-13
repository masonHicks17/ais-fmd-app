"""Module M14 -- scenario planner."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import scenarios
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester_index, ordered_semesters
from ais_fmd.ui import shell, theme

auth.require(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Scenario Planner",
    "Model next term against what actually happened last term, rather than "
    "against a guess.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined")
    st.stop()

baseline_semester = st.selectbox(
    "Base the projection on",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="planner_baseline",
)

baseline = scenarios.build_baseline(
    bundle.transactions, bundle.budgets, bundle.terms, baseline_semester
)

if baseline.income == 0 and baseline.expenses == 0:
    shell.empty_state(
        f"No activity recorded in {baseline_semester}",
        "Choose a term with transactions to project from.",
    )
    st.stop()

suggestion = scenarios.suggest_from_history(
    bundle.transactions, bundle.budgets, bundle.terms, baseline_semester
)

member_word = "member" if baseline.dues_payers == 1 else "members"
shell.say(
    f"Baseline: {format_currency(baseline.income)} in, "
    f"{format_currency(baseline.expenses)} out, net "
    f"{format_currency(baseline.net)} — {baseline.dues_payers} {member_word} paid an "
    f"average of {format_currency(baseline.dues_rate)}."
    + (
        f" Membership moved {suggestion['growth']:.2f}× against {suggestion['reference']}."
        if suggestion.get("reference")
        else ""
    ),
    caption=True,
)

# --- Inputs ------------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
left, right = st.columns([1, 1])

with left:
    st.markdown("#### Membership and dues")
    dues_rate = shell.linked_slider(
        "Dues rate",
        min_value=0.0,
        max_value=150.0,
        value=float(suggestion["dues_rate"]),
        step=2.50,
        format="dollar",
        key="planner_dues_rate",
    )
    # Fixed 0-300, not `max(400, suggestion["expected_members"] * 2)`: a range
    # that scales with a *projected* number made the slider's proportions
    # swing with whatever the projection happened to suggest, which read as
    # broken rather than adaptive. 300 comfortably covers the real roster
    # (149 members on the Fall 2026 form, HANDOFF.md §12.1) with room to model
    # real growth; the number input next to it still accepts anything in range
    # exactly, so nothing is lost for an unusual term.
    expected_members = shell.linked_slider(
        "Expected paying members",
        min_value=0,
        max_value=300,
        value=min(int(suggestion["expected_members"]), 300),
        step=5,
        key="planner_expected_members",
    )
    other_income_change = st.number_input(
        "Change in other income (sponsorship, events)",
        value=0.0,
        step=250.0,
        format="%.2f",
        help="Positive raises non-dues income; negative reduces it.",
    )

with right:
    shell.heading(
        "Committee spending",
        help="Percentage of what each committee actually spent in the baseline term.",
    )

    multipliers: dict[int, float] = {}
    active = [
        cid for cid in BUDGETED_COMMITTEE_IDS if baseline.committee_spend.get(cid, 0.0) > 0
    ]
    if not active:
        st.caption("No committee spending in the baseline term to adjust.")
    else:
        global_adjust = st.slider(
            "Adjust every committee", 0, 200, 100, step=5, format="%d%%"
        )
        with st.expander("Adjust individually"):
            for committee_id in active:
                multipliers[committee_id] = (
                    st.slider(
                        f"{committee_name(committee_id)} "
                        f"({format_currency(baseline.committee_spend[committee_id])})",
                        0,
                        200,
                        global_adjust,
                        step=5,
                        format="%d%%",
                        key=f"mult_{committee_id}",
                    )
                    / 100
                )
        for committee_id in active:
            multipliers.setdefault(committee_id, global_adjust / 100)

    other_expense_change = st.number_input(
        "Change in other expenses",
        value=0.0,
        step=250.0,
        format="%.2f",
    )

scenario = scenarios.Scenario(
    dues_rate=dues_rate,
    expected_members=expected_members,
    committee_multipliers=multipliers,
    other_income_change=other_income_change,
    other_expense_change=other_expense_change,
)
projection = scenarios.project(baseline, scenario)

# --- Outcome -----------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    "Projected outcome",
    help="Projections assume next term resembles the baseline in everything not "
    "adjusted here. They are a planning aid, not a forecast.",
)

metrics = st.columns(4)
metrics[0].metric(
    "Income", format_currency(projection.projected_income),
    delta=format_currency(projection.income_change, signed=True),
)
metrics[1].metric(
    "Expenses", format_currency(projection.projected_expenses),
    delta=format_currency(projection.expense_change, signed=True),
    delta_color="inverse",
)
metrics[2].metric(
    "Net", format_currency(projection.projected_net),
    delta=format_currency(projection.net_change, signed=True),
)
break_even = scenarios.break_even_members(baseline, scenario)
metrics[3].metric(
    "Break-even members",
    "—" if break_even is None else f"{break_even:,}",
    help="Paying members needed to cover projected expenses at this dues rate.",
)

if projection.projected_net < 0:
    shell.notify(
        "warning",
        f"This scenario runs a deficit of "
        f"{format_currency(abs(projection.projected_net))}."
        + (
            f" It would take {break_even:,} paying members at "
            f"{format_currency(dues_rate)} to break even."
            if break_even
            else ""
        ),
    )
else:
    shell.notify(
        "success",
        f"This scenario finishes with a surplus of "
        f"{format_currency(projection.projected_net)}.",
    )

# --- Detail ------------------------------------------------------------------

table_column, chart_column = st.columns([1, 1])

with table_column:
    st.markdown("**Line by line**")
    comparison = scenarios.comparison_frame(projection)
    shell.dataframe(
        comparison,
        column_config={
            "Baseline": st.column_config.NumberColumn(format="dollar"),
            "Scenario": st.column_config.NumberColumn(format="dollar"),
            "Change": st.column_config.NumberColumn(format="dollar"),
        },
    )

with chart_column:
    st.markdown("**Baseline against scenario**")
    figure = go.Figure()
    headline = comparison[comparison["Line"].isin(["Total income", "Total expenses", "Net"])]
    figure.add_trace(
        go.Bar(
            name="Baseline",
            x=headline["Line"],
            y=headline["Baseline"],
            marker=dict(color=theme.active().unbudgeted),
            hovertemplate="Baseline: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            name="Scenario",
            x=headline["Line"],
            y=headline["Scenario"],
            marker=dict(color=theme.active().accent),
            hovertemplate="Scenario: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        barmode="group",
        height=340,
        yaxis=dict(title="Dollars", tickprefix="$", separatethousands=True),
        xaxis=dict(title=None),
    )
    shell.chart(figure, key="planner_compare")

committees = scenarios.committee_frame(projection)
if not committees.empty:
    st.markdown("**By committee**")
    shell.dataframe(
        committees,
        column_config={
            "Baseline": st.column_config.NumberColumn(format="dollar"),
            "Scenario": st.column_config.NumberColumn(format="dollar"),
            "Change": st.column_config.NumberColumn(format="dollar"),
        },
    )

