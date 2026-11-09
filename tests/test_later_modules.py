"""
Tests for period locking (M10), reimbursements (M8), receipts (M9),
and scenario planning (M14).
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.data.backend import TransactionChange
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import receipts as receipts_domain
from ais_fmd.domain import reimbursements as reimb
from ais_fmd.domain import scenarios
from ais_fmd.domain.dedupe import assign_natural_keys

ACTOR = "tester@sandbox.local"


@pytest.fixture
def backend(tmp_path) -> SqliteBackend:
    backend = SqliteBackend(tmp_path / "later.db")
    from ais_fmd.config.categories import COMMITTEES

    with backend._connect() as connection:  # noqa: SLF001
        connection.executemany(
            "INSERT OR REPLACE INTO committees "
            "(CommitteeID, Committee_Name, Committee_Type) VALUES (?, ?, ?)",
            [(c.id, c.name, c.kind) for c in COMMITTEES],
        )
        connection.commit()
    backend.insert_term("FA25", "Fall 2025", "2025-08-20", "2025-12-12", ACTOR)
    return backend


def transaction(date: str = "2025-09-15", amount: float = -50.0) -> list[dict]:
    return assign_natural_keys(
        [
            {
                "transaction_date": date,
                "amount": amount,
                "details": f"PURCHASE AUTHORIZED ON 09/15 MERCHANT {date}",
                "account": "Wells Fargo",
                "budget_category": 5,
                "purpose": "Meeting Food",
            }
        ]
    )


# --- Period locking (M10) ----------------------------------------------------

def test_locking_a_term_is_recorded_and_audited(backend):
    result = backend.set_term_lock("FA25", True, ACTOR)
    assert result.updated == 1

    terms = backend.fetch_terms()
    assert int(terms.iloc[0]["locked"]) == 1
    assert terms.iloc[0]["locked_by"] == ACTOR

    audit = backend.fetch_audit(50)
    assert (audit["action"] == "lock").any()


def test_locking_an_already_locked_term_is_a_no_op(backend):
    backend.set_term_lock("FA25", True, ACTOR)
    again = backend.set_term_lock("FA25", True, ACTOR)
    assert again.updated == 0
    assert again.unchanged == 1


def test_import_into_a_locked_period_is_refused_entirely(backend):
    backend.set_term_lock("FA25", True, ACTOR)
    receipt = backend.insert_transactions(transaction(), "checking.csv", ACTOR)

    assert not receipt.ok
    assert "closed" in (receipt.error or "").lower()
    assert backend.fetch_transactions().empty, "nothing may be written when the period is closed"


def test_edits_inside_a_locked_period_are_refused(backend):
    backend.insert_transactions(transaction(), "checking.csv", ACTOR)
    stored = backend.fetch_transactions()
    target = int(stored.iloc[0]["transactionid"])

    backend.set_term_lock("FA25", True, ACTOR)
    result = backend.update_transactions(
        [TransactionChange(transaction_id=target, purpose="Merch", budget_category=9)], ACTOR
    )

    assert result.failed == [target]
    assert "closed" in (result.error or "").lower()
    # The stored value must be untouched.
    assert backend.fetch_transactions().iloc[0]["purpose"] == "Meeting Food"


def test_transactions_outside_the_locked_period_are_unaffected(backend):
    backend.insert_term("SP26", "Spring 2026", "2026-01-05", "2026-05-01", ACTOR)
    backend.insert_transactions(transaction("2026-02-10"), "spring.csv", ACTOR)
    backend.set_term_lock("FA25", True, ACTOR)

    stored = backend.fetch_transactions()
    target = int(stored.iloc[0]["transactionid"])
    result = backend.update_transactions(
        [TransactionChange(transaction_id=target, purpose="Merch", budget_category=9)], ACTOR
    )
    assert result.updated == 1


def test_reopening_restores_writability(backend):
    backend.set_term_lock("FA25", True, ACTOR)
    assert not backend.insert_transactions(transaction(), "a.csv", ACTOR).ok

    backend.set_term_lock("FA25", False, ACTOR)
    assert backend.insert_transactions(transaction(), "a.csv", ACTOR).ok


# --- Sponsor name (M20) -------------------------------------------------------

def test_set_transaction_sponsor_name_writes_and_is_audited(backend):
    backend.insert_transactions(transaction(), "a.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])

    result = backend.set_transaction_sponsor_name(target, "Acme Corp", ACTOR)
    assert result.updated == 1
    assert backend.fetch_transactions().iloc[0]["sponsor_name"] == "Acme Corp"

    audit = backend.fetch_audit(50)
    assert ((audit["transaction_id"] == target) & (audit["field"] == "sponsor_name")).any()


def test_set_transaction_sponsor_name_blank_clears_it(backend):
    backend.insert_transactions(transaction(), "a.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])
    backend.set_transaction_sponsor_name(target, "Acme Corp", ACTOR)

    result = backend.set_transaction_sponsor_name(target, "   ", ACTOR)
    assert result.updated == 1
    assert pd.isna(backend.fetch_transactions().iloc[0]["sponsor_name"])


def test_set_transaction_sponsor_name_unchanged_is_a_no_op(backend):
    backend.insert_transactions(transaction(), "a.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])
    backend.set_transaction_sponsor_name(target, "Acme Corp", ACTOR)

    again = backend.set_transaction_sponsor_name(target, "Acme Corp", ACTOR)
    assert again.updated == 0
    assert again.unchanged == 1


def test_set_transaction_sponsor_name_unknown_transaction_fails(backend):
    result = backend.set_transaction_sponsor_name(999_999, "Acme Corp", ACTOR)
    assert result.error and "does not exist" in result.error


def test_set_transaction_sponsor_name_refused_in_a_locked_period(backend):
    backend.insert_transactions(transaction(), "a.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])
    backend.set_term_lock("FA25", True, ACTOR)

    result = backend.set_transaction_sponsor_name(target, "Acme Corp", ACTOR)
    assert result.failed == [target]
    assert "closed" in (result.error or "").lower()
    assert pd.isna(backend.fetch_transactions().iloc[0]["sponsor_name"])


# --- Receipts (M9) -----------------------------------------------------------

@pytest.mark.parametrize(
    "hostile",
    ["../../etc/passwd", "..\\..\\windows\\system32\\x.png", "/absolute/path.png"],
)
def test_filenames_cannot_escape_the_receipts_directory(hostile):
    safe = receipts_domain.safe_filename(hostile)
    assert "/" not in safe and "\\" not in safe
    assert not safe.startswith(".")


def test_only_allowlisted_types_are_accepted():
    assert receipts_domain.validate("receipt.exe", b"data") is not None
    assert receipts_domain.validate("receipt.png", b"data") is None
    assert receipts_domain.validate("receipt.pdf", b"data") is None


def test_empty_and_oversized_files_are_rejected():
    assert receipts_domain.validate("r.png", b"") is not None
    too_big = b"x" * (receipts_domain.MAX_BYTES + 1)
    assert receipts_domain.validate("r.png", too_big) is not None


def test_storing_the_same_receipt_twice_reuses_one_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(tmp_path / "db.sqlite"))
    first = receipts_domain.store("receipt.png", b"same-bytes", "image/png")
    second = receipts_domain.store("receipt.png", b"same-bytes", "image/png")
    assert first.ok and second.ok
    assert first.stored_path == second.stored_path


def test_stored_receipt_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(tmp_path / "db.sqlite"))
    stored = receipts_domain.store("receipt.pdf", b"pdf-bytes", "application/pdf")
    assert stored.ok
    assert receipts_domain.load(stored.stored_path) == b"pdf-bytes"


def test_receipt_coverage_is_measured_by_value():
    transactions = pd.DataFrame(
        {
            "transactionid": [1, 2],
            "amount": [-1000.0, -10.0],
            "details": ["big", "small"],
        }
    )
    receipts = pd.DataFrame({"transaction_id": [1]})
    coverage = receipts_domain.coverage(transactions, receipts)
    # One of two rows, but 99% of the value.
    assert coverage["with_receipt"] == 1
    assert coverage["rate"] == pytest.approx(1000 / 1010 * 100)


# --- Reimbursements (M8) -----------------------------------------------------

def test_request_validation():
    assert reimb.validate_request(0, "food", 5) is not None
    assert reimb.validate_request(-5, "food", 5) is not None
    assert reimb.validate_request(50, "", 5) is not None
    assert reimb.validate_request(50, "food", None) is not None
    assert reimb.validate_request(50, "food", 5) is None


def test_request_lifecycle(backend):
    created = backend.create_reimbursement(
        {
            "requester": "ava@example.edu",
            "committee_id": 5,
            "amount": 42.50,
            "description": "GBM groceries",
            "incurred_on": "2025-09-10",
        },
        ACTOR,
    )
    assert created.updated == 1

    requests = backend.fetch_reimbursements()
    assert len(requests) == 1
    request_id = int(requests.iloc[0]["request_id"])
    assert requests.iloc[0]["status"] == reimb.PENDING

    approved = backend.decide_reimbursement(request_id, reimb.APPROVED, ACTOR, "ok")
    assert approved.updated == 1
    assert backend.fetch_reimbursements().iloc[0]["status"] == reimb.APPROVED


def test_matching_requires_an_outgoing_transaction():
    """A reimbursement is money leaving; an incoming payment is never the match."""
    requests = pd.DataFrame(
        [
            {
                "request_id": 1,
                "requester": "ava@example.edu",
                "committee_id": 5,
                "amount": 42.50,
                "status": reimb.APPROVED,
                "incurred_on": "2025-09-10",
                "submitted_at": "2025-09-11",
                "matched_transaction_id": None,
            }
        ]
    )
    incoming_only = pd.DataFrame(
        {
            "transactionid": [1],
            "transaction_date": pd.to_datetime(["2025-09-15"]),
            "amount": [42.50],  # positive
            "details": ["ZELLE FROM SOMEONE"],
        }
    )
    assert reimb.find_matches(requests, incoming_only) == []

    outgoing = incoming_only.assign(amount=[-42.50])
    matches = reimb.find_matches(requests, outgoing)
    assert len(matches) == 1
    assert matches[0].transaction_id == 1


def test_matching_respects_the_date_window():
    requests = pd.DataFrame(
        [
            {
                "request_id": 1,
                "requester": "ava@example.edu",
                "committee_id": 5,
                "amount": 42.50,
                "status": reimb.APPROVED,
                "incurred_on": "2025-09-10",
                "submitted_at": "2025-09-11",
                "matched_transaction_id": None,
            }
        ]
    )
    far_future = pd.DataFrame(
        {
            "transactionid": [1],
            "transaction_date": pd.to_datetime(["2026-06-01"]),
            "amount": [-42.50],
            "details": ["PAYMENT"],
        }
    )
    assert reimb.find_matches(requests, far_future) == []


def test_already_claimed_transactions_are_not_offered_twice():
    requests = pd.DataFrame(
        [
            {
                "request_id": 1, "requester": "a@x.edu", "committee_id": 5,
                "amount": 42.50, "status": reimb.APPROVED, "incurred_on": "2025-09-10",
                "submitted_at": "2025-09-11", "matched_transaction_id": None,
            },
            {
                "request_id": 2, "requester": "b@x.edu", "committee_id": 5,
                "amount": 42.50, "status": reimb.PAID, "incurred_on": "2025-09-10",
                "submitted_at": "2025-09-11", "matched_transaction_id": 1,
            },
        ]
    )
    transactions = pd.DataFrame(
        {
            "transactionid": [1],
            "transaction_date": pd.to_datetime(["2025-09-15"]),
            "amount": [-42.50],
            "details": ["PAYMENT"],
        }
    )
    assert reimb.find_matches(requests, transactions) == []


def test_only_approved_requests_are_matched():
    requests = pd.DataFrame(
        [
            {
                "request_id": 1, "requester": "a@x.edu", "committee_id": 5,
                "amount": 42.50, "status": reimb.PENDING, "incurred_on": "2025-09-10",
                "submitted_at": "2025-09-11", "matched_transaction_id": None,
            }
        ]
    )
    transactions = pd.DataFrame(
        {
            "transactionid": [1],
            "transaction_date": pd.to_datetime(["2025-09-15"]),
            "amount": [-42.50],
            "details": ["PAYMENT"],
        }
    )
    assert reimb.find_matches(requests, transactions) == []


def test_summarize_counts_outstanding_value():
    requests = pd.DataFrame(
        [
            {"request_id": 1, "status": reimb.PENDING, "amount": 10.0},
            {"request_id": 2, "status": reimb.APPROVED, "amount": 20.0},
            {"request_id": 3, "status": reimb.PAID, "amount": 100.0},
        ]
    )
    summary = reimb.summarize(requests)
    assert summary[reimb.PENDING] == 1
    assert summary["outstanding_amount"] == pytest.approx(30.0)


# --- Scenario planning (M14) -------------------------------------------------

@pytest.fixture
def planning_data():
    terms = pd.DataFrame(
        {
            "TermID": ["FA25"],
            "Semester": ["Fall 2025"],
            "start_date": ["2025-08-20"],
            "end_date": ["2025-12-12"],
        }
    )
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(
                ["2025-09-01", "2025-09-02", "2025-09-10", "2025-09-11"]
            ),
            "amount": [35.0, 35.0, -400.0, -100.0],
            "details": [
                "ZELLE FROM AVA MITCHELL ON 09/01 REF # A DUES",
                "ZELLE FROM NOAH PATEL ON 09/02 REF # B DUES",
                "PURCHASE AUTHORIZED ON 09/10 PUBLIX FL",
                "PURCHASE AUTHORIZED ON 09/11 VISTAPRINT FL",
            ],
            "budget_category": pd.array([1, 1, 5, 9], dtype="Int64"),
            "purpose": ["Dues", "Dues", "Meeting Food", "Merch"],
            "account": ["Wells Fargo"] * 4,
        }
    )
    budgets = pd.DataFrame(
        {"termid": ["FA25", "FA25"], "committeeid": [5, 9], "budget_amount": [500.0, 500.0]}
    )
    return transactions, budgets, terms


def test_baseline_reflects_actuals(planning_data):
    transactions, budgets, terms = planning_data
    baseline = scenarios.build_baseline(transactions, budgets, terms, "Fall 2025")
    assert baseline.income == pytest.approx(70.0)
    assert baseline.expenses == pytest.approx(500.0)
    assert baseline.dues_payers == 2
    assert baseline.dues_rate == pytest.approx(35.0)
    assert baseline.committee_spend[5] == pytest.approx(400.0)


def test_raising_dues_raises_projected_income(planning_data):
    transactions, budgets, terms = planning_data
    baseline = scenarios.build_baseline(transactions, budgets, terms, "Fall 2025")

    same = scenarios.project(baseline, scenarios.Scenario(dues_rate=35.0, expected_members=2))
    higher = scenarios.project(baseline, scenarios.Scenario(dues_rate=50.0, expected_members=2))
    assert higher.projected_income > same.projected_income
    assert higher.projected_net > same.projected_net


def test_cutting_a_committee_reduces_projected_expenses(planning_data):
    transactions, budgets, terms = planning_data
    baseline = scenarios.build_baseline(transactions, budgets, terms, "Fall 2025")

    cut = scenarios.project(
        baseline,
        scenarios.Scenario(dues_rate=35.0, expected_members=2, committee_multipliers={5: 0.5}),
    )
    assert cut.committee_projection[5] == pytest.approx(200.0)
    assert cut.projected_expenses < baseline.expenses


def test_break_even_member_count(planning_data):
    transactions, budgets, terms = planning_data
    baseline = scenarios.build_baseline(transactions, budgets, terms, "Fall 2025")

    needed = scenarios.break_even_members(
        baseline, scenarios.Scenario(dues_rate=50.0, expected_members=2)
    )
    assert needed is not None and needed >= 1

    # Free dues can never close a gap.
    assert (
        scenarios.break_even_members(
            baseline, scenarios.Scenario(dues_rate=0.0, expected_members=2)
        )
        is None
    )


def test_comparison_frame_balances(planning_data):
    transactions, budgets, terms = planning_data
    baseline = scenarios.build_baseline(transactions, budgets, terms, "Fall 2025")
    projection = scenarios.project(
        baseline, scenarios.Scenario(dues_rate=40.0, expected_members=10)
    )
    frame = scenarios.comparison_frame(projection)

    net_row = frame[frame["Line"] == "Net"].iloc[0]
    income_row = frame[frame["Line"] == "Total income"].iloc[0]
    expense_row = frame[frame["Line"] == "Total expenses"].iloc[0]
    assert net_row["Scenario"] == pytest.approx(
        income_row["Scenario"] - expense_row["Scenario"]
    )
