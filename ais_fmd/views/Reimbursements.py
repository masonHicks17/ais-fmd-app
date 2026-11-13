"""
Modules M8 and M9 -- reimbursement requests and receipt capture.

A request records the committee and purpose *before* the money moves, so the
matching bank transaction arrives already categorized with a receipt attached —
instead of the categorizer reverse-engineering intent from a Venmo note.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    committee_dropdown_options,
    committee_name,
    parse_committee_label,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import receipts as receipts_domain
from ais_fmd.domain import reimbursements as reimb
from ais_fmd.domain.money import format_currency
from ais_fmd.ui import shell

identity = auth.require(auth.Role.MEMBER)
is_treasurer = identity.can(auth.Role.TREASURER)

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Reimbursements",
    "Submit what you spent with a receipt. Approved requests match themselves "
    "against the bank feed when the next statement is uploaded.",
)

requests = repo.load_reimbursements()
receipts = repo.load_receipts()
transactions = repo.load_transactions()

summary = reimb.summarize(requests)
metrics = st.columns(4)
metrics[0].metric("Awaiting review", summary[reimb.PENDING])
metrics[1].metric("Approved, unpaid", summary[reimb.APPROVED])
metrics[2].metric("Paid", summary[reimb.PAID])
metrics[3].metric("Outstanding", format_currency(summary["outstanding_amount"]))

tabs = ["Submit a request", "All requests"]
if is_treasurer:
    tabs += ["Review", "Match to bank feed", "Receipt coverage"]
rendered = st.tabs(tabs)

# ============================================================================
# SUBMIT
# ============================================================================

with rendered[0]:
    shell.heading(
        "New request",
        help="Attach the receipt now — it is far harder to find later, and an expense "
        "without one is the hardest thing to defend in an audit.",
    )

    with st.form("reimbursement_form", clear_on_submit=True):
        columns = st.columns(2)
        with columns[0]:
            requester = st.text_input("Your name or email", value=identity.email)
            amount = st.number_input("Amount", min_value=0.0, step=5.0, format="%.2f")
            incurred_on = st.date_input("Date of the expense")
        with columns[1]:
            committee_label = st.selectbox("Committee", committee_dropdown_options())
            description = st.text_area(
                "What was it for?",
                placeholder="Groceries for the Oct 8 GBM…",
                height=104,
            )

        receipt_file = st.file_uploader(
            "Receipt",
            type=[extension.lstrip(".") for extension in sorted(receipts_domain.ALLOWED_EXTENSIONS)],
            help=f"Image or PDF, up to {receipts_domain.MAX_BYTES // 1_048_576} MB.",
        )
        submitted = st.form_submit_button("Submit request", type="primary", disabled=identity.read_only)

    if submitted:
        committee_id = parse_committee_label(committee_label)
        problem = reimb.validate_request(amount, description, committee_id)

        receipt_id = None
        if not problem and receipt_file is not None:
            data = receipt_file.getvalue()
            stored = receipts_domain.store(receipt_file.name, data, receipt_file.type)
            if not stored.ok:
                problem = stored.error
            else:
                receipt_id, receipt_result = repo.store_receipt(
                    {
                        "file_name": stored.file_name,
                        "stored_path": stored.stored_path,
                        "content_type": stored.content_type,
                        "byte_size": stored.byte_size,
                        "vendor": None,
                        "amount": float(amount),
                        "receipt_date": incurred_on.isoformat(),
                    },
                    identity.email,
                )
                if receipt_result.error:
                    problem = receipt_result.error

        if problem:
            shell.error_state("Request not submitted", problem)
        else:
            result = repo.create_reimbursement(
                {
                    "requester": requester.strip() or identity.email,
                    "committee_id": committee_id,
                    "amount": float(amount),
                    "description": description.strip(),
                    "incurred_on": incurred_on.isoformat(),
                    "receipt_id": receipt_id,
                },
                identity.email,
            )
            if result.error:
                shell.error_state("Could not save the request", result.error)
            else:
                shell.notify(
                    "success",
                    f"Submitted {format_currency(amount)} for "
                    f"{committee_name(committee_id)}."
                    + (" Receipt attached." if receipt_id else " No receipt attached."),
                )
                st.rerun()

# ============================================================================
# ALL REQUESTS
# ============================================================================

with rendered[1]:
    if requests.empty:
        shell.empty_state("No requests yet")
    else:
        status_filter = st.multiselect(
            "Status", list(reimb.STATUSES), default=list(reimb.STATUSES), key="reimb_status"
        )
        view = requests[requests["status"].isin(status_filter)]
        shell.dataframe(
            reimb.display_frame(view),
            column_config={
                "Amount": st.column_config.NumberColumn(format="dollar"),
                "Submitted": st.column_config.DatetimeColumn(format="MMM D, YYYY"),
            },
        )

# ============================================================================
# REVIEW (treasurer)
# ============================================================================

if is_treasurer:
    with rendered[2]:
        pending = requests[requests["status"] == reimb.PENDING] if not requests.empty else pd.DataFrame()
        if pending.empty:
            st.success("Nothing awaiting review.")
        else:
            receipts_by_id = (
                receipts.set_index("receipt_id").to_dict("index") if not receipts.empty else {}
            )

            for _, request in pending.iterrows():
                request_id = int(request["request_id"])
                with st.container(border=True):
                    header, actions = st.columns([3, 1])
                    with header:
                        shell.say(
                            f"**{format_currency(request['amount'])}** — "
                            f"{committee_name(request['committee_id'])} · "
                            f"{request['requester']}"
                        )
                        st.caption(request["description"] or "(no description)")
                        st.caption(f"Incurred {request['incurred_on']}")

                        receipt = receipts_by_id.get(request.get("receipt_id"))
                        if receipt:
                            data = receipts_domain.load(receipt["stored_path"])
                            if data:
                                st.download_button(
                                    f"Receipt: {receipt['file_name']}",
                                    data,
                                    file_name=receipt["file_name"],
                                    mime=receipt.get("content_type") or "application/octet-stream",
                                    key=f"receipt_dl_{request_id}",
                                )
                            else:
                                st.caption("Receipt file is missing from storage.")
                        else:
                            st.caption("⚠ No receipt attached.")

                    with actions:
                        note = st.text_input(
                            "Note", key=f"note_{request_id}", label_visibility="collapsed",
                            placeholder="Optional note",
                        )
                        if st.button("Approve", key=f"approve_{request_id}", type="primary", disabled=identity.read_only):
                            result = repo.decide_reimbursement(
                                request_id, reimb.APPROVED, identity.email, note
                            )
                            if result.error:
                                shell.error_state("Could not approve", result.error)
                            else:
                                st.rerun()
                        if st.button("Reject", key=f"reject_{request_id}", disabled=identity.read_only):
                            result = repo.decide_reimbursement(
                                request_id, reimb.REJECTED, identity.email, note
                            )
                            if result.error:
                                shell.error_state("Could not reject", result.error)
                            else:
                                st.rerun()

    # ========================================================================
    # MATCH
    # ========================================================================

    with rendered[3]:
        shell.heading(
            "Approved requests, matched against the bank feed",
            help="Matching is deliberately conservative: exact amount, outgoing only, "
            "within 45 days. Candidates are offered for confirmation rather than "
            "linked automatically.",
        )

        candidates = reimb.find_matches(requests, transactions)
        if not candidates:
            approved = (
                requests[requests["status"] == reimb.APPROVED] if not requests.empty else pd.DataFrame()
            )
            if approved.empty:
                shell.empty_state("No approved requests awaiting payment")
            else:
                shell.empty_state(
                    f"{len(approved)} approved request(s), no matching transaction found",
                    "The payment may not have cleared yet, or the amount differs.",
                )
        else:
            shell.dataframe(
                reimb.candidates_frame(candidates),
                column_config={
                    "Amount": st.column_config.NumberColumn(format="dollar"),
                    "Date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                    "Confidence": st.column_config.ProgressColumn(
                        min_value=0.0, max_value=1.0, format="%.0f%%"
                    ),
                },
            )

            options = {
                f"Request {c.request_id} → transaction {c.transaction_id} "
                f"({format_currency(c.amount)}, {c.confidence:.0%})": c
                for c in candidates
            }
            chosen = st.selectbox("Confirm a match", list(options), key="match_choice")
            if st.button("Link and mark paid", type="primary", disabled=identity.read_only):
                candidate = options[chosen]
                result = repo.link_reimbursement(
                    candidate.request_id, candidate.transaction_id, identity.email
                )
                if result.error:
                    shell.error_state("Could not link", result.error)
                else:
                    shell.notify(
                        "success",
                        f"Request {candidate.request_id} marked paid against "
                        f"transaction {candidate.transaction_id}.",
                    )
                    st.rerun()

    # ========================================================================
    # RECEIPT COVERAGE
    # ========================================================================

    with rendered[4]:
        st.markdown("#### Receipt coverage")
        coverage = receipts_domain.coverage(transactions, receipts)

        columns = st.columns(4)
        columns[0].metric("Expenses", f"{coverage['expenses']:,}")
        columns[1].metric("With a receipt", f"{coverage['with_receipt']:,}")
        columns[2].metric("Value covered", format_currency(coverage["covered_amount"]))
        columns[3].metric(
            "Coverage",
            f"{coverage['rate']:.1f}%",
            help="Measured by value rather than row count — an audit cares about the "
            "dollars backed by evidence, not the number of receipts.",
        )
        st.progress(min(coverage["rate"] / 100, 1.0))

        st.markdown("#### Attach a receipt to an existing transaction")
        expenses = transactions[transactions["amount"] < 0] if not transactions.empty else pd.DataFrame()
        if expenses.empty:
            shell.empty_state("No expenses recorded")
        else:
            largest = expenses.nlargest(200, expenses["amount"].abs().name or "amount")
            largest = expenses.reindex(expenses["amount"].abs().sort_values(ascending=False).index).head(200)
            choices = {
                f"{int(row['transactionid'])} · {pd.to_datetime(row['transaction_date']):%Y-%m-%d} · "
                f"{format_currency(abs(row['amount']))} · {str(row['details'])[:60]}": int(
                    row["transactionid"]
                )
                for _, row in largest.iterrows()
            }
            target_label = st.selectbox("Transaction", list(choices), key="receipt_txn")
            attach_file = st.file_uploader(
                "Receipt file",
                type=[e.lstrip(".") for e in sorted(receipts_domain.ALLOWED_EXTENSIONS)],
                key="attach_receipt",
            )
            if st.button("Attach receipt", disabled=attach_file is None or identity.read_only):
                data = attach_file.getvalue()
                stored = receipts_domain.store(attach_file.name, data, attach_file.type)
                if not stored.ok:
                    shell.error_state("Could not store the receipt", stored.error or "")
                else:
                    _, result = repo.store_receipt(
                        {
                            "file_name": stored.file_name,
                            "stored_path": stored.stored_path,
                            "content_type": stored.content_type,
                            "byte_size": stored.byte_size,
                            "transaction_id": choices[target_label],
                        },
                        identity.email,
                    )
                    if result.error:
                        shell.error_state("Could not record the receipt", result.error)
                    else:
                        st.success("Receipt attached.")
                        st.rerun()
