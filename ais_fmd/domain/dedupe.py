"""
Duplicate detection.

FINDING F3. The original key was (details, date). Venmo rows survived that
because their details string embeds a transaction ID, but Wells Fargo details
are the raw bank description -- so two genuine purchases at the same merchant,
same day, same amount collapsed into one and the second was silently dropped.

The naive fix (add amount to the key) trades one failure for another: it stops
losing data, but it also stops catching re-uploads of a file containing a
genuinely repeated charge.

The key carries an **occurrence ordinal**, assigned batch-locally from zero, and
matching uses multiset semantics: a row is a duplicate when its ordinal falls
within the count already stored for that identity. So:

  * Two real $4.50 coffees on the same day, nothing stored: ordinals 0 and 1,
    both beyond the stored count of 0, so both import.
  * Re-uploading that statement: the same two rows get ordinals 0 and 1 again,
    both now within the stored count of 2, so both are rejected.
  * A statement containing a third genuine repeat: ordinals 0, 1, 2 -- the first
    two are already accounted for, the third imports.

The question is therefore not "does this row exist" but "do we already have this
many of it", which is the only formulation that separates a re-upload from a
genuine repeat purchase using content alone.

The key is written to a column with a unique index, so the database enforces it
rather than a pandas filter that only runs when someone remembers to call it.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Sequence

import pandas as pd

from ..config.categories import normalize_account
from .money import parse_amount

_WHITESPACE = re.compile(r"\s+")


def _normalize_details(details: object) -> str:
    text = "" if details is None else str(details)
    return _WHITESPACE.sub(" ", text).strip().lower()


def _date_text(value: object) -> str:
    """
    A transaction date as the parsers write it: "YYYY-MM-DD".

    Stored rows can come back as timestamps -- the Supabase backend converts the
    column with `pd.to_datetime` -- and `str()` of one is "2026-08-17 00:00:00",
    which matches no freshly parsed row. On the real Fall 2026 statement that
    made the Import page call all 277 rows new when 199 were already stored (the
    database's unique key still refused them). A midnight timestamp is reduced to
    its date; strings are left exactly as they were, so no stored key changes.
    """
    if value is None or value is pd.NaT:
        return ""
    if isinstance(value, datetime):
        if value.time() == time(0):
            return value.strftime("%Y-%m-%d")
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def identity_tuple(record: dict) -> tuple[str, str, str, str]:
    """The part of a transaction that makes it 'the same transaction'."""
    amount = parse_amount(record.get("amount"))
    amount_text = "none" if amount is None else f"{amount:.2f}"
    return (
        _date_text(record.get("transaction_date") or None),
        amount_text,
        _normalize_details(record.get("details")),
        str(normalize_account(record.get("account")) or "").lower(),
    )


def natural_key(record: dict, occurrence: int = 0) -> str:
    """Stable content hash for a transaction at a given occurrence ordinal."""
    parts = identity_tuple(record) + (str(occurrence),)
    joined = "␟".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def assign_natural_keys(records: Sequence[dict]) -> list[dict]:
    """
    Return copies of `records` with a `natural_key` field populated.

    Ordinals are assigned **batch-locally**, starting at zero for each identity.
    That is what makes re-uploading a file reproduce exactly the keys it produced
    the first time -- which is how a re-upload is detected at all.
    """
    seen: Counter[tuple[str, str, str, str]] = Counter()
    out: list[dict] = []
    for record in records:
        identity = identity_tuple(record)
        occurrence = seen[identity]
        seen[identity] += 1
        enriched = dict(record)
        enriched["natural_key"] = natural_key(record, occurrence)
        out.append(enriched)
    return out


@dataclass
class DedupeResult:
    new: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.duplicates)


def split_new_and_duplicate(
    records: Sequence[dict],
    existing: Sequence[dict] = (),
) -> DedupeResult:
    """
    Partition incoming records into genuinely new rows and duplicates.

    FINDING F16. The original crashed on an empty transactions table -- it
    assigned a column to a column-less DataFrame and then read a different
    column that did not exist. An empty `existing` is simply the case where
    nothing is a duplicate.
    """
    if not records:
        return DedupeResult()

    # How many rows of each identity are already stored. Multiset semantics:
    # the question is not "does this row exist" but "do we already have this
    # many of it".
    existing_counts: Counter[tuple[str, str, str, str]] = Counter(
        identity_tuple(row) for row in existing
    )

    result = DedupeResult()
    batch_index: Counter[tuple[str, str, str, str]] = Counter()

    for record in records:
        identity = identity_tuple(record)
        ordinal = batch_index[identity]
        batch_index[identity] += 1

        enriched = dict(record)
        enriched["natural_key"] = natural_key(record, ordinal)

        if ordinal < existing_counts[identity]:
            # Position already accounted for by a stored row -- this is a repeat
            # of something we have, so it is a duplicate.
            result.duplicates.append(enriched)
        else:
            # Beyond what is stored: a genuinely additional occurrence. Its
            # ordinal is the next free one, so the key cannot collide.
            result.new.append(enriched)

    return result
