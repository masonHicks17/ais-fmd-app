"""
End-to-end check of the later modules against whatever is in the sandbox.

Runs the full reimbursement lifecycle (submit -> approve -> match -> paid),
exercises period locking against real transactions, and projects a scenario.
Uses a throwaway copy of the database so the app's data is left alone.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ais_fmd import settings
from ais_fmd.data.backend import TransactionChange
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import receipts as receipts_domain
from ais_fmd.domain import reimbursements as reimb
from ais_fmd.domain import scenarios
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import default_semester, ordered_semesters

ACTOR = "verify@sandbox.local"
checks: dict[str, bool] = {}


def main() -> int:
    live = settings.sandbox_db_path()
    if not live.exists():
        print("No sandbox database. Run the app or the loader first.")
        return 1

    tmp = Path(tempfile.mkdtemp()) / "copy.db"
    shutil.copy(live, tmp)
    backend = SqliteBackend(tmp)
    print(f"working on a copy: {tmp}\n")

    transactions = backend.fetch_transactions()
    terms = backend.fetch_terms()
    budgets = backend.fetch_budgets()
    print(f"transactions: {len(transactions):,}   terms: {len(terms)}")

    # ---------------------------------------------------------------- M8/M9
    print("\n=== M8 reimbursements + M9 receipts ===")

    stored = receipts_domain.store("gbm_groceries.png", b"fake-receipt-bytes", "image/png")
    print(f"[receipt] stored ok={stored.ok} size={stored.byte_size}")
    checks["receipt stored"] = stored.ok

    receipt_id, _ = backend.store_receipt(
        {
            "file_name": stored.file_name,
            "stored_path": stored.stored_path,
            "content_type": stored.content_type,
            "byte_size": stored.byte_size,
        },
        ACTOR,
    )

    # Pick a real outgoing transaction to reimburse against.
    outgoing = transactions[transactions["amount"] < 0]
    target = outgoing.iloc[len(outgoing) // 2]
    amount = abs(float(target["amount"]))
    incurred = pd.to_datetime(target["transaction_date"]) - pd.Timedelta(days=4)

    backend.create_reimbursement(
        {
            "requester": "ava.mitchell@ufl.edu",
            "committee_id": 5,
            "amount": amount,
            "description": "Groceries for GBM",
            "incurred_on": incurred.strftime("%Y-%m-%d"),
            "receipt_id": receipt_id,
        },
        ACTOR,
    )
    requests = backend.fetch_reimbursements()
    request_id = int(requests.iloc[0]["request_id"])
    print(f"[request] created #{request_id} for {format_currency(amount)}")
    checks["request created"] = len(requests) == 1

    print(f"[match]   pending requests are not matched: {not reimb.find_matches(requests, transactions)}")
    checks["pending not matched"] = not reimb.find_matches(requests, transactions)

    backend.decide_reimbursement(request_id, reimb.APPROVED, ACTOR, "receipt checked")
    requests = backend.fetch_reimbursements()
    candidates = reimb.find_matches(requests, transactions)
    print(f"[match]   candidates after approval: {len(candidates)}")
    checks["approved finds a match"] = len(candidates) > 0

    if candidates:
        best = candidates[0]
        print(
            f"          best: txn {best.transaction_id} "
            f"{format_currency(abs(best.amount))} conf {best.confidence:.0%} — {best.reason}"
        )
        backend.link_reimbursement_to_transaction(request_id, best.transaction_id, ACTOR)
        after = backend.fetch_reimbursements().iloc[0]
        print(f"[match]   status now: {after['status']}")
        checks["marked paid"] = after["status"] == reimb.PAID

        again = reimb.find_matches(backend.fetch_reimbursements(), transactions)
        checks["claimed txn not reoffered"] = all(
            c.transaction_id != best.transaction_id for c in again
        )

    coverage = receipts_domain.coverage(transactions, backend.fetch_receipts())
    print(f"[receipt] coverage {coverage['rate']:.2f}% of expense value")

    # ------------------------------------------------------------------ M10
    print("\n=== M10 period locking ===")
    populated = default_semester(transactions, terms)
    term_row = terms[terms["Semester"] == populated].iloc[0]
    term_id = term_row["TermID"]
    print(f"closing {term_id} ({populated})")

    backend.set_term_lock(term_id, True, ACTOR)

    in_term = transactions[
        (pd.to_datetime(transactions["transaction_date"]) >= pd.to_datetime(term_row["start_date"]))
        & (pd.to_datetime(transactions["transaction_date"]) <= pd.to_datetime(term_row["end_date"]))
    ]
    print(f"transactions inside it: {len(in_term)}")

    if not in_term.empty:
        victim = int(in_term.iloc[0]["transactionid"])
        before = backend.fetch_transactions()
        before_purpose = before[before["transactionid"] == victim].iloc[0]["purpose"]

        result = backend.update_transactions(
            [TransactionChange(transaction_id=victim, purpose="Merch", budget_category=9)], ACTOR
        )
        after = backend.fetch_transactions()
        after_purpose = after[after["transactionid"] == victim].iloc[0]["purpose"]

        def same(a, b) -> bool:
            """NaN != NaN, so a plain == reports an unchanged null as changed."""
            if pd.isna(a) and pd.isna(b):
                return True
            return a == b

        unchanged = same(before_purpose, after_purpose)
        print(f"[lock]    edit refused: {victim in result.failed}")
        print(f"[lock]    value unchanged: {unchanged} ({before_purpose!r} -> {after_purpose!r})")
        checks["locked edit refused"] = victim in result.failed
        checks["locked value unchanged"] = unchanged

    from ais_fmd.domain.dedupe import assign_natural_keys

    fresh = assign_natural_keys(
        [
            {
                "transaction_date": pd.to_datetime(term_row["start_date"]).strftime("%Y-%m-%d"),
                "amount": -12.34,
                "details": "SHOULD NOT IMPORT INTO A CLOSED TERM",
                "account": "Wells Fargo",
                "budget_category": 5,
                "purpose": "Meeting Food",
            }
        ]
    )
    receipt = backend.insert_transactions(fresh, "blocked.csv", ACTOR)
    print(f"[lock]    import refused: {not receipt.ok}  ({receipt.error})")
    checks["locked import refused"] = not receipt.ok

    backend.set_term_lock(term_id, False, ACTOR)
    reopened = backend.insert_transactions(fresh, "blocked.csv", ACTOR)
    print(f"[lock]    import succeeds after reopening: {reopened.ok}")
    checks["reopen restores writes"] = reopened.ok

    # ------------------------------------------------------------------ M14
    print("\n=== M14 scenario planner ===")
    busiest = None
    best_count = -1
    for semester in ordered_semesters(terms):
        baseline = scenarios.build_baseline(transactions, budgets, terms, semester)
        if baseline.dues_payers > best_count:
            busiest, best_count = semester, baseline.dues_payers

    baseline = scenarios.build_baseline(transactions, budgets, terms, busiest)
    print(
        f"baseline {busiest}: {format_currency(baseline.income)} in, "
        f"{format_currency(baseline.expenses)} out, {baseline.dues_payers} payers "
        f"at {format_currency(baseline.dues_rate)}"
    )

    current = scenarios.project(
        baseline,
        scenarios.Scenario(dues_rate=baseline.dues_rate, expected_members=baseline.dues_payers),
    )
    raised = scenarios.project(
        baseline,
        scenarios.Scenario(
            dues_rate=baseline.dues_rate + 10,
            expected_members=baseline.dues_payers,
            committee_multipliers={cid: 0.9 for cid in baseline.committee_spend},
        ),
    )
    print(f"  as-is        : net {format_currency(current.projected_net)}")
    print(f"  +$10 dues,   : net {format_currency(raised.projected_net)}")
    print(f"  -10% spending  change {format_currency(raised.net_change - current.net_change)}")
    checks["scenario improves net"] = raised.projected_net > current.projected_net

    needed = scenarios.break_even_members(baseline, scenarios.Scenario(baseline.dues_rate, 0))
    print(f"  break-even members at {format_currency(baseline.dues_rate)}: {needed}")

    print("\n=== CHECKS ===")
    for label, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
