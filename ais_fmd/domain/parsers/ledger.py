"""
Historical ledger parser -- recovers human labels from past treasurers' work.

WHY THIS EXISTS. Everything the system "knew" until now was its own output:
`budget_category` in the transactions table was written by `categorize_frame` at
import time, not by a person. Fitting anything to those labels would train the
model on itself. The Google Drive workbooks ("Temporary FMD", "Copy of AIS
Ledger Workbook") are different -- past treasurers typed the Budget and Purpose
columns by hand, covering Fall 2022 onward. That is genuine ground truth, and it
is the only genuine ground truth the project has.

WHAT TRANSFERS AND WHAT DOES NOT. The labels come with real bank descriptions,
so the same features can be derived from them. But the cards in that era
(0319, 6570, 8648, 2949) belong to officers who have since graduated; the
current roster is 8313/5718/3568/8408. A card weight fitted across both eras
would be meaningless. Every example therefore carries an `era`, and the fitter
scopes card features to it. Merchant names and description shapes -- WIX.COM is
Technology, VENMO CASHOUT is a transfer -- do carry across.

The workbooks are read as the Drive connector renders them: markdown tables.
Several sheets repeat the same rows in different shapes, so rows are keyed and
deduplicated rather than trusted to be distinct.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ...config.categories import LINE_BY_NAME as COMMITTEE_BY_NAME

REQUIRED_COLUMNS = frozenset({"Date", "Amount", "Details", "Budget", "Purpose"})

# Committee names used by past treasurers that no longer exist in
# config.categories. Mapping them silently would rewrite history into today's
# vocabulary, so they are reported as skipped instead and left for a human.
RETIRED_COMMITTEE_NAMES = frozenset({"mixer", "leadership"})

_ESCAPES = re.compile(r"\\")
_BLANKISH = frozenset({"", "blank", "-", "n/a", "na", "none"})


@dataclass
class LedgerImport:
    """Parsed labels plus an account of everything that did not make it."""

    examples: list[dict] = field(default_factory=list)
    skipped_no_label: int = 0
    skipped_no_details: int = 0
    skipped_unknown_committee: dict[str, int] = field(default_factory=dict)
    duplicates: int = 0

    @property
    def total_kept(self) -> int:
        return len(self.examples)

    def summary_line(self) -> str:
        unknown = sum(self.skipped_unknown_committee.values())
        return (
            f"{self.total_kept} labels kept; skipped {self.skipped_no_label} unlabeled, "
            f"{self.skipped_no_details} without a description, {unknown} with an "
            f"unrecognised committee; {self.duplicates} duplicate rows collapsed."
        )


def _clean(value: object) -> str:
    """Strip the backslash escaping the markdown rendering introduces."""
    return _ESCAPES.sub("", str(value or "")).strip()


def _is_blank(text: str) -> bool:
    return text.lower() in _BLANKISH


def parse_amount(text: str) -> float | None:
    cleaned = _clean(text).replace("$", "").replace(",", "").replace(" ", "")
    if not cleaned or _is_blank(cleaned):
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def markdown_tables(content: str, required: frozenset[str] = REQUIRED_COLUMNS) -> list[list[dict]]:
    """Every markdown table in `content` whose header carries all `required` columns."""
    lines = content.split("\n")
    tables: list[list[dict]] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        header = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not required.issubset(set(header)):
            continue

        rows: list[dict] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if not candidate.startswith("|"):
                break
            cells = [cell.strip() for cell in candidate.strip("|").split("|")]
            # The ':-:' alignment row carries no data.
            if set("".join(cells)) <= set(": -"):
                cursor += 1
                continue
            if len(cells) == len(header):
                rows.append(dict(zip(header, cells)))
            cursor += 1

        if rows:
            tables.append(rows)
    return tables


def committee_id_for(name: str) -> int | None:
    committee = COMMITTEE_BY_NAME.get(_clean(name).lower())
    return committee.id if committee else None


def natural_key(date: str, amount: float | None, details: str) -> str:
    """Stable identity for a ledger row, so a re-import cannot duplicate it."""
    basis = f"{_clean(date)}|{amount if amount is not None else ''}|{_clean(details).lower()}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def parse_workbook(
    content: str,
    *,
    source_ref: str,
    era: str,
    labeled_by: str = "historical-ledger",
) -> LedgerImport:
    """Extract every human-labeled row from one rendered workbook."""
    result = LedgerImport()
    seen: set[str] = set()

    for rows in markdown_tables(content):
        for row in rows:
            details = _clean(row.get("Details"))
            budget = _clean(row.get("Budget"))

            if not budget or _is_blank(budget):
                result.skipped_no_label += 1
                continue
            if not details or _is_blank(details):
                result.skipped_no_details += 1
                continue

            committee_id = committee_id_for(budget)
            if committee_id is None:
                key = budget if budget.lower() in RETIRED_COMMITTEE_NAMES else budget
                result.skipped_unknown_committee[key] = (
                    result.skipped_unknown_committee.get(key, 0) + 1
                )
                continue

            date = _clean(row.get("Date"))
            amount = parse_amount(row.get("Amount"))
            key = natural_key(date, amount, details)
            if key in seen:
                result.duplicates += 1
                continue
            seen.add(key)

            result.examples.append(
                {
                    "source": "ledger",
                    "source_ref": source_ref,
                    "era": era,
                    "transaction_date": date,
                    "amount": amount,
                    "details": details,
                    "account": _clean(row.get("Account")) or None,
                    "committee_id": committee_id,
                    "purpose": _clean(row.get("Purpose")) or None,
                    "labeled_by": labeled_by,
                    "natural_key": key,
                }
            )

    return result
