"""
Regression tests, one per finding from the architecture review.

Each test is named for the finding it locks down, so a failure points straight
at what broke and why it mattered.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.config.categories import (
    ACCOUNTS,
    committee_label,
    legacy_account_values,
    normalize_account,
    parse_committee_label,
)
from ais_fmd.domain import quality
from ais_fmd.domain.budgets import budget_vs_actual
from ais_fmd.domain.dedupe import assign_natural_keys, split_new_and_duplicate
from ais_fmd.domain.money import (
    equals_any,
    format_currency,
    format_delta,
    parse_amount,
    safe_percent,
)


# --- F3: duplicate detection -------------------------------------------------

def _record(date: str, amount: float, details: str, account: str = "Wells Fargo") -> dict:
    return {
        "transaction_date": date,
        "amount": amount,
        "details": details,
        "account": account,
    }


def test_f3_genuine_same_day_repeat_purchases_are_both_kept():
    """
    The original keyed on (details, date) only, so a second identical purchase
    on the same day was silently discarded. Both must survive.
    """
    records = [
        _record("2025-09-16", -4.50, "STARBUCKS 00417 GAINESVILLE FL"),
        _record("2025-09-16", -4.50, "STARBUCKS 00417 GAINESVILLE FL"),
    ]
    result = split_new_and_duplicate(records, existing=[])
    assert len(result.new) == 2, "both genuine purchases should import"
    assert not result.duplicates


def test_f3_reuploading_the_same_file_is_still_rejected():
    """The occurrence ordinal must not let a real re-upload slip through."""
    records = [
        _record("2025-09-16", -4.50, "STARBUCKS 00417"),
        _record("2025-09-16", -4.50, "STARBUCKS 00417"),
    ]
    stored = assign_natural_keys(records)
    second_attempt = split_new_and_duplicate(records, existing=stored)
    assert not second_attempt.new
    assert len(second_attempt.duplicates) == 2


def test_f3_amount_is_part_of_the_key():
    """Same day, same description, different amount -- two distinct transactions."""
    stored = assign_natural_keys([_record("2025-09-16", -4.50, "PUBLIX")])
    incoming = [_record("2025-09-16", -9.75, "PUBLIX")]
    result = split_new_and_duplicate(incoming, existing=stored)
    assert len(result.new) == 1


def test_f16_empty_existing_table_does_not_raise():
    """The original crashed on a fresh database."""
    result = split_new_and_duplicate([_record("2025-01-01", 10.0, "x")], existing=[])
    assert len(result.new) == 1


def test_f16_empty_incoming_batch_is_a_no_op():
    result = split_new_and_duplicate([], existing=[])
    assert result.total == 0


# --- F12: divide by zero -----------------------------------------------------

def test_f12_zero_budget_yields_none_not_infinity():
    assert safe_percent(500, 0) is None
    assert safe_percent(500, None) is None
    assert safe_percent(0, 0) is None


def test_f12_percent_never_poisons_an_axis_range():
    """
    The original produced inf, which then flowed into max(100, series.max()*1.1)
    and destroyed the axis scale for every other committee.
    """
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01", "2025-09-02"]),
            "amount": [-100.0, -50.0],
            "budget_category": pd.array([9, 13], dtype="Int64"),
            "purpose": ["Marketing", "Merch"],
            "account": ["Wells Fargo", "Wells Fargo"],
        }
    )
    budgets = pd.DataFrame(
        {"termid": ["FA25"], "committeeid": [9], "budget_amount": [0.0]}
    )
    terms = pd.DataFrame(
        {
            "TermID": ["FA25"],
            "Semester": ["Fall 2025"],
            "start_date": ["2025-08-20"],
            "end_date": ["2025-12-12"],
        }
    )
    summary = budget_vs_actual(transactions, budgets, terms, "Fall 2025")
    percents = [value for value in summary["% Spent"] if value is not None]
    assert all(value == value and value != float("inf") for value in percents)


# --- F14: delta formatting ---------------------------------------------------

def test_f14_delta_is_formatted_currency_not_a_bare_float():
    assert format_delta(1234.56) == "+$1,234.56"
    assert format_delta(-89.1) == "-$89.10"
    assert format_delta(0) is None


def test_currency_formatting_handles_missing_values():
    assert format_currency(None) == "—"
    assert format_currency(float("nan")) == "—"
    assert format_currency(1234.5) == "$1,234.50"


# --- F18 / money -------------------------------------------------------------

def test_f18_dues_comparison_is_exact_not_float_membership():
    """The original did `amt not in {35.0, 52.5}` on floats."""
    dues = (Decimal("35.00"), Decimal("52.50"))
    assert equals_any("35.00", dues)
    assert equals_any(35, dues)
    assert equals_any("$52.50", dues)
    assert not equals_any(35.01, dues)


def test_f5_unparseable_amount_returns_none_not_zero():
    """
    The original `numeric_amount` returned 0.0 on failure, so a misread column
    became a stream of real $0.00 transactions.
    """
    assert parse_amount("not a number") is None
    assert parse_amount("") is None
    assert parse_amount(None) is None
    assert parse_amount("abc") is None


def test_amount_parsing_handles_real_world_formats():
    assert parse_amount("$1,234.56") == Decimal("1234.56")
    assert parse_amount("-$45.00") == Decimal("-45.00")
    assert parse_amount("(45.00)") == Decimal("-45.00")  # accounting negatives
    assert parse_amount("1,234") == Decimal("1234.00")


# --- F6: committee label round-trip -----------------------------------------

def test_f6_committee_label_round_trips():
    """
    The editor compared int 8 against the string "8 - Meeting Food", so the
    change test was always true and every row was rewritten on every save.
    """
    for committee_id in (1, 4, 5, 17):
        label = committee_label(committee_id)
        assert parse_committee_label(label) == committee_id
    # A retired line (8 Meeting Food, 15 Technology) is labelled as the committee it
    # folded into, so its label round-trips to that committee, not the old line.
    for retired, committee_id in ((8, 5), (15, 6)):
        assert parse_committee_label(committee_label(retired)) == committee_id


def test_f6_parse_handles_blanks_and_garbage():
    assert parse_committee_label("") is None
    assert parse_committee_label(None) is None
    assert parse_committee_label("not a committee") is None
    assert parse_committee_label("999 - Nonexistent") is None


def test_f6_parse_accepts_bare_ids_and_names():
    assert parse_committee_label("4") == 4
    assert parse_committee_label(4) == 4
    assert parse_committee_label("President") == 4
    # Retired line 8 (Meeting Food) folds into Membership (5), by id or by name.
    assert parse_committee_label("8") == 5
    assert parse_committee_label(8) == 5
    assert parse_committee_label("Meeting Food") == 5


# --- F13: account labels -----------------------------------------------------

def test_f13_legacy_wells_folds_onto_canonical_value():
    assert normalize_account("Wells") == "Wells Fargo"
    assert normalize_account("wells fargo") == "Wells Fargo"
    assert normalize_account("Venmo") == "Venmo"
    assert normalize_account("") is None


def test_f13_legacy_values_are_detectable_for_backfill():
    stale = legacy_account_values(["Venmo", "Wells", "Wells Fargo", None, ""])
    assert stale == {"Wells"}
    assert all(value in ACCOUNTS for value in ("Venmo", "Wells Fargo"))


# --- F7: disputed mappings are surfaced, not silently changed ---------------

def test_f7_disputed_mappings_are_reported():
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01"]),
            "amount": [-100.0],
            "details": ["x"],
            "budget_category": pd.array([7], dtype="Int64"),
            "purpose": ["Professional Development"],
            "account": ["Wells Fargo"],
        }
    )
    report = quality.disputed_mapping_report(transactions)
    assert not report.empty
    row = report[report["Purpose"] == "Professional Development"].iloc[0]
    assert row["Transactions"] == 1
    assert "7" in row["Currently mapped to"]


def test_stored_rows_read_back_as_timestamps_are_still_duplicates():
    """
    The Supabase backend returns transaction_date as a pandas Timestamp. On the
    real Fall 2026 statement the Import page called all 277 rows new when 199
    were already stored, because "2026-08-17 00:00:00" != "2026-08-17".
    """
    import pandas as pd

    from ais_fmd.domain.dedupe import natural_key, split_new_and_duplicate

    staged = {"transaction_date": "2026-08-17", "amount": -298.96,
              "details": "PURCHASE AUTHORIZED ON 08/15 TST*MACDINTONS", "account": "Wells Fargo"}
    stored = dict(staged, transaction_date=pd.Timestamp("2026-08-17"))
    result = split_new_and_duplicate([staged], [stored])
    assert result.new == [] and len(result.duplicates) == 1
    # Strings are untouched, so every key already in the database still matches.
    assert natural_key(staged) == natural_key(dict(staged))
