"""Terms, budgets, parsers, reconciliation, and the assistant router."""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain import assistant, reconcile
from ais_fmd.domain.budgets import budget_vs_actual, semester_totals, spending_by_committee
from ais_fmd.domain.parsers import venmo as venmo_parser
from ais_fmd.domain.parsers import wells_fargo as wells_parser
from ais_fmd.domain.terms import (
    attach_semester,
    ordered_semesters,
    previous_semester,
    validate_semester_name,
)


@pytest.fixture
def terms() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "TermID": ["FA24", "SP25", "FA25"],
            "Semester": ["Fall 2024", "Spring 2025", "Fall 2025"],
            "start_date": ["2024-08-21", "2025-01-06", "2025-08-20"],
            "end_date": ["2024-12-13", "2025-05-02", "2025-12-12"],
        }
    )


@pytest.fixture
def transactions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(
                ["2024-09-10", "2025-02-14", "2025-09-05", "2025-09-06", "2030-01-01"]
            ),
            "amount": [100.0, -250.0, -80.0, 500.0, -10.0],
            "details": ["a", "b", "c", "d", "e"],
            "budget_category": pd.array([9, 9, 13, 1, 13], dtype="Int64"),
            "purpose": ["Marketing", "Marketing", "Merch", "Dues", "Merch"],
            "account": ["Venmo", "Wells Fargo", "Wells Fargo", "Venmo", "Wells Fargo"],
        }
    )


# --- Terms -------------------------------------------------------------------

def test_semesters_are_ordered_chronologically(terms):
    assert ordered_semesters(terms) == ["Fall 2024", "Spring 2025", "Fall 2025"]


def test_default_semester_skips_terms_with_no_data(terms, transactions):
    """
    Terms are usually defined a year ahead, so the newest defined term is often
    empty. Opening a page on an empty term looks like the app is broken.
    """
    from ais_fmd.domain.terms import default_semester, default_semester_index

    # `transactions` has nothing in Fall 2025 beyond Sept; the newest populated
    # term is Fall 2025, not any later empty one.
    assert default_semester(transactions, terms) == "Fall 2025"

    later = pd.concat(
        [
            terms,
            pd.DataFrame(
                {
                    "TermID": ["FA26"],
                    "Semester": ["Fall 2026"],
                    "start_date": ["2026-08-20"],
                    "end_date": ["2026-12-12"],
                }
            ),
        ],
        ignore_index=True,
    )
    assert default_semester(transactions, later) == "Fall 2025"
    assert default_semester_index(transactions, later) == 2


def test_default_semester_falls_back_when_nothing_has_data(terms):
    from ais_fmd.domain.terms import default_semester

    empty = pd.DataFrame(columns=["transaction_date", "amount"])
    assert default_semester(empty, terms) == "Fall 2025"


def test_previous_semester(terms):
    assert previous_semester(terms, "Fall 2025") == "Spring 2025"
    assert previous_semester(terms, "Fall 2024") is None
    assert previous_semester(terms, "Nonexistent") is None


def test_attach_semester_is_vectorised_and_correct(terms, transactions):
    tagged = attach_semester(transactions, terms)
    assert tagged.loc[0, "Semester"] == "Fall 2024"
    assert tagged.loc[1, "Semester"] == "Spring 2025"
    assert tagged.loc[2, "Semester"] == "Fall 2025"


def test_dates_outside_every_term_map_to_none(terms, transactions):
    tagged = attach_semester(transactions, terms)
    assert pd.isna(tagged.loc[4, "Semester"])


def test_attach_semester_on_empty_input(terms):
    empty = pd.DataFrame(columns=["transaction_date", "amount"])
    tagged = attach_semester(empty, terms)
    assert "Semester" in tagged.columns
    assert tagged.empty


def test_semester_name_validation():
    assert validate_semester_name("Fall 2026")[0]
    assert not validate_semester_name("Autumn 2026")[0]
    assert not validate_semester_name("Fall")[0]
    assert not validate_semester_name("")[0]


# --- Budgets -----------------------------------------------------------------

