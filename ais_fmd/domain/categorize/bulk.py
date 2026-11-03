"""
Module M4b -- bulk merchant proposals (P3).

THE PROBLEM THIS SOLVES. Merchant memory works, and is empty. Working it one
row at a time through the review queue means 330 decisions. Grouping the same
rows by merchant turns that into 97 decisions, each of which is permanent: once
"PUBLIX GAINESVILLE" is mapped, every future Publix row resolves for free.

THE COST ARGUMENT. The original Colab script sent every transaction to a model
on every run and then overrode most of its answers in Python. This module asks
the question at the *merchant* level instead of the row level, and persists the
answer:

    330 rows/run, every run   ->   97 merchants, asked once, then never again

WHAT THIS MODULE WILL NOT DO. It proposes; it does not decide. Which committee a
merchant books to is a judgement about real money -- the same class of decision
as the disputed purpose mappings in §8 of the handoff. Every proposal carries
its evidence and a confidence, and `needs_decision` is true for anything the
deterministic signals cannot settle. Writing to the merchants table is a
separate, explicit step that only accepts rows a human has confirmed.

TWO POPULATIONS. The residual is not one thing:

  * ~210 rows are card purchases with a stable merchant name -> merchant memory.
  * ~120 rows are incoming Zelle payments from members, which `merchant_key`
    deliberately refuses to learn from (a rule keyed on a member's name would
    mis-categorise everything else that person ever pays). But their *memo*
    often says what the payment was for -- "HOODIE TSHIRT", "HEADSHOT" -- and
    nothing was reading it. Those are grouped by memo keyword instead.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from ...config.categories import committee_name
from ..money import parse_amount
from .merchants import merchant_key
from .predicates import (
    COMMITTEE_PURPOSE,
    MEETING_WEEKDAYS,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)

KIND_MERCHANT = "merchant"
KIND_MEMO = "transfer-memo"

# The memo sits after the reference code on a Zelle row:
#   "ZELLE FROM PARKER LANE ON 09/20 REF # BACQZL9WFMD1 PARKER LANE HEADSHOT"
_ZELLE_MEMO = re.compile(r"\bref\s*#?\s*\w+\s*(.*)$", re.I)
_WHITESPACE = re.compile(r"\s+")

# Memo keywords that name what an incoming payment was for. Substring matching
# is deliberate: real memos run words together ("PEI CHI WANGHEADSHOT").
MEMO_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("merch", ("hoodie", "tshirt", "t shirt", "shirt", "crewneck", "merch", "sweater")),
    ("headshot", ("headshot", "head shot")),
    ("formal", ("formal",)),
    ("road trip", ("road trip", "roadtrip", "bus", "hotel")),
    ("dues", ("dues", "membership fee")),
)

# Descriptions that are not merchants at all.
DEPOSIT_MARKERS: tuple[str, ...] = ("mobile deposit", "edeposit", "deposit in branch")
RETURN_MARKERS: tuple[str, ...] = ("return on", "refund", "reversal")


@dataclass
class Group:
    """One merchant (or one memo theme) and every residual row belonging to it."""

    key: str
    kind: str
    display_name: str
    rows: list[dict] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def total_amount(self) -> Decimal:
        total = Decimal("0")
        for row in self.rows:
            amount = parse_amount(row.get("amount"))
            if amount is not None:
                total += amount
        return total

    @property
    def all_positive(self) -> bool:
        amounts = [parse_amount(row.get("amount")) for row in self.rows]
        return bool(amounts) and all(a is not None and a > 0 for a in amounts)

    @property
    def all_negative(self) -> bool:
        amounts = [parse_amount(row.get("amount")) for row in self.rows]
        return bool(amounts) and all(a is not None and a < 0 for a in amounts)

    @property
    def weekdays(self) -> Counter:
        return Counter(
            weekday_from_details(row.get("details"), row.get("transaction_date"))
            for row in self.rows
        )

    @property
    def meeting_weekday_share(self) -> float:
        counts = self.weekdays
        known = sum(count for day, count in counts.items() if day != "Unknown")
        if not known:
            return 0.0
        hits = sum(count for day, count in counts.items() if day in MEETING_WEEKDAYS)
        return hits / known

    def sample(self, limit: int = 3) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            text = _WHITESPACE.sub(" ", str(row.get("details") or "")).strip()
            if text and text not in seen:
                seen.append(text)
            if len(seen) >= limit:
                break
        return seen


@dataclass(frozen=True)
class Proposal:
    """A suggested mapping, with the evidence behind it."""

    key: str
    kind: str
    display_name: str
    row_count: int
    total_amount: Decimal
    committee_id: int | None
    confidence: float
    basis: str
    # Rows sharing this merchant that a deterministic rule ALREADY classified,
    # keyed by the committee they are currently booked to. See `overrides`.
    already_classified: dict[int, int] = field(default_factory=dict)

    @property
    def overrides(self) -> dict[int, int]:
        """
        Existing classifications this mapping would silently change.

        THE HAZARD. Merchant memory is consulted *before* the deterministic
        rules, so a merchant rule wins over them. Mapping "publix gainesville"
        to Membership would not only categorise the 31 unresolved Publix rows --
        it would also re-book every Publix row that `rule_meeting_food` had
        already, correctly, assigned to Meeting Food, without anyone asking.

        The row-by-row review queue cannot cause this, because it touches one
        transaction at a time. Bulk mapping can, which is why it is surfaced
        here rather than discovered later in a variance report.
        """
        if self.committee_id is None:
            return {}
        return {
            committee: count
            for committee, count in self.already_classified.items()
            if committee != self.committee_id
        }

    @property
    def override_count(self) -> int:
        return sum(self.overrides.values())

    def override_warning(self) -> str:
        if not self.overrides:
            return ""
        parts = ", ".join(
            f"{count} currently {committee_name(committee)}"
            for committee, count in sorted(self.overrides.items(), key=lambda kv: -kv[1])
        )
        return (
            f"WOULD RE-BOOK {self.override_count} already-classified row(s): {parts} "
            f"-> {self.committee}"
        )

    @property
    def purpose(self) -> str | None:
        return COMMITTEE_PURPOSE.get(self.committee_id) if self.committee_id else None

    @property
    def committee(self) -> str:
        return committee_name(self.committee_id) if self.committee_id else ""

    @property
    def needs_decision(self) -> bool:
        """True when a human must choose before this can be written."""
        return self.committee_id is None or self.confidence < 0.8


def extract_memo(details: object) -> str:
    """The free-text note a member typed on a Zelle payment. '' when absent."""
    if details is None:
        return ""
    text = _WHITESPACE.sub(" ", str(details)).strip()
    match = _ZELLE_MEMO.search(text)
    return match.group(1).strip() if match else ""


def memo_theme(details: object) -> str:
    """Which memo keyword group this payment belongs to, or ''."""
    memo = extract_memo(details).lower()
    if not memo:
        return ""
    for theme, keywords in MEMO_KEYWORDS:
        if any(keyword in memo for keyword in keywords):
            return theme
    return ""


def group_residual(records: list[dict]) -> list[Group]:
    """
    Bucket unresolved rows into things that can be decided once.

    Rows that fit neither bucket are left out: they have no stable identity, so
    there is nothing to learn from them and they belong in the row-by-row queue.
    """
    groups: dict[tuple[str, str], Group] = {}
    names: dict[tuple[str, str], Counter] = {}

    for record in records:
        details = record.get("details")
        key = merchant_key(details)
        if key:
            identity = (KIND_MERCHANT, key)
        else:
            theme = memo_theme(details)
            if not theme:
                continue
            identity = (KIND_MEMO, theme)

        group = groups.get(identity)
        if group is None:
            # The key is already the noise-stripped merchant name, so it is a
            # far better label than the raw row -- every raw description starts
            # with "PURCHASE AUTHORIZED ON 09/13", which identifies nothing.
            group = Group(key=identity[1], kind=identity[0], display_name=identity[1])
            groups[identity] = group
            names[identity] = Counter()
        group.rows.append(record)

    return sorted(groups.values(), key=lambda g: (-g.row_count, g.key))


def propose(group: Group, already: dict[str, dict[int, int]] | None = None) -> Proposal:
    """
    Suggest a committee from local evidence only.

    Confidence is calibrated so that `needs_decision` is honest: anything below
    0.8 is a hint for a human, not an answer. Nothing here books money on its
    own.
    """
    committee_id, confidence, basis = _propose_inner(group)
    return Proposal(
        key=group.key,
        kind=group.kind,
        display_name=group.display_name,
        row_count=group.row_count,
        total_amount=group.total_amount,
        committee_id=committee_id,
        confidence=confidence,
        basis=basis,
        already_classified=dict((already or {}).get(group.key, {})),
    )


def existing_assignments(
    records: list[dict], committee_ids: list[int | None]
) -> dict[str, dict[int, int]]:
    """
    merchant_key -> {committee_id: row count} for rows that ARE already classified.

    This is what makes `Proposal.overrides` possible: without it the tool cannot
    tell that a merchant it is about to map is one the deterministic rules are
    already handling.
    """
    out: dict[str, dict[int, int]] = {}
    for record, committee_id in zip(records, committee_ids):
        if committee_id is None:
            continue
        key = merchant_key(record.get("details"))
        if not key:
            continue
        out.setdefault(key, {})
        out[key][int(committee_id)] = out[key].get(int(committee_id), 0) + 1
    return out


def _propose_inner(group: Group) -> tuple[int | None, float, str]:
    if group.kind == KIND_MEMO:
        return _propose_memo(group)

    # Match on the merchant key, not the raw description. Every raw row begins
    # "PURCHASE AUTHORIZED ON <date>", so matching there made "return on amazon"
    # fail its own rule -- the raw text reads "PURCHASE RETURN AUTHORIZED ON".
    text = group.key.lower()
    joined = " ".join([group.key, *group.sample(5)]).lower()

    # Not a merchant: a deposit into the account.
    if any(marker in text for marker in DEPOSIT_MARKERS) and group.all_positive:
        return (
            None,
            0.0,
            "Bank deposit, not a merchant. Needs a human: which income line this "
            "belongs to changes the books.",
        )

    if any(marker in text for marker in RETURN_MARKERS) and group.all_positive:
        return 17, 0.85, "Positive amount on a return/refund description."

    if looks_like_bar(joined):
        return 5, 0.85, "Bar or liquor merchant (same keyword list the rules use)."

    if looks_like_food_merchant(joined):
        share = group.meeting_weekday_share
        if share >= 0.8:
            return (
                8,
                0.85,
                f"Food merchant, {share:.0%} of dated rows fall on Tue/Wed.",
            )
        if share <= 0.2:
            return (
                None,
                0.4,
                f"Food merchant, but only {share:.0%} of dated rows are Tue/Wed, so "
                f"the meeting-food rule does not apply. Meeting Food (8) or a social "
                f"line -- a human must choose.",
            )
        return (
            None,
            0.5,
            f"Food merchant with mixed weekdays ({share:.0%} Tue/Wed). Likely split "
            f"across two committees; decide per merchant.",
        )

    return None, 0.0, "No local signal. Needs a human or the model pass."


def _propose_memo(group: Group) -> tuple[int | None, float, str]:
    theme = group.key
    evidence = f"{group.row_count} incoming payments whose memo mentions '{theme}'"
    if theme == "merch":
        return 13, 0.8, f"{evidence}. Apparel sold to members."
    if theme == "formal":
        return 18, 0.8, f"{evidence}."
    if theme == "dues":
        return 1, 0.8, f"{evidence}."
    if theme == "road trip":
        return 14, 0.75, f"{evidence}."
    if theme == "headshot":
        return (
            None,
            0.5,
            f"{evidence}. Professional Development (10) and Membership (5) are both "
            f"defensible; nothing in the data settles it.",
        )
    return None, 0.0, evidence


@dataclass
class ProposalReport:
    """What a bulk run found, in the terms a treasurer needs to act on."""

    proposals: list[Proposal]
    residual_rows: int
    grouped_rows: int
    ungrouped_rows: int

    @property
    def confident(self) -> list[Proposal]:
        return [p for p in self.proposals if not p.needs_decision]

    @property
    def needs_decision(self) -> list[Proposal]:
        return [p for p in self.proposals if p.needs_decision]

    @property
    def rows_covered_if_confirmed(self) -> int:
        return sum(p.row_count for p in self.proposals)

    @property
    def rows_covered_by_confident(self) -> int:
        return sum(p.row_count for p in self.confident)

    @property
    def conflicting(self) -> list[Proposal]:
        """Proposals that would re-book rows a rule already classified."""
        return sorted(
            (p for p in self.proposals if p.overrides), key=lambda p: -p.override_count
        )

    def summary_line(self) -> str:
        return (
            f"{self.residual_rows} unresolved rows -> {len(self.proposals)} decisions "
            f"({self.grouped_rows} rows grouped, {self.ungrouped_rows} not groupable). "
            f"{len(self.confident)} proposals are confident, covering "
            f"{self.rows_covered_by_confident} rows; "
            f"{len(self.needs_decision)} need a human."
        )


def build_report(
    residual_records: list[dict],
    *,
    all_records: list[dict] | None = None,
    committee_ids: list[int | None] | None = None,
) -> ProposalReport:
    """
    Group the residual and propose a mapping for each group.

    Pass `all_records` and their assigned `committee_ids` to enable override
    detection -- without them the report cannot warn that a mapping would
    re-book rows the deterministic rules already handle.
    """
    already: dict[str, dict[int, int]] = {}
    if all_records is not None and committee_ids is not None:
        already = existing_assignments(all_records, committee_ids)

    groups = group_residual(residual_records)
    grouped_rows = sum(group.row_count for group in groups)
    return ProposalReport(
        proposals=[propose(group, already) for group in groups],
        residual_rows=len(residual_records),
        grouped_rows=grouped_rows,
        ungrouped_rows=len(residual_records) - grouped_rows,
    )
