"""Tests for dues (M7), alerts (M6), and the board pack (M12)."""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain import alerts as alerts_domain
from ais_fmd.domain import dues as dues_domain
from ais_fmd.domain import report as report_domain


@pytest.fixture
def terms() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "TermID": ["FA24", "FA25"],
            "Semester": ["Fall 2024", "Fall 2025"],
            "start_date": ["2024-08-21", "2025-08-20"],
            "end_date": ["2024-12-13", "2025-12-12"],
        }
    )


def zelle(name: str, note: str = "DUES") -> str:
    return f"ZELLE FROM {name} ON 09/05 REF # JFR0TQ6QPUIM {note}"


@pytest.fixture
def transactions() -> pd.DataFrame:
    rows = [
        ("2025-09-01", 35.00, zelle("AVA MITCHELL"), 1, "Dues"),
        ("2025-09-02", 35.00, zelle("NOAH PATEL"), 1, "Dues"),
        ("2025-09-03", 52.50, zelle("SOFIA RAMIREZ"), 1, "Dues"),
        # Uncategorized but amount-signature dues -- must still be counted.
        ("2025-09-04", 35.00, zelle("LIAM CHEN"), None, None),
        # Same member paying twice.
        ("2025-09-05", 35.00, zelle("AVA MITCHELL"), 1, "Dues"),
        # Not dues.
        ("2025-09-10", -420.00, "PURCHASE AUTHORIZED ON 09/10 PUBLIX FL", 5, "Meeting Food"),
        ("2024-09-01", 35.00, zelle("PRIOR MEMBER"), 1, "Dues"),
    ]
    return pd.DataFrame(
        {
            "transaction_date": pd.to_datetime([r[0] for r in rows]),
            "amount": [r[1] for r in rows],
            "details": [r[2] for r in rows],
            "budget_category": pd.array([r[3] for r in rows], dtype="Int64"),
            "purpose": [r[4] for r in rows],
            "account": ["Wells Fargo"] * len(rows),
        }
    )


# --- Dues (M7) ---------------------------------------------------------------

def test_payer_name_recovered_from_zelle_description():
    assert dues_domain.extract_payer(zelle("AVA MITCHELL")) == "Ava Mitchell"


def test_payer_recovered_from_venmo_pipe_format():
    details = "3421987654321098 | Fall dues | Noah Patel | UF AIS"
    assert dues_domain.extract_payer(details) == "Noah Patel"


def test_payer_is_none_when_undeterminable():
    assert dues_domain.extract_payer("PURCHASE AUTHORIZED ON 09/10 PUBLIX FL") is None
    assert dues_domain.extract_payer(None) is None


def test_uncategorized_dues_are_still_counted(transactions, terms):
    """
    Collection must not understate purely because someone has not worked the
    review queue, so amount-signature payments count even when unassigned.
    """
    summary = dues_domain.summarize(transactions, terms, "Fall 2025")
    assert summary.payment_count == 5
    assert summary.total_collected == pytest.approx(192.50)


def test_repeat_payments_aggregate_to_one_member(transactions, terms):
    summary = dues_domain.summarize(transactions, terms, "Fall 2025")
    assert summary.payment_count == 5
    assert summary.unique_payers == 4  # Ava paid twice

    roster = dues_domain.payer_roster(transactions, terms, "Fall 2025")
    ava = roster[roster["Payer"] == "Ava Mitchell"].iloc[0]
    assert ava["Payments"] == 2
    assert ava["Paid"] == pytest.approx(70.00)


def test_expenses_are_never_counted_as_dues(transactions, terms):
    dues = dues_domain.dues_transactions(transactions)
    assert (dues["amount"] > 0).all()


def test_multiple_rates_are_reported(transactions, terms):
    summary = dues_domain.summarize(transactions, terms, "Fall 2025")
    assert set(summary.rates) == {35.00, 52.50}
    assert summary.rates[35.00] == 4


def test_collection_curve_is_cumulative_and_day_indexed(transactions, terms):
    curve = dues_domain.collection_curve(transactions, terms, "Fall 2025")
    assert not curve.empty
    assert curve["Cumulative"].is_monotonic_increasing
    assert (curve["Day"] >= 0).all()
    assert curve["Cumulative"].iloc[-1] == pytest.approx(192.50)


def test_outstanding_gap():
    gap = dues_domain.outstanding(collected_payers=30, expected_members=50)
    assert gap["outstanding"] == 20
    assert gap["rate"] == pytest.approx(60.0)
    # Never negative when more people pay than expected.
    assert dues_domain.outstanding(60, 50)["outstanding"] == 0
    assert dues_domain.outstanding(5, 0)["rate"] == 0.0


