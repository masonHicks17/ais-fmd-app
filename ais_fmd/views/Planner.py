"""
Module M14 -- planning a term: income, then committee budgets, then the outcome.

Rebuilt 2026-11-17. The treasurer types next term's figures instead of
dragging sliders: the dues rate, how many members will pay, each sponsor
(pledged or paid, when, and a note) and other income. Below that, what the
committees can be given: a suggested budget for each, split by what it has
needed in recent terms, beside what it actually spent, with a box to type the
budget the treasurer settles on. The outcome at the bottom is the same
projection as before, fed by the typed figures.

Sponsors are saved per semester name (planned_sponsorships), so a term can be
planned before Treasury defines it. The other figures and budgets stay on this
page: setting the real budgets is still Treasury -> Budgets.
"""

from __future__ import annotations

import hashlib

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import scenarios
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester_index, ordered_semesters, previous_semester
from ais_fmd.ui import shell, theme

identity = auth.require(auth.Role.TREASURER)

STATUSES = ["Pledged", "Paid"]
SPONSOR_COLUMNS = ["Sponsor", "Amount", "Status", "Date paid", "Notes"]

shell.environment_banner()
shell.page_header(
    "Planner",
    "Plan a term: what will come in, then what each committee can be given, "
    "checked against what they actually spent.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined")
    st.stop()

choices = scenarios.planning_semesters(bundle.terms)
pick_plan, pick_base = st.columns(2)
planning = pick_plan.selectbox(
    "Planning for",
    choices,
    index=len(choices) - 1,
    key="planner_semester",
    help="The last choice is the term after the latest one defined, so it can be planned before it exists.",
)
baseline_semester = pick_base.selectbox(
    "Compare with",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="planner_baseline",
    help="A term that has happened. Its figures fill in the starting numbers and the comparison at the bottom.",
)

baseline = scenarios.build_baseline(bundle.transactions, bundle.budgets, bundle.terms, baseline_semester)

if baseline.income == 0 and baseline.expenses == 0:
    shell.empty_state(
        f"No activity recorded in {baseline_semester}",
        "Choose a term with transactions to compare with.",
    )
    st.stop()

suggestion = scenarios.suggest_from_history(bundle.transactions, bundle.budgets, bundle.terms, baseline_semester)
earlier = previous_semester(bundle.terms, baseline_semester)
history_terms = [s for s in (earlier, baseline_semester) if s]


def _money(value: object) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return 0.0 if pd.isna(number) else float(number)


# --- Income ------------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    f"Income for {planning}",
    help="Type the figures you expect. The starting numbers come from "
    f"{baseline_semester}; change any of them.",
)

dues_column, members_column, other_column = st.columns(3)
dues_rate = dues_column.number_input(
    "Dues rate ($)",
    min_value=0.0,
    value=float(round(suggestion["dues_rate"], 2)),
    step=5.0,
    format="%.2f",
    key="planner_dues_rate",
    help=f"{baseline.dues_payers} members paid an average of {format_currency(baseline.dues_rate)} in {baseline_semester}.",
)
expected_members = int(
    members_column.number_input(
        "Expected paying members",
        min_value=0,
        # The comparison term's real payers. The year-on-year growth is a hint
        # only: applied on top it compounded (15 -> 142 payers made "1,344").
        value=int(baseline.dues_payers),
        step=5,
        key="planner_expected_members",
        help=(
            f"{baseline.dues_payers} paid in {baseline_semester}"
            + (
                f"; {suggestion['reference']} had "
                f"{round(baseline.dues_payers / suggestion['growth']) if suggestion['growth'] else 0}."
                if suggestion.get("reference")
                else "."
            )
        ),
    )
)
other_income = other_column.number_input(
    "Other income ($)",
    min_value=0.0,
    value=float(round(baseline.other_income, 2)),
    step=50.0,
    format="%.2f",
    key="planner_other_income",
    help=f"Events, fundraising and anything else that isn't dues or a sponsor. "
    f"{baseline_semester}: {format_currency(baseline.other_income)}.",
)

