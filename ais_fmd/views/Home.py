"""
Home: now and next, then the term in detail.

Home and Dashboard were merged on 2026-11-08. The top half looks forward from
today -- every chart answers a question a treasurer acts on this week:

  Cash runway          will the balance stay above the reserve through term end?
  Spend pace           are we spending faster than the budget allows?
  Committees to watch  who is furthest ahead of the calendar, and when do they run out?
  Dues                 are members paying faster than the same term last year?
  Needs you            alerts and data-quality issues, one list, each opens its page

Below it, "This term in detail" is the former Dashboard (ui/term_analysis.py):
KPIs against past terms, every committee's budget, where the money went and
net by week, with its own semester and committee pickers.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from ais_fmd import auth, nav
from ais_fmd.data import repositories as repo
from ais_fmd.domain import alerts as alert_domain
from ais_fmd.domain import dues as dues_domain
from ais_fmd.domain import pace
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester, ordered_semesters, term_id_for_semester
from ais_fmd.ui import charts, shell, term_analysis

# Session keys of the hub switchers (see the *Hub.py views), so an action can
# open a hub on the right view.
_HUB_KEYS = {
    "ais_fmd/views/TransactionsHub.py": "transactions_hub",
    "ais_fmd/views/AlertsHub.py": "alerts_hub",
}

identity = auth.current_user()

# A VP's navigation is a short list (see `ais_fmd/nav.py`); a button that jumps to a
# page outside it would raise, so those buttons are simply not drawn for them.
_OPENABLE = {page[0] for page in nav.VP_PAGES} if identity.committee_scoped else None


_HERE = "ais_fmd/views/Home.py"


def _can_open(page: str) -> bool:
    return page == _HERE or _OPENABLE is None or page in _OPENABLE


shell.environment_banner()

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.page_header("UF AIS Financial Management")
    shell.empty_state(
        "No terms defined yet",
        "Add an academic term from Treasury → Manage Terms to get started.",
    )
    st.stop()

semester = default_semester(bundle.transactions, bundle.terms)
clock = pace.term_clock(bundle.terms, semester)
if clock is None:
    shell.page_header("UF AIS Financial Management")
    shell.empty_state(f"{semester} has no start and end dates", "Set them on Treasury → Manage Terms.")
    st.stop()

prior = pace.equivalent_prior(bundle.terms, semester)

# --- Header ------------------------------------------------------------------

hour = datetime.now().hour
greeting = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
head, term_progress = st.columns([3, 1], vertical_alignment="bottom")
with head:
    shell.page_header(
        f"Good {greeting}, {identity.role.label}",
        f"{semester} · week {clock.week} of {clock.weeks}",
    )
with term_progress:
    st.progress(clock.fraction, text=f"{semester} · day {clock.day} of {clock.length}")

main, side = st.columns([2.6, 1], gap="medium")

# --- Cash runway -------------------------------------------------------------

with main:
    balances = repo.load_statement_balances()
    runway = pace.cash_runway(
        bundle.transactions, bundle.budgets, bundle.terms, balances, clock, prior=prior
    )
    with shell.card("runway", order=1):
        now_column, end_column = st.columns(2)
        shell.metric(now_column, "In the bank", runway.today_balance)
        shell.metric(
            end_column,
            f"Projected {clock.end:%b %d}",
            runway.end_mid,
            delta=runway.end_mid - pace.RESERVE_FLOOR,
        )
        shell.chart(
            charts.cash_runway(runway, clock, reserve=pace.RESERVE_FLOOR),
            key="home_runway",
            reveal="left",
        )
        shell.say(
            f"{runway.anchor}. Likely range at term end {format_currency(runway.end_low)} "
            f"to {format_currency(runway.end_high)}; the delta above is against the "
            f"{format_currency(pace.RESERVE_FLOOR)} reserve.",
            caption=True,
        )

    pace_column, watch_column = st.columns(2, gap="medium")

    # --- Spend pace ----------------------------------------------------------

    with pace_column, shell.card("pace", order=2):
        budget_total = pace.budget_totals(bundle.budgets, bundle.terms).get(semester, 0.0)
        spent = pace.cumulative_by_day(
            bundle.transactions, bundle.terms, semester, measure="expenses", through_day=clock.day
        )
        prior_spent = (
            pace.cumulative_by_day(bundle.transactions, bundle.terms, prior, measure="expenses")
            if prior
            else None
        )
        expected = budget_total * clock.fraction
        actual = float(spent["Value"].iloc[-1]) if not spent.empty else 0.0
        ratio = actual / expected if expected else 0.0
        if not budget_total:
            status = shell.pill("no budget set", "muted")
        elif ratio <= 1.05:
            status = shell.pill("On pace", "on track")
        elif ratio <= 1.2:
            status = shell.pill("Ahead of pace", "approaching")
        else:
            status = shell.pill(f"{ratio:.1f}× pace", "over")
        shell.heading("Spend pace", status)
        shell.chart(
            charts.spend_pace(spent, prior_spent, clock, budget=budget_total, prior_label=prior),
            key="home_pace",
            reveal="left",
        )

    # --- Committees to watch -------------------------------------------------

    with watch_column, shell.card("watch", order=3):
        committees = pace.committee_pace(bundle.transactions, bundle.budgets, bundle.terms, clock)
        shell.heading("Committees to watch")
        shell.chart(charts.committee_pace(committees, clock, rows=3), key="home_watch")
        if not committees.empty:
            # Same page now: a link down to the committee budgets section.
            st.markdown(
                f"[All {len(committees)} committees ↓](#{term_analysis.ANCHOR})",
                unsafe_allow_html=False,
            )

# --- Dues --------------------------------------------------------------------

with side:
    with shell.card("dues", order=4):
        summary = dues_domain.summarize(bundle.transactions, bundle.terms, semester)
        term_id = term_id_for_semester(bundle.terms, semester)
        roster = repo.load_members(term_id) if term_id else None
        roster_size = 0 if roster is None else len(roster)
        curve = dues_domain.collection_curve(bundle.transactions, bundle.terms, semester)
        prior_curve = (
            dues_domain.collection_curve(bundle.transactions, bundle.terms, prior) if prior else None
        )
        collected = summary.total_collected
        delta = None
        if prior_curve is not None and not prior_curve.empty:
            at_day = prior_curve[prior_curve["Day"] <= clock.day]["Cumulative"]
            prior_collected = float(at_day.iloc[-1]) if not at_day.empty else 0.0
            delta = f"{format_currency(collected - prior_collected, signed=True)} vs {prior} at day {clock.day}"
        shell.metric(
            st,
            "Dues",
            f"{summary.unique_payers:,} / {roster_size:,} paid" if roster_size else f"{summary.unique_payers:,} paid",
            delta=delta,
            currency=False,
        )
        shell.chart(charts.dues_curve(curve, prior_curve, clock, prior_label=prior), key="home_dues")
        shell.say(f"{format_currency(collected)} collected this term.", caption=True)
        if _can_open("ais_fmd/views/DuesHub.py") and st.button(
            "Open Dues", key="home_open_dues", icon=":material/arrow_forward:"
        ):
            st.switch_page("ais_fmd/views/DuesHub.py")

    # --- Needs you -----------------------------------------------------------

    with shell.card("todo", order=5):
        items: list[tuple[str, str, str, str, str | None]] = []  # title, detail, icon, page, hub tab
        for alert in alert_domain.evaluate(
            bundle.transactions, bundle.budgets, bundle.terms, balances, semester=semester
        ):
            code = alert.code.lower()
            if "budget" in code:
                target = (_HERE, None)  # the committee budgets section below
            elif "backlog" in code or "uncateg" in code:
                target = ("ais_fmd/views/TransactionsHub.py", "Review Queue")
            elif "reconcil" in code:
                target = ("ais_fmd/views/Reconciliation.py", None)
            elif "stale" in code:
                target = ("ais_fmd/views/TransactionsHub.py", "Upload statement")
            else:
                target = ("ais_fmd/views/AlertsHub.py", "Alerts & Reports")
            icon = {"critical": ":material/error:", "warning": ":material/warning:"}.get(
                alert.severity, ":material/info:"
            )
            items.append((alert.title, alert.detail, icon, *target))
        # Pages a VP cannot open are not offered; their alerts are not theirs to work.
        items = [item for item in items if _can_open(item[3])]
        if identity.can(auth.Role.TREASURER):
            for issue in repo.run_quality_checks():
                if issue.severity in {"critical", "high", "medium"}:
                    items.append((
                        issue.title,
                        f"{issue.count:,} row(s)",
                        ":material/rule:",
                        "ais_fmd/views/AlertsHub.py",
                        "Data Quality",
                    ))

        shell.heading(
            "Needs you",
            shell.pill(str(len(items)), "over" if items else "on track"),
        )
        if not items:
            shell.empty_state("Nothing needs you", "Alerts and data-quality checks are clear.")
        for index, (title, detail, icon, page, tab) in enumerate(items[:5]):
            with st.container(key=f"todo-{index}"):
                if page == _HERE:
                    # Budget alerts: the chart that explains them is further down.
                    glyph = icon.removeprefix(":material/").removesuffix(":")
                    shell.html(
                        f'<a class="ais-todo-link" href="#{term_analysis.ANCHOR}" target="_self">'
                        f'<span class="material-symbols-rounded">{glyph}</span>{title}</a>'
                    )
                elif st.button(shell.money_safe(title), key=f"todo_button_{index}", icon=icon, type="tertiary"):
                    if tab and page in _HUB_KEYS:
                        st.session_state[_HUB_KEYS[page]] = tab
                    st.switch_page(page)
                shell.say(detail, caption=True)
        if len(items) > 5:
            shell.say(f"{len(items) - 5} more on Alerts & Data Quality.", caption=True)

# --- This term in detail (the former Dashboard) --------------------------------

term_analysis.render(bundle, semesters)
