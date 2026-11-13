"""
Module M1 -- reconciliation.

The check the original never performed: does the ledger actually agree with what
the bank says the balance was?
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import ACCOUNTS
from ais_fmd.data import repositories as repo
from ais_fmd.domain import reconcile
from ais_fmd.domain.money import format_currency
from ais_fmd.ui import shell

identity = auth.require(auth.Role.MEMBER)
can_edit = identity.can_write

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Reconciliation",
    "Prove that opening balance plus the period's transactions equals the closing "
    "balance the bank reported.",
)

transactions = repo.load_transactions()
balances = repo.load_statement_balances()

if balances.empty:
    shell.empty_state(
        "No statement balances recorded",
        "Add a statement period below to reconcile it against the ledger.",
    )
else:
    results = repo.reconcile_all()
    unbalanced = [result for result in results if not result.balanced]

    metrics = st.columns(4)
    metrics[0].metric("Periods checked", len(results))
    metrics[1].metric("Reconciled", len(results) - len(unbalanced))
    metrics[2].metric("Out of balance", len(unbalanced))
    total_gap = sum(abs(result.discrepancy) for result in unbalanced)
    metrics[3].metric("Total unexplained", format_currency(total_gap))

    if unbalanced:
        st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
        shell.heading(
            "Periods that do not reconcile",
            help="Activity gaps are listed under a period when the ledger goes quiet. "
            "A long silence usually means a statement was never imported.",
        )
        for result in unbalanced:
            with st.container(border=True):
                st.markdown(
                    f"{shell.pill('out of balance', 'over')} &nbsp; **{result.account}** "
                    f"· {result.period_start:%b %d, %Y} – {result.period_end:%b %d, %Y}",
                    unsafe_allow_html=True,
                )
                shell.say(result.explain())
                detail = st.columns(4)
                detail[0].metric("Opening", format_currency(result.opening_balance))
                detail[1].metric("Transactions", f"{result.transaction_count:,}")
                detail[2].metric("Expected closing", format_currency(result.expected_closing))
                detail[3].metric("Statement says", format_currency(result.closing_balance))
                if result.date_gaps:
                    st.caption(
                        "Activity gaps in this period: "
                        + "; ".join(
                            f"{start:%b %d} → {end:%b %d}" for start, end in result.date_gaps[:4]
                        )
                        + "."
                    )
    else:
        st.success("Every recorded statement period reconciles.")

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
    st.markdown("#### All periods")
    frame = reconcile.to_frame(results)
    shell.dataframe(
        frame,
        column_config={
            "Opening": st.column_config.NumberColumn(format="dollar"),
            "Expected Closing": st.column_config.NumberColumn(format="dollar"),
            "Statement Closing": st.column_config.NumberColumn(format="dollar"),
            "Discrepancy": st.column_config.NumberColumn(format="dollar"),
        },
    )

# --- Record a period ---------------------------------------------------------

if can_edit:
    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
    shell.heading(
        "Record a statement period",
        help="Take these four numbers straight off the statement header. The ledger is "
        "then checked against them.",
    )

    with st.form("balance_form"):
        columns = st.columns(3)
        with columns[0]:
            account = st.selectbox("Account", ACCOUNTS, key="rec_account")
        with columns[1]:
            period_start = st.date_input("Period start", key="rec_start")
            opening = st.number_input("Opening balance", value=0.0, step=100.0, format="%.2f")
        with columns[2]:
            period_end = st.date_input("Period end", key="rec_end")
            closing = st.number_input("Closing balance", value=0.0, step=100.0, format="%.2f")
        submitted = st.form_submit_button("Record and reconcile", type="primary")

    if submitted:
        if period_end <= period_start:
            shell.error_state("Period end must be after the period start")
        else:
            result = repo.record_statement_balance(
                {
                    "account": account,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "opening_balance": float(opening),
                    "closing_balance": float(closing),
                    "source_file": None,
                },
                identity.email,
            )
            if result.error:
                shell.error_state("Could not record the period", result.error)
            else:
                check = reconcile.reconcile_period(
                    transactions,
                    account=account,
                    period_start=period_start,
                    period_end=period_end,
                    opening_balance=opening,
                    closing_balance=closing,
                )
                shell.notify("success" if check.balanced else "warning", check.explain())
                st.rerun()