# Sponsors: one row each, saved for the semester being planned.
def _sponsor_rows(frame: pd.DataFrame) -> list[dict]:
    rows = []
    for _, row in frame.iterrows():
        name = "" if pd.isna(row["Sponsor"]) else str(row["Sponsor"]).strip()
        if not name and pd.isna(row["Amount"]):
            continue  # an empty row the editor added
        paid_on = row["Date paid"]
        rows.append(
            {
                "sponsor": name,
                "amount": _money(row["Amount"]),
                "status": row["Status"] if row["Status"] in STATUSES else "Pledged",
                "paid_on": None if paid_on is None or pd.isna(paid_on) else pd.Timestamp(paid_on).strftime("%Y-%m-%d"),
                "notes": "" if pd.isna(row["Notes"]) else str(row["Notes"]).strip(),
            }
        )
    return rows


shell.heading(
    "Sponsorships",
    help="Add each sponsor with the boxes below the list. Pledged and paid both count toward the plan; "
    "when the money arrives, set Status to Paid and fill in the date in the list. To remove a sponsor, "
    "tick its row in the list and press Delete. Saved for the semester above.",
)
stored = repo.load_planned_sponsorships(planning)
start = pd.DataFrame(
    {
        "Sponsor": stored["sponsor"].astype(str) if not stored.empty else pd.Series(dtype=str),
        "Amount": pd.to_numeric(stored["amount"], errors="coerce") if not stored.empty else pd.Series(dtype=float),
        "Status": stored["status"].astype(str) if not stored.empty else pd.Series(dtype=str),
        "Date paid": (
            pd.to_datetime(stored["paid_on"], errors="coerce").dt.date if not stored.empty else pd.Series(dtype=object)
        ),
        "Notes": stored["notes"].fillna("").astype(str) if not stored.empty else pd.Series(dtype=str),
    },
    columns=SPONSOR_COLUMNS,
)
if start.empty:
    sponsors = start
else:
    sponsors = st.data_editor(
        start,
        key=f"planner_sponsors_{planning}",
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        disabled=identity.read_only,
        column_config={
            "Sponsor": st.column_config.TextColumn(required=True, max_chars=120),
            "Amount": st.column_config.NumberColumn(min_value=0.0, step=50.0, format="dollar", required=True),
            "Status": st.column_config.SelectboxColumn(options=STATUSES, default="Pledged", required=True),
            "Date paid": st.column_config.DateColumn(format="MMM D, YYYY"),
            "Notes": st.column_config.TextColumn(width="large", max_chars=300),
        },
    )

typed_sponsors = _sponsor_rows(sponsors)
saved_sponsors = _sponsor_rows(start)
totals = scenarios.sponsor_totals(typed_sponsors)
unsaved = typed_sponsors != saved_sponsors
sponsor_note, sponsor_save = st.columns([3, 1], vertical_alignment="center")
with sponsor_note:
    shell.say(
        (
            f"Paid {format_currency(totals.paid)} · pledged {format_currency(totals.pledged)} · "
            f"{totals.count} sponsor{'s' if totals.count != 1 else ''}. "
            if totals.count
            else f"No sponsors for {planning} yet. "
        )
        + f"{baseline_semester} raised {format_currency(baseline.sponsorship_income)} from sponsors.",
        caption=True,
    )
if unsaved and sponsor_save.button(
    "Save changes", key="planner_save_sponsors", type="primary", disabled=identity.read_only, width="stretch"
):
    result = repo.replace_planned_sponsorships(planning, typed_sponsors, identity.email)
    if result.ok:
        shell.flash("success", f"Saved {result.updated} sponsor{'s' if result.updated != 1 else ''} for {planning}.")
        st.rerun()
    else:
        shell.error_state("Sponsors not saved", result.error or "")

with st.form("planner_add_sponsor", clear_on_submit=True, border=False):
    name_column, amount_column, status_column, date_column = st.columns([3, 2, 2, 2])
    new_name = name_column.text_input("Sponsor", placeholder="Company or person", key="planner_new_sponsor")
    new_amount = amount_column.number_input(
        "Amount ($)", min_value=0.0, step=50.0, format="%.2f", key="planner_new_amount"
    )
    new_status = status_column.selectbox("Status", STATUSES, key="planner_new_status")
    new_paid_on = date_column.date_input("Date paid", value=None, key="planner_new_paid_on")
    notes_column, add_column = st.columns([7, 2], vertical_alignment="bottom")
    new_notes = notes_column.text_input(
        "Notes", placeholder="When it was promised, how it was paid, who to thank", key="planner_new_notes"
    )
    add = add_column.form_submit_button(
        "Add sponsor", type="primary", disabled=identity.read_only, width="stretch"
    )
