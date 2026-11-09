"""
Dues payment checks (domain/dues_checks.py): the price for the payment's date.

Prices mirror Fall 2026 as the membership form announced them: $50, rising to
$65 after Wednesday Aug 19, with $35 as the exec rate.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain import dues_checks as checks

TERMS = pd.DataFrame(
    [
        {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-12", "end_date": "2026-05-01",
         "dues_rates": "35.00,52.50", "dues_early_price": None, "dues_late_price": None,
         "dues_increase_date": None, "dues_late_cutoff": None},
        {"TermID": "SU26", "Semester": "Summer 2026", "start_date": "2026-05-11", "end_date": "2026-08-07",
         "dues_rates": None, "dues_early_price": None, "dues_late_price": None,
         "dues_increase_date": None, "dues_late_cutoff": None},
        {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18",
         "dues_rates": "35.00,50.00,65.00", "dues_early_price": 50.0, "dues_late_price": 65.0,
         "dues_increase_date": "2026-08-19", "dues_late_cutoff": "2026-09-30"},
    ]
)


def _zelle(day: str, amount: float, name: str = "JANE DOE", **extra) -> dict:
    return {
        "transactionid": extra.pop("transactionid", 1),
        "transaction_date": day,
        "amount": amount,
        "details": f"ZELLE FROM {name} ON {day[5:7]}/{day[8:10]} REF # ABC123",
        "budget_category": extra.pop("budget_category", None),
        "account": "Wells Fargo",
    }


def _check(*rows: dict) -> pd.DataFrame:
    return checks.check_payments(pd.DataFrame(list(rows)), TERMS, "Fall 2026")


def test_early_price_on_the_increase_day_is_fine():
    result = _check(_zelle("2026-08-19", 50))
    assert result["Check"].tolist() == [checks.OK]


def test_early_price_after_the_increase_is_underpaid_by_the_difference():
    result = _check(_zelle("2026-08-20", 50))
    assert result["Check"].tolist() == [checks.UNDERPAID]
    assert "$15.00" in result["Detail"].iloc[0]


def test_late_price_before_the_increase_is_flagged_as_overpaid():
    result = _check(_zelle("2026-08-16", 65))
    assert result["Check"].tolist() == [checks.OVERPAID]


def test_late_price_after_the_increase_is_fine():
    assert _check(_zelle("2026-09-02", 65))["Check"].tolist() == [checks.OK]


def test_exec_rate_is_fine_on_any_day():
    assert _check(_zelle("2026-09-02", 35))["Check"].tolist() == [checks.OK]


@pytest.mark.parametrize("amount, people", [(100, 2), (130, 2), (115, 2), (150, 3), (195, 3)])
def test_a_sum_of_prices_covers_several_people(amount, people):
    result = _check(_zelle("2026-08-25", amount))
    assert result["Check"].tolist() == [checks.MULTIPLE]
    assert f"{people} people" in result["Detail"].iloc[0]


def test_payment_after_the_late_cutoff_is_flagged_even_at_the_right_price():
    result = _check(_zelle("2026-10-05", 65))
    assert result["Check"].tolist() == [checks.LATE]


def test_late_cutoff_note_is_added_to_a_payment_already_flagged():
    result = _check(_zelle("2026-10-05", 50))
    assert result["Check"].tolist() == [checks.UNDERPAID]
    assert "cutoff" in result["Detail"].iloc[0]


def test_payments_between_terms_count_toward_the_next_term():
    """Fall 2026 dues started arriving before the 08/15 start; summer is skipped."""
    assert _check(_zelle("2026-08-12", 50))["Check"].tolist() == [checks.OK]
    assert _check(_zelle("2026-07-20", 50))["Check"].tolist() == [checks.OK]


def test_the_roster_counts_payments_made_before_the_term_began():
    """Regression: Roster tagged by plain term dates and showed early payers as unpaid."""
    from ais_fmd.domain import roster

    payments = pd.DataFrame([_zelle("2026-08-12", 50), _zelle("2026-08-20", 65, name="JOHN ROE", transactionid=2)])
    rows = roster.semester_dues_rows(payments.assign(budget_category=1), TERMS, "Fall 2026")
    assert len(rows) == 2


def test_an_incoming_transfer_that_is_not_dues_shaped_is_left_alone():
    assert _check(_zelle("2026-08-25", 20)).empty


def test_an_odd_amount_already_categorized_as_dues_is_flagged():
    result = _check(_zelle("2026-08-25", 20, budget_category=1))
    assert result["Check"].tolist() == [checks.UNEXPECTED]


def test_card_purchases_and_refunds_are_not_candidates():
    purchase = {"transactionid": 2, "transaction_date": "2026-08-25", "amount": 50,
                "details": "PURCHASE AUTHORIZED ON 08/24 CANVA", "budget_category": None,
                "account": "Wells Fargo"}
    outgoing = _zelle("2026-08-25", -50)
    assert _check(purchase, outgoing).empty


def test_no_prices_configured_means_no_checks():
    terms = TERMS.assign(dues_early_price=None, dues_late_price=None, dues_increase_date=None)
    assert checks.check_payments(pd.DataFrame([_zelle("2026-08-20", 50)]), terms, "Fall 2026").empty


def test_money_owed_sorts_first():
    result = _check(_zelle("2026-08-16", 50), _zelle("2026-08-25", 100), _zelle("2026-08-20", 50))
    assert result["Check"].tolist() == [checks.UNDERPAID, checks.MULTIPLE, checks.OK]


def test_prices_are_read_from_the_terms_row():
    prices = checks.prices_for(TERMS, "Fall 2026")
    assert prices.early == Decimal("50") and prices.late == Decimal("65")
    assert prices.increase_date == date(2026, 8, 19)
    assert prices.other_rates == (Decimal("35.00"),)
