"""
Dues payment checks: is each payment the right price for the day it was made?

Treasury's rule (2026-11-08): dues are a set price that goes up once, on a date
announced on the membership form ("Dues are $50 (The price will increase to $65
on Wednesday, Aug 19 at 11:59pm)"). So a payment is checked against the price
*for its date*, and a payment that is a multiple of the price is someone paying
for themselves and another member.

  early price on or before the increase date   OK
  early price after it                         Underpaid: owes the difference
  late price before the increase date          Overpaid: paid the late price early
  two or more prices added up ($100, $115...)  Covers N people: find the others
  any of these after the late cutoff           Late: dues, or paying for someone?

Candidates are incoming Zelle and Venmo payments in the term's dues window,
plus anything already categorized as dues. The window is `terms.dues_terms`,
the same one the Collected figure uses: it reaches back to the day after the
previous (non-summer) term ended, because Fall 2026 dues started arriving
before 08/15 and nobody pays dues for a summer.

Amounts that match nothing are not flagged unless the row is already
categorized as dues: an incoming Zelle for $20 is most likely not dues at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations_with_replacement

import pandas as pd

from .dues import DUES_COMMITTEE_ID, extract_payer, parse_rates
from .money import parse_amount
from .terms import dues_terms

# How many people one payment can plausibly cover before it stops looking like dues.
MAX_PEOPLE = 4

OK = "OK"
UNDERPAID = "Underpaid"
OVERPAID = "Paid late price early"
MULTIPLE = "Covers more than one person"
LATE = "Late in the term"
UNEXPECTED = "Unexpected amount"

# Order a treasurer works them in: money owed first.
SEVERITY = {UNDERPAID: 0, MULTIPLE: 1, UNEXPECTED: 2, LATE: 3, OVERPAID: 4, OK: 9}


@dataclass(frozen=True)
class DuesPrices:
    """One term's dues prices and dates, as stored on the terms row."""

    early: Decimal | None
    late: Decimal | None
    increase_date: date | None  # last day the early price applies
    late_cutoff: date | None
    other_rates: tuple[Decimal, ...] = ()  # e.g. the $35 exec rate, valid any day

    @property
    def configured(self) -> bool:
        return self.early is not None and self.late is not None and self.increase_date is not None

    def price_on(self, day: date) -> Decimal | None:
        if not self.configured:
            return None
        return self.early if day <= self.increase_date else self.late


def _decimal(value: object) -> Decimal | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    amount = parse_amount(value)
    return amount if amount is not None and amount > 0 else None


def _day(value: object) -> date | None:
    stamp = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(stamp) else stamp.date()


def prices_for(terms: pd.DataFrame, semester: str) -> DuesPrices | None:
    """The stored prices for `semester`, or None when the term does not exist."""
    if terms is None or terms.empty:
        return None
    row = terms[terms["Semester"].astype(str) == str(semester)]
    if row.empty:
        return None
    record = row.iloc[0].to_dict()
    early = _decimal(record.get("dues_early_price"))
    late = _decimal(record.get("dues_late_price"))
    other = tuple(r for r in parse_rates(record.get("dues_rates")) if r not in {early, late})
    return DuesPrices(
        early=early,
        late=late,
        increase_date=_day(record.get("dues_increase_date")),
        late_cutoff=_day(record.get("dues_late_cutoff")),
        other_rates=other,
    )


def dues_window(terms: pd.DataFrame, semester: str) -> tuple[date, date] | None:
    """The days dues for `semester` arrive in: `terms.dues_terms`, same as Collected."""
    window = dues_terms(terms)
    row = window[window["Semester"].astype(str) == str(semester)] if not window.empty else window
    if row.empty:
        return None
    start, end = row.iloc[0]["start_date"], row.iloc[0]["end_date"]
    if pd.isna(start) or pd.isna(end):
        return None
    return start.date(), end.date()


def people_covered(amount: Decimal, prices: DuesPrices) -> int | None:
    """
    How many people `amount` pays for, if it is two to MAX_PEOPLE prices added up.

    Any mix counts -- $115 is one early and one late payment, which happens when
    someone pays for a friend who missed the deadline.
    """
    singles = sorted({p for p in (prices.early, prices.late, *prices.other_rates) if p})
    for count in range(2, MAX_PEOPLE + 1):
        for combo in combinations_with_replacement(singles, count):
            if sum(combo) == amount:
                return count
    return None


def _is_transfer_in(row: dict) -> bool:
    text = str(row.get("details") or "").lower()
    return "zelle from" in text or str(row.get("account") or "").lower() == "venmo"


def check_payments(transactions: pd.DataFrame, terms: pd.DataFrame, semester: str) -> pd.DataFrame:
    """
    Every candidate dues payment in `semester` with its check, worst first.

    Columns: Date, Payer, Amount, Check, Detail, transaction_id. An empty frame
    when the term has no prices configured -- there is nothing to check against.
    """
    columns = ["Date", "Payer", "Amount", "Check", "Detail", "transaction_id"]
    prices = prices_for(terms, semester)
    window = dues_window(terms, semester) if prices else None
    if prices is None or not prices.configured or window is None or transactions.empty:
        return pd.DataFrame(columns=columns)

    start, end = window
    rows = []
    for row in transactions.to_dict("records"):
        amount = parse_amount(row.get("amount"))
        day = _day(row.get("transaction_date"))
        if amount is None or amount <= 0 or day is None or not (start <= day <= end):
            continue
        categorized_dues = row.get("budget_category") == DUES_COMMITTEE_ID
        if not (categorized_dues or _is_transfer_in(row)):
            continue

        price = prices.price_on(day)
        check, detail = None, ""
        if amount == price or amount in prices.other_rates:
            check = OK
        elif amount == prices.early and day > prices.increase_date:
            check = UNDERPAID
            detail = (
                f"Paid the early price after it went up on {prices.increase_date:%b %d}; "
                f"owes ${prices.late - prices.early:,.2f} more."
            )
        elif amount == prices.late and day <= prices.increase_date:
            check = OVERPAID
            detail = (
                f"Paid the late price before {prices.increase_date:%b %d}; "
                f"overpaid by ${prices.late - prices.early:,.2f}."
            )
        else:
            people = people_covered(amount, prices)
            if people:
                check = MULTIPLE
                detail = (
                    f"Enough for {people} people. Find who else it covers; the memo may "
                    "name them. Not counted as dues until categorized as Membership dues."
                    if not categorized_dues
                    else f"Enough for {people} people. Find who else it covers."
                )
            elif categorized_dues:
                check = UNEXPECTED
                detail = "Categorized as dues, but the amount matches no price or combination."

        if check is None:
            continue  # an incoming transfer that is not dues-shaped: not ours to judge
        if prices.late_cutoff and day > prices.late_cutoff:
            late_note = (
                f"Paid after the {prices.late_cutoff:%b %d} cutoff: late dues, or paying for "
                "someone else?"
            )
            if check == OK:
                check, detail = LATE, late_note
            else:
                detail = f"{detail} {late_note}"

        rows.append(
            {
                "Date": day,
                "Payer": extract_payer(row.get("details")) or "",
                "Amount": float(amount),
                "Check": check,
                "Detail": detail,
                "transaction_id": row.get("transactionid"),
            }
        )

    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        return frame
    frame["_order"] = frame["Check"].map(SEVERITY)
    return frame.sort_values(["_order", "Date"]).drop(columns="_order").reset_index(drop=True)