if add:
    if not (new_name or "").strip():
        shell.error_state("Sponsor not added", "Type the sponsor's name.")
    else:
        row = {
            "sponsor": new_name.strip(),
            "amount": float(new_amount or 0),
            "status": new_status,
            "paid_on": new_paid_on.isoformat() if new_paid_on else None,
            "notes": (new_notes or "").strip(),
        }
        # The list as it stands, edits included, plus the new sponsor.
        result = repo.replace_planned_sponsorships(planning, typed_sponsors + [row], identity.email)
        if result.ok:
            shell.flash("success", f"Added {row['sponsor']} ({format_currency(row['amount'])}) to {planning}.")
            st.rerun()
        else:
            shell.error_state("Sponsor not added", result.error or "")

dues_income = dues_rate * expected_members
total_income = dues_income + totals.total + other_income
summary = st.columns(4)
summary[0].metric("Dues", format_currency(dues_income), help=f"{expected_members:,} × {format_currency(dues_rate)}")
summary[1].metric("Sponsorships", format_currency(totals.total))
summary[2].metric("Other income", format_currency(other_income))
summary[3].metric(
    "Expected income",
    format_currency(total_income),
    delta=format_currency(total_income - baseline.income, signed=True),
    help=f"Against {format_currency(baseline.income)} in {baseline_semester}.",
)

# --- Committee budgets -------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    f"Committee budgets for {planning}",
    help="Suggested: what is left of the expected income after other expenses and the reserve, "
    "split by what each committee has needed (its average spend over the terms shown, or its "
    "last budget when that is higher), rounded down to $25. Type a budget in the last column to "
    "set your own; clear it to go back to the suggestion. Nothing here changes Treasury → Budgets.",
)

expense_column, reserve_column = st.columns(2)
other_expenses = expense_column.number_input(
    "Other expenses, not a committee ($)",
    min_value=0.0,
    value=float(round(baseline.other_expenses, 2)),
    step=50.0,
    format="%.2f",
    key="planner_other_expenses",
    help=f"Fees, transfers out and anything not booked to a budgeted committee. "
    f"{baseline_semester}: {format_currency(baseline.other_expenses)}.",
)
reserve_percent = reserve_column.number_input(
    "Keep in reserve (% of income)",
    min_value=0,
    max_value=100,
    value=10,
    step=5,
    key="planner_reserve",
    help="Held back from the committees for surprises; it shows as surplus in the outcome below.",
)
reserve = total_income * reserve_percent / 100
available = max(total_income - other_expenses - reserve, 0.0)

history = scenarios.committee_history(bundle.transactions, bundle.budgets, bundle.terms, history_terms)
suggested = scenarios.suggest_budgets(scenarios.need_weights(history), available)

table = pd.DataFrame({"Committee": [committee_name(cid) for cid in BUDGETED_COMMITTEE_IDS]})
for semester in history_terms:
    table[f"Spent {semester}"] = [history[cid][semester]["spent"] for cid in BUDGETED_COMMITTEE_IDS]
table[f"Budget {baseline_semester}"] = [history[cid][baseline_semester]["budget"] for cid in BUDGETED_COMMITTEE_IDS]
table["Suggested"] = [suggested[cid] for cid in BUDGETED_COMMITTEE_IDS]

# The budget column starts at the suggestion. A typed figure is kept as an
# override for this term (session only) and survives the suggestions moving
# when income changes; the editor is keyed by what it shows, so it redraws
# fresh after each change instead of replaying old edits onto new numbers.
overrides: dict[int, float] = st.session_state.setdefault(f"_planner_overrides_{planning}", {})
plan_column = f"Budget {planning}"
table[plan_column] = [float(overrides.get(cid, suggested[cid])) for cid in BUDGETED_COMMITTEE_IDS]
table["Set by"] = ["You" if cid in overrides else "Suggested" for cid in BUDGETED_COMMITTEE_IDS]