def test_semester_totals(terms, transactions):
    totals = semester_totals(transactions, terms, "Fall 2025")
    assert totals["income"] == 500.0
    assert totals["expenses"] == 80.0
    assert totals["net"] == 420.0
    assert totals["count"] == 2


def test_spending_by_committee_excludes_ledger_buckets(terms, transactions):
    """Dues/Transfers/Refunded are bookkeeping buckets, not budgeted committees."""
    spending = spending_by_committee(transactions)
    assert "Dues" not in spending["Committee_Name"].tolist()


def test_budget_status_classification(terms):
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01", "2025-09-02", "2025-09-03"]),
            "amount": [-1200.0, -90.0, -50.0],
            "details": ["a", "b", "c"],
            "budget_category": pd.array([9, 5, 6], dtype="Int64"),
            "purpose": ["Marketing", "Merch", "Technology"],
            "account": ["Wells Fargo"] * 3,
        }
    )
    budgets = pd.DataFrame(
        {
            "termid": ["FA25", "FA25", "FA25"],
            "committeeid": [9, 5, 6],
            "budget_amount": [1000.0, 100.0, 1000.0],
        }
    )
    summary = budget_vs_actual(transactions, budgets, terms, "Fall 2025")
    by_name = dict(zip(summary["Committee_Name"], summary["Status"]))
    assert by_name["Marketing"] == "over"
    assert by_name["Membership"] == "approaching"
    assert by_name["Operations"] == "on track"


# --- Parsers -----------------------------------------------------------------

def test_wells_fargo_parses_the_standard_headerless_layout():
    raw = pd.DataFrame(
        [
            ["09/16/2025", "-142.30", "*", "", "PURCHASE AUTHORIZED ON 09/16 PUBLIX GAINESVILLE FL"],
            ["09/17/2025", "-58.10", "*", "", "PURCHASE AUTHORIZED ON 09/17 CHIPOTLE GAINESVILLE FL"],
        ]
    )
    result = wells_parser.parse(raw, "checking_sep.csv")
    assert result.ok
    assert result.row_count == 2
    assert result.rows["account"].unique().tolist() == ["Wells Fargo"]


def test_wells_fargo_rejects_a_file_whose_layout_changed():
    """
    FINDING F5: the original trusted column positions blindly, so a layout change
    imported a stream of $0.00 rows. It must refuse instead.
    """
    raw = pd.DataFrame([["not a date", "not money", "junk"], ["also not", "nope", "junk"]])
    result = wells_parser.parse(raw, "broken.csv")
    assert not result.ok
    assert result.errors
    assert result.rows.empty


def test_wells_fargo_reports_unreadable_rows_rather_than_zeroing_them():
    raw = pd.DataFrame(
        [
            ["09/16/2025", "-142.30", "*", "", "PURCHASE AUTHORIZED ON 09/16 PUBLIX FL"],
            ["09/17/2025", "-58.10", "*", "", "PURCHASE AUTHORIZED ON 09/17 CHIPOTLE FL"],
            ["09/18/2025", "-12.00", "*", "", "PURCHASE AUTHORIZED ON 09/18 PANERA FL"],
            ["garbage", "garbage", "*", "", "UNREADABLE ROW"],
        ]
    )
    result = wells_parser.parse(raw, "checking.csv")
    assert result.ok
    assert result.rejected_rows == 1
    assert result.warnings
    assert 0.0 not in result.rows["amount"].tolist()


def test_venmo_parses_and_builds_identifying_details():
    raw = pd.DataFrame(
        {
            "ID": ["3421987654321098", "3421987654321099"],
            "Datetime": ["2025-09-10", "2025-09-11"],
            "Note": ["Fall dues", "Formal ticket"],
            "From": ["Ava Mitchell", "Noah Patel"],
            "To": ["UF AIS", "UF AIS"],
            "Amount (total)": ["$35.00", "$55.00"],
        }
    )
    result = venmo_parser.parse(raw, "VenmoStatement_Sep.csv")
    assert result.ok
    assert result.row_count == 2
    assert "Ava Mitchell" in result.rows.iloc[0]["details"]
    assert result.rows["account"].unique().tolist() == ["Venmo"]


