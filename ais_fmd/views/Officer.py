"""
Module M11 -- committee officer portal.

A VP's version of the org-wide Dashboard: the same four questions -- how am I
doing against budget, where did the money go, how does this term compare with
past ones, and am I on pace -- answered for one committee, and only that one.

A committee can own several budget lines (Membership owns Membership and
Passport), so every figure here is about all of them together; `config/
vp_committees.py` says which lines belong. The committee comes from the signed-in
profile, never from a widget a VP could change, and the page only ever reads rows
booked to that committee's lines.

This is the page that depends on FINDING F2 being fixed. Under the original
shared-password model there was no way to give a chair sight of their own budget
without handing them the keys to everything.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from ais_fmd import auth, settings
from ais_fmd.config import vp_committees
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import committee_report, exports, vp_history
from ais_fmd.domain import reimbursements as reimb
from ais_fmd.domain import spend_kinds, vp_metrics
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import (
    attach_semester,
    date_range_for_semester,
    default_semester_index,
    ordered_semesters,
)
from ais_fmd.ui import charts, committee_charts, committee_panels, shell, theme
from ais_fmd.ui import flags as flag_ui

identity = auth.require(auth.Role.OFFICER)

shell.environment_banner()
shell.page_header(
    "My Committee",
    "Your budget, where the money went, and how this term compares — for your "
    "committee only.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined")
    st.stop()

# --- Which committee ---------------------------------------------------------
#
# In production the committee comes from the signed-in profile. A treasurer or
# admin can look at any committee; an officer sees only their own.

can_choose = identity.can(auth.Role.TREASURER)

# An officer whose profile has no committee sees nothing rather than a picker:
# a picker would let them open any committee, which is the one thing this page
# exists to prevent.
if not can_choose and identity.committee_id is None:
    shell.empty_state(
        "No committee assigned to your account",
        " Ask the treasurer to add your email under Officer Access.",
    )
    st.stop()

controls = st.columns([2, 2])
with controls[0]:
    if can_choose:
        committee_names = [committee_name(cid) for cid in BUDGETED_COMMITTEE_IDS]
        default_index = (
            committee_names.index(committee_name(identity.committee_id))
            if identity.committee_id in BUDGETED_COMMITTEE_IDS
            else 0
        )
        picked_name = st.selectbox("Committee", committee_names, index=default_index)
        committee_id = next(
            cid for cid in BUDGETED_COMMITTEE_IDS if committee_name(cid) == picked_name
        )
    else:
        committee_id = identity.committee_id
        st.markdown(f"**Committee**  \n{vp_committees.title_for(committee_id)}")

# A committee can own several budget lines. Everything below is about all of them
# together; an unmapped committee is just its own single line.
line_ids = vp_committees.budget_ids_for(committee_id)
line_names = [committee_name(cid) for cid in line_ids]
chosen_name = vp_committees.title_for(committee_id)

with controls[1]:
    semester = st.selectbox(
        "Semester",
        semesters,
        index=default_semester_index(bundle.transactions, bundle.terms),
        key="officer_semester",
    )

if not can_choose:
    st.caption(
        "You are seeing only your own committee. Treasurers can view any committee."
    )

# How current the figures are, so a VP knows whether last week's charges are in.
uploaded_files = repo.load_uploaded_files()
freshness = vp_metrics.data_freshness(bundle.transactions, uploaded_files)
freshness_note = vp_metrics.freshness_text(bundle.transactions, uploaded_files)
st.caption(freshness_note)

# --- Scope: this committee's rows only, before anything is computed ----------

transactions = bundle.transactions
mine = (
    transactions
    if transactions.empty
    else transactions[transactions["budget_category"].isin(line_ids)]
)
scoped = attach_semester(mine, bundle.terms)
scoped = scoped[scoped["Semester"] == semester]

summary = budget_domain.budget_vs_actual(
    transactions, bundle.budgets, bundle.terms, semester
)
lines = summary[summary["Committee_Name"].isin(line_names)]

if lines.empty:
    shell.empty_state(
        f"No budget or spending recorded for {chosen_name} in {semester}",
        "Ask the treasurer to set an allocation for this term.",
    )
    st.stop()

# --- Headline ----------------------------------------------------------------

position = vp_metrics.rollup(summary, line_names)
percent = position["percent"]
status = vp_metrics.status_for(percent, position["spent"])

# Pace: money used against the share of the term that has passed. Spending 60% of
# a budget means something different in week 3 than in week 12.
elapsed = vp_metrics.term_elapsed_percent(bundle.terms, semester)

# Spent, Remaining and Used take the status colour; Budget is the fixed reference
# and stays neutral. Remaining goes to "over" on its own once it is negative.
tone = committee_panels.tone_for(status)
remaining_tone = "over" if position["remaining"] < 0 else tone

# Each number appears once (2026-11-09). The bar card on the right carries what
# was spent and the share used, against how much of the term has gone; the tiles
# carry what the bar cannot: the budget, what is left, and where the current
# pace ends up. The old "Spent" and "Used" tiles and the pace sentence repeated
# the bar.
projected = vp_metrics.projected_spend(position["spent"], elapsed) if elapsed else None
left_at_end = None if projected is None else position["budget"] - projected
def _dollars(amount: float) -> str:
    # Whole dollars: three tiles side by side cut cents-wide figures off ("$5,370....").
    return f"{'-' if amount < 0 else ''}${abs(amount):,.0f}"


tiles = [
    {"label": "Budget", "value": _dollars(position["budget"]), "currency": False},
    {"label": "Left now", "value": _dollars(position["remaining"]), "currency": False, "tone": remaining_tone},
]
if left_at_end is not None:
    tiles.append({
        "label": "Left at term end" if left_at_end >= 0 else "Over at term end",
        "value": _dollars(abs(left_at_end)),
        "currency": False,
        "tone": "over" if left_at_end < 0 else tone,
    })
tiles_column, bar_column = st.columns([3, 1.35], gap="medium")
with tiles_column:
    shell.metric_row(tiles)
    if left_at_end is not None:
        st.caption("Left at term end assumes you keep spending at the pace you have so far.")
with bar_column:
    with shell.card("spend-bar", order=2):
        shell.html(committee_panels.spend_bar(position, status, elapsed))

if status == "over":
    shell.notify(
        "warning",
        f"{chosen_name} is over its allocation by "
        f"{format_currency(abs(position['remaining']))}. Speak to the treasurer "
        f"before committing anything further.",
    )

# A printable one-page report for this committee and term. Built by a function
# that does its own scoping, so what is in the file cannot depend on this page.
report_html = committee_report.build_for_committee(
    transactions,
    bundle.budgets,
    bundle.terms,
    committee_id,
    semester,
    generated=datetime.now(),
    data_note=freshness_note,
    sandbox=settings.is_sandbox(),
)
st.download_button(
    "Download committee report",
    report_html,
    file_name=f"{exports.slug(chosen_name)}_{exports.slug(semester)}_report.html",
    mime="text/html",
    key="download_committee_report",
    help="A one-page report for this committee and term. Opens in any browser and prints to PDF.",
)

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Updates on flagged charges ----------------------------------------------
#
# What happened to charges this committee flagged, and any charge the treasurer
# moved INTO it. Directly under the budget numbers, which is where a moved
# charge shows up. Nothing draws when there is nothing to say.

flag_ui.updates_panel(
    flag_ui.load_flags_or_none(line_ids),
    flag_ui.load_arrivals_or_none(line_ids),
    bundle.transactions,
    line_ids,
)

# --- Where the money went ----------------------------------------------------
#
# Sits directly under the headline numbers: the first question after "how much is
# left" is "where did it go". A stacked Pareto by payee, with the spending pace
# beside it. It replaced a by-purpose waterfall: every committee books under one
# purpose, so that chart only ever had a single bar.

flow_column, pace_column = st.columns(2, gap="large")

with flow_column:
    st.subheader("Where the money went")
    # By kind of spending (food, bar tabs, supplies...), not by payee: forty
    # restaurants each with 2% told a VP nothing (2026-11-09).
    spending = scoped if scoped.empty else scoped[~budget_domain.is_transfer(scoped)]
    kinds = spend_kinds.kind_split(spending)
    if kinds.empty:
        shell.empty_state("Nothing spent yet", f"No charges are booked to {chosen_name} in {semester}.")
    else:
        shell.chart(
            committee_charts.kind_bars(kinds, committee_charts.committee_color(committee_id)),
            key="committee_kinds",
        )
        stores = [k for k in kinds["Kind"] if k in set(spend_kinds.BIG_STORES.values())]
        shell.say(
            "Grouped by what the merchant sells."
            + (f" {', '.join(stores)} {'is' if len(stores) == 1 else 'are'} shown by name: a big-store "
               "receipt could be food, drinks or supplies, and the bank doesn't say which."
               if stores else ""),
            caption=True,
        )

with pace_column:
    st.subheader("Spending pace")
    term_window = date_range_for_semester(bundle.terms, semester)
    if term_window is None:
        shell.empty_state("No term dates on record", f"{semester} has no start and end dates set.")
    else:
        term_start, term_end = term_window
        spend_so_far = vp_metrics.cumulative_spend(scoped, term_start, term_end, freshness["through"])
        pace_figure = committee_charts.pace_chart(
            spend_so_far,
            position["budget"],
            term_start,
            term_end,
            today=pd.Timestamp.today().normalize(),
        )
        pace_figure.update_layout(height=360)
        shell.chart(pace_figure, key="committee_pace")
        shell.say(
            "Solid: what you have spent. Dashed: an even spend across the term, so "
            "above it means ahead of pace.",
            caption=True,
        )

income_split = budget_domain.categorize_flow(scoped, budget_domain.INCOME)

# A committee that owns several budget lines keeps a per-line breakdown. None do
# today, so this is invisible until one does.
if len(line_names) > 1:
    by_line = lines.rename(columns={"Committee_Name": "Budget line"})
    by_line["% Spent"] = by_line["% Spent"].map(
        lambda value: "—" if value is None or value != value else f"{value:.1f}%"
    )
    shell.dataframe(
        by_line[["Budget line", "Budget", "Spent", "% Spent", "Status"]],
        column_config={
            "Budget": st.column_config.NumberColumn(format="dollar"),
            "Spent": st.column_config.NumberColumn(format="dollar"),
            "% Spent": st.column_config.TextColumn(width="small"),
            "Status": st.column_config.TextColumn(width="small"),
        },
    )

if not income_split.empty:
    st.markdown("#### Money received")
    shell.chart(
        charts.ranked_bar(
            income_split,
            label_column="Category",
            value_column="Amount",
            color=theme.active().income,
        ),
        key="committee_income_split",
    )

# Where this committee stood at the same point in earlier terms: the other half of
# "is 95% spent unusual?". Shown only when there is something meaningful to say.
past_terms = vp_history.same_point_in_past_terms(
    transactions,
    bundle.budgets,
    bundle.terms,
    line_ids,
    semester,
    through=freshness["through"],
    today=pd.Timestamp.today().normalize(),
)
if past_terms is not None:
    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
    usual = vp_history.compare_to_usual(past_terms)
    tag = vp_history.VERDICT_TAGS.get(usual["verdict"]) if usual else None
    kind = {"more": "approaching", "same": "on track", "less": "on track"}.get(usual["verdict"] if usual else "")
    st.markdown(
        "**This term vs. past terms**" + (f" &nbsp;{shell.pill(tag, kind)}" if tag else ""),
        unsafe_allow_html=True,
    )
    sentence = vp_history.short_sentence(past_terms)
    if sentence:
        shell.say(sentence)
    shell.chart(
        committee_charts.same_point_bars(
            vp_history.chart_frame(past_terms),
            usual["typical"] if usual else None,
            # The usual line is the same-season median, so say which season.
            usual["label"].replace("past ", "usual for ").replace(" terms", "") if usual else "usual",
        ),
        key="committee_same_point",
    )
    st.caption(
        f"Each bar is how much of that term's budget was spent by the same point: "
        f"{past_terms.elapsed_percent:.0f}% of the way through the term."
    )
    with st.expander("See the numbers"):
        with shell.card("committee-compare", order=4):
            shell.html(committee_panels.comparison_table(past_terms, status))
        st.caption(
            f"Measured as of {past_terms.as_of:%b} {past_terms.as_of.day} (the earlier of today and "
            "the latest charge on record). Each term is measured against its own budget. "
            "The black mark on each bar is where this term is now."
        )

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Trend and burn rate -----------------------------------------------------

st.subheader("Across semesters")

# Projected burn is shown for some committees only (see `vp_committees`). Everyone
# else gets the semester chart at full width.
show_burn = vp_committees.shows_projected_burn(line_ids)
if show_burn:
    trend_column, burn_column = st.columns([3, 2])
else:
    trend_column, burn_column = st.container(), None

with trend_column:
    history = vp_metrics.historical(mine, bundle.budgets, bundle.terms, line_names)
    shell.chart(charts.trend_bars(history), key="committee_trend")

if burn_column is not None:
    with burn_column:
        st.markdown("**Projected burn**")
        burn = budget_domain.burn_rate_projection(
            transactions, bundle.budgets, bundle.terms, semester
        )
        burn = burn[burn["Committee_Name"].isin(line_names)]
        at_risk = burn[
            burn["Note"].isin({"Projected to exhaust before term end", "Already over budget"})
        ]
        if at_risk.empty:
            st.caption("Nothing projected to overrun: at the current pace you finish within budget.")
        else:
            compact = at_risk.copy()
            compact["Risk"] = compact["Note"].map(
                lambda note: "Over budget" if note == "Already over budget" else "Will exhaust"
            )
            shell.dataframe(
                compact[["Committee_Name", "Daily Burn", "Projected Exhaustion", "Risk"]].rename(
                    columns={"Committee_Name": "Budget line", "Daily Burn": "Per day"}
                ),
                column_config={
                    "Per day": st.column_config.NumberColumn(format="dollar"),
                    "Projected Exhaustion": st.column_config.DateColumn(format="MMM D, YYYY"),
                    "Risk": st.column_config.TextColumn(width="small"),
                },
            )

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

# --- Reimbursements ----------------------------------------------------------
#
# Reimbursements are cut from the MVP: the page is unloaded from app.py's
# navigation and the tables live in the deferred migration 002. This section
# used to call `repo.load_reimbursements()` unconditionally, which against a
# database with only migration 001 applied queries a relation that does not
# exist -- taking down the whole page rather than hiding one section of it.
#
# Asking the backend whether it can answer, rather than assuming it can, keeps
# the section working in the sandbox (where the tables do exist) and silent in
# production until 002 runs. Turning the feature back on needs no change here.

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

try:
    requests = repo.load_reimbursements()
    reimbursements_available = True
except Exception:  # noqa: BLE001 - a missing table is a deployment state, not an error
    requests = pd.DataFrame()
    reimbursements_available = False

if not reimbursements_available:
    st.caption(
        "Reimbursement tracking is not enabled on this deployment yet. Your "
        "budget and spending above are unaffected."
    )
    st.stop()

st.markdown("#### Reimbursements for this committee")

if requests.empty:
    shell.empty_state(
        "No requests raised",
        "Ask the treasurer if someone on your committee is owed money back.",
    )
else:
    mine_requests = requests[requests["committee_id"].isin(line_ids)]
    if mine_requests.empty:
        shell.empty_state("No requests for this committee yet")
    else:
        outstanding = mine_requests[mine_requests["status"].isin([reimb.PENDING, reimb.APPROVED])]
        if not outstanding.empty:
            shell.say(
                f"{len(outstanding)} outstanding, totalling "
                f"{format_currency(outstanding['amount'].sum())} — this is committed "
                f"but not yet reflected in the spend figure above.",
                caption=True,
            )
        shell.dataframe(
            reimb.display_frame(mine_requests),
            column_config={
                "Amount": st.column_config.NumberColumn(format="dollar"),
                "Submitted": st.column_config.DatetimeColumn(format="MMM D, YYYY"),
            },
        )
