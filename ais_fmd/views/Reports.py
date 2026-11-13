"""
Modules M6 and M12 -- alerts and the board pack.

Alerts report on the *state of the finances*; Data Quality reports on the
*integrity of the records*. They are deliberately separate: clean data can still
be telling you something is wrong.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import report as report_domain
from ais_fmd.domain.terms import default_semester_index, ordered_semesters
from ais_fmd.ui import reconcile_tab, shell

# Where each alert's "→" line leads. The hub tab is chosen the way Home does it:
# `shell.hub` reads the hub's session key before it draws.
_ACTION_TARGETS = {
    "unreconciled": ("ais_fmd/views/Reconciliation.py", None),
    "uncategorized_backlog": ("ais_fmd/views/TransactionsHub.py", "Review Queue"),
    "stale_data": ("ais_fmd/views/Treasury.py", "Reconcile"),
    "unbudgeted_spend": ("ais_fmd/views/Treasury.py", "Budgets"),
}

auth.require(auth.Role.MEMBER)

shell.environment_banner()
shell.page_header(
    "Alerts & Reports",
    "What needs attention now, and a printable summary for the board.",
)

bundle = repo.load_bundle()
balances = repo.load_statement_balances()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined", "Add an academic term first.")
    st.stop()

selected = st.selectbox(
    "Semester",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="report_semester",
)

tab_alerts, tab_report = st.tabs(["Alerts", "Board pack"])

# ============================================================================
# ALERTS (M6)
# ============================================================================

with tab_alerts:
    alerts = repo.evaluate_alerts(semester=selected)

    if not alerts:
        st.success("Nothing needs attention. Budgets are within limits and the ledger reconciles.")
    else:
        counts = {
            level: sum(1 for alert in alerts if alert.severity == level)
            for level in ("critical", "warning", "info")
        }
        metrics = st.columns(3)
        metrics[0].metric("Critical", counts["critical"])
        metrics[1].metric("Warnings", counts["warning"])
        metrics[2].metric("For information", counts["info"])

        st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

        style = {"critical": "over", "warning": "approaching", "info": "muted"}
        for index, alert in enumerate(alerts):
            with st.container(border=True):
                st.markdown(
                    f"{shell.pill(alert.severity, style.get(alert.severity, 'muted'))} "
                    f"&nbsp; **{alert.title}**",
                    unsafe_allow_html=True,
                )
                shell.say(alert.detail)
                target = _ACTION_TARGETS.get(alert.code)
                if alert.action and target:
                    page, tab = target
                    if st.button(alert.action, key=f"alert_go_{alert.code}_{index}", type="tertiary",
                                 icon=":material/arrow_forward:"):
                        if tab and page.endswith("Treasury.py"):
                            reconcile_tab.open_treasury(tab, reconcile_tab.BANK)
                        elif tab:
                            st.session_state["transactions_hub"] = tab
                        st.switch_page(page)
                elif alert.action:
                    st.caption(f"→ {alert.action}")

        with st.expander("How this differs from Data Quality"):
            st.markdown(
                """
**Data Quality** reports on the integrity of the records — orphaned references,
uncategorized rows, values that cannot be right.

**Alerts** report on the state of the finances — a committee over budget, a
period that will not reconcile, a statement that looks like it was never
uploaded. Perfectly clean data can still raise every alert on this page.
"""
            )

# ============================================================================
# BOARD PACK (M12)
# ============================================================================

with tab_report:
    shell.heading(
        "Semester report",
        help="A self-contained HTML file: headline figures, budget against actual, "
        "largest expenses, dues, and anything outstanding. Opens in any browser "
        "and prints to PDF — the print stylesheet is included.",
    )

    pack = report_domain.build(
        bundle.transactions, bundle.budgets, bundle.terms, selected, balances
    )

    st.markdown("**Summary as it will appear**")
    for line in pack.narrative:
        shell.say(f"• {line}")

    preview_columns = st.columns(4)
    preview_columns[0].metric("Income", f"${pack.totals['income']:,.2f}")
    preview_columns[1].metric("Expenses", f"${pack.totals['expenses']:,.2f}")
    preview_columns[2].metric("Net", f"${pack.totals['net']:,.2f}")
    preview_columns[3].metric("Outstanding items", len(pack.alerts))

    html_document = report_domain.to_html(pack)

    st.download_button(
        f"Download {selected} report",
        html_document,
        file_name=f"AIS_{selected.replace(' ', '_')}_report.html",
        mime="text/html",
        type="primary",
        key="download_report",
    )

    with st.expander("Preview the full report"):
        st.components.v1.html(html_document, height=760, scrolling=True)