money_columns = {c: st.column_config.NumberColumn(format="dollar") for c in table.columns if c not in ("Committee", "Set by")}
money_columns[plan_column] = st.column_config.NumberColumn(
    format="dollar", min_value=0.0, step=0.01, help="Type a budget to set it; clear it to go back to the suggestion."
)
signature = hashlib.sha1(repr((planning, list(table[plan_column]))).encode()).hexdigest()[:10]
edited = st.data_editor(
    table,
    key=f"planner_budgets_{signature}",
    width="stretch",
    hide_index=True,
    disabled=[c for c in table.columns if c != plan_column],
    column_config=money_columns,
)
changed = False
for cid, shown, typed in zip(BUDGETED_COMMITTEE_IDS, table[plan_column], edited[plan_column]):
    if pd.isna(typed):
        changed |= overrides.pop(cid, None) is not None
    elif abs(float(typed) - float(shown)) > 0.004:
        overrides[cid] = round(float(typed), 2)
        changed = True
if changed:
    st.rerun()
budgets = {cid: float(overrides.get(cid, suggested[cid])) for cid in BUDGETED_COMMITTEE_IDS}
if overrides and st.button("Use the suggestions for every committee", key="planner_reset_budgets"):
    overrides.clear()
    st.rerun()

committee_total = sum(budgets.values())
left_over = total_income - other_expenses - reserve - committee_total
footer = st.columns(4)
footer[0].metric("Expected income", format_currency(total_income))
footer[1].metric("Committee budgets", format_currency(committee_total))
footer[2].metric(
    "Other expenses and reserve",
    format_currency(other_expenses + reserve),
    help=f"{format_currency(other_expenses)} other expenses + {format_currency(reserve)} reserve ({reserve_percent}%).",
)
footer[3].metric(
    "Left over" if left_over >= 0 else "Short by",
    format_currency(abs(left_over)),
    help="Income less budgets, other expenses and the reserve.",
)
if left_over < 0:
    shell.notify(
        "warning",
        f"These budgets are {format_currency(abs(left_over))} more than the income allows after the "
        f"reserve. Lower a budget, or raise dues, members or sponsorships.",
    )

# --- Outcome -----------------------------------------------------------------

scenario = scenarios.Scenario(
    dues_rate=dues_rate,
    expected_members=expected_members,
    other_income=totals.total + other_income,
    other_expenses=other_expenses,
    committee_budgets=budgets,
)
projection = scenarios.project(baseline, scenario)

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    f"{planning} against {baseline_semester}",
    help="The plan above if every committee spends its budget, beside what actually happened. "
    "A planning aid, not a forecast.",
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
    help="Paying members needed to cover the planned expenses at this dues rate.",
)

table_column, chart_column = st.columns([1, 1])

with table_column:
    st.markdown("**Line by line**")
    comparison = scenarios.comparison_frame(projection).rename(
        columns={"Baseline": baseline_semester, "Scenario": planning}
    )
    shell.dataframe(
        comparison,
        column_config={
            baseline_semester: st.column_config.NumberColumn(format="dollar"),
            planning: st.column_config.NumberColumn(format="dollar"),
            "Change": st.column_config.NumberColumn(format="dollar"),
        },
    )

with chart_column:
    st.markdown(f"**{baseline_semester} against {planning}**")
    figure = go.Figure()
    headline = comparison[comparison["Line"].isin(["Total income", "Total expenses", "Net"])]
    figure.add_trace(
        go.Bar(
            name=baseline_semester,
            x=headline["Line"],
            y=headline[baseline_semester],
            marker=dict(color=theme.active().unbudgeted),
            hovertemplate=f"{baseline_semester}: $%{{y:,.2f}}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            name=planning,
            x=headline["Line"],
            y=headline[planning],
            marker=dict(color=theme.active().accent),
            hovertemplate=f"{planning}: $%{{y:,.2f}}<extra></extra>",
        )
    )
    figure.update_layout(
        barmode="group",
        height=340,
        yaxis=dict(title="Dollars", tickprefix="$", separatethousands=True),
        xaxis=dict(title=None),
    )
    shell.chart(figure, key="planner_compare")