def test_venmo_drops_footer_rows():
    raw = pd.DataFrame(
        {
            "ID": ["3421987654321098", ""],
            "Datetime": ["2025-09-10", ""],
            "Note": ["Fall dues", "Account Statement - (@UFAIS)"],
            "From": ["Ava Mitchell", ""],
            "To": ["UF AIS", ""],
            "Amount (total)": ["$35.00", ""],
        }
    )
    result = venmo_parser.parse(raw, "VenmoStatement.csv")
    assert result.row_count == 1


def test_parsers_reject_empty_files():
    assert not wells_parser.parse(pd.DataFrame(), "x.csv").ok
    assert not venmo_parser.parse(pd.DataFrame(), "x.csv").ok


# --- Reconciliation (M1) -----------------------------------------------------

def test_reconciliation_balances_when_the_ledger_agrees():
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-05", "2025-09-20"]),
            "amount": [-100.0, 250.0],
            "account": ["Wells Fargo", "Wells Fargo"],
            "details": ["a", "b"],
        }
    )
    result = reconcile.reconcile_period(
        transactions,
        account="Wells Fargo",
        period_start="2025-09-01",
        period_end="2025-09-30",
        opening_balance=1000.0,
        closing_balance=1150.0,
    )
    assert result.balanced
    assert result.transaction_count == 2


def test_reconciliation_detects_a_missing_transaction():
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-05"]),
            "amount": [-100.0],
            "account": ["Wells Fargo"],
            "details": ["a"],
        }
    )
    result = reconcile.reconcile_period(
        transactions,
        account="Wells Fargo",
        period_start="2025-09-01",
        period_end="2025-09-30",
        opening_balance=1000.0,
        closing_balance=775.20,
    )
    assert not result.balanced
    assert round(float(result.discrepancy), 2) == 124.80
    assert "124.80" in result.explain()


def test_reconciliation_folds_legacy_account_spellings():
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-05"]),
            "amount": [-100.0],
            "account": ["Wells"],  # legacy value
            "details": ["a"],
        }
    )
    result = reconcile.reconcile_period(
        transactions,
        account="Wells Fargo",
        period_start="2025-09-01",
        period_end="2025-09-30",
        opening_balance=1000.0,
        closing_balance=900.0,
    )
    assert result.balanced, "legacy 'Wells' rows must still be counted"


# --- Assistant routing (F10) -------------------------------------------------

def test_f10_committee_spending_question_routes_to_spending_not_the_roster(terms, transactions):
    """
    The original tested for the word 'committee' first and returned the roster,
    so this exact question never reached the spending branch.
    """
    tool = assistant.select_tool(
        "What's the total spending for the Marketing committee this semester?"
    )
    assert tool.name == "committee_spending"


def test_f10_answer_contains_real_numbers(terms, transactions):
    context = assistant.Context(
        transactions=transactions, budgets=pd.DataFrame(), terms=terms,
        scope=assistant.Scope(role="treasurer"),
    )
    answer = assistant.answer_question(
        "How much did the Marketing committee spend in Spring 2025?", context
    )
    assert answer.tool == "committee_spending"
    assert "$250.00" in answer.text


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Which committees are over budget?", "budget_status"),
        ("How much income came in via Venmo?", "income_summary"),
        ("Show me all transactions over $500", "largest_transactions"),
        ("Find all uncategorized transactions", "uncategorized"),
        ("Compare Fall 2024 and Spring 2025", "compare_semesters"),
    ],
)
def test_assistant_routes_each_advertised_example(question, expected):
    assert assistant.select_tool(question).name == expected


def test_assistant_never_raises_on_odd_input(terms, transactions):
    context = assistant.Context(
        transactions=transactions, budgets=pd.DataFrame(), terms=terms
    )
    for question in ["", "   ", "asdfghjkl", "!!!", "spending " * 200]:
        assert isinstance(assistant.answer_question(question, context), assistant.Answer)