def test_dues_on_empty_data(terms):
    empty = pd.DataFrame(
        columns=["transaction_date", "amount", "details", "budget_category", "purpose", "account"]
    )
    summary = dues_domain.summarize(empty, terms, "Fall 2025")
    assert summary.payment_count == 0
    assert summary.total_collected == 0.0


# --- Alerts (M6) -------------------------------------------------------------

def test_over_budget_raises_a_critical_alert(transactions, terms):
    budgets = pd.DataFrame(
        {"termid": ["FA25"], "committeeid": [5], "budget_amount": [100.0]}
    )
    alerts = alerts_domain.evaluate(transactions, budgets, terms, semester="Fall 2025")
    over = [a for a in alerts if a.code == "budget_over"]
    assert len(over) == 1
    assert over[0].severity == "critical"
    assert "Membership" in over[0].title


def test_uncategorized_backlog_alert_scales_with_share(transactions, terms):
    alerts = alerts_domain.evaluate(transactions, pd.DataFrame(), terms, semester="Fall 2025")
    backlog = [a for a in alerts if a.code == "uncategorized_backlog"]
    assert len(backlog) == 1


def test_no_alerts_when_everything_is_within_budget(terms):
    clean = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01"]),
            "amount": [-50.0],
            "details": ["PURCHASE AUTHORIZED ON 09/01 PUBLIX FL"],
            "budget_category": pd.array([5], dtype="Int64"),
            "purpose": ["Meeting Food"],
            "account": ["Wells Fargo"],
        }
    )
    budgets = pd.DataFrame(
        {"termid": ["FA25"], "committeeid": [5], "budget_amount": [5000.0]}
    )
    alerts = alerts_domain.evaluate(clean, budgets, terms, semester="Fall 2025")
    assert not [a for a in alerts if a.severity == "critical"]


def test_unreconciled_period_raises_a_critical_alert(transactions, terms):
    balances = pd.DataFrame(
        [
            {
                "account": "Wells Fargo",
                "period_start": "2025-09-01",
                "period_end": "2025-09-30",
                "opening_balance": 1000.0,
                "closing_balance": 9999.0,  # deliberately wrong
            }
        ]
    )
    alerts = alerts_domain.evaluate(
        transactions, pd.DataFrame(), terms, balances, semester="Fall 2025"
    )
    unreconciled = [a for a in alerts if a.code == "unreconciled"]
    assert len(unreconciled) == 1
    assert unreconciled[0].severity == "critical"


def test_alerts_are_sorted_worst_first(transactions, terms):
    budgets = pd.DataFrame({"termid": ["FA25"], "committeeid": [5], "budget_amount": [1.0]})
    alerts = alerts_domain.evaluate(transactions, budgets, terms, semester="Fall 2025")
    ranks = [alert.rank for alert in alerts]
    assert ranks == sorted(ranks)


def test_alerts_on_empty_terms():
    assert alerts_domain.evaluate(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) == []


# --- Board pack (M12) --------------------------------------------------------

def test_board_pack_builds_and_narrates(transactions, terms):
    budgets = pd.DataFrame({"termid": ["FA25"], "committeeid": [5], "budget_amount": [1000.0]})
    pack = report_domain.build(transactions, budgets, terms, "Fall 2025")
    assert pack.semester == "Fall 2025"
    assert pack.narrative
    assert any("Fall 2025" in line for line in pack.narrative)
    assert pack.dues.payment_count == 5


def test_board_pack_renders_valid_standalone_html(transactions, terms):
    budgets = pd.DataFrame({"termid": ["FA25"], "committeeid": [5], "budget_amount": [1000.0]})
    pack = report_domain.build(transactions, budgets, terms, "Fall 2025")
    document = report_domain.to_html(pack)

    assert document.startswith("<!doctype html>")
    assert "</html>" in document
    assert "Fall 2025" in document
    assert "@media print" in document, "must carry a print stylesheet"
    # Self-contained: no external requests.
    assert "http://" not in document and "https://" not in document


def test_board_pack_escapes_description_text(terms):
    """Transaction descriptions are untrusted text and must not inject markup."""
    hostile = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01"]),
            "amount": [-500.0],
            "details": ["<script>alert('x')</script>"],
            "budget_category": pd.array([5], dtype="Int64"),
            "purpose": ["Meeting Food"],
            "account": ["Wells Fargo"],
        }
    )
    pack = report_domain.build(hostile, pd.DataFrame(), terms, "Fall 2025")
    document = report_domain.to_html(pack)
    assert "<script>alert" not in document
    assert "&lt;script&gt;" in document
