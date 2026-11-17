"""
Deterministic categorization rules.

Two changes of substance from the original:

1. **The keyword lists are actually used.** `FOOD_MERCHANT_KEYWORDS` and
   `BAR_LIQUOR_KEYWORDS` were defined in the original module and referenced
   nowhere -- Meeting Food was delegated to the language model even though the
   data needed to decide it locally was sitting right there. Implementing that
   rule in Python is what lets the pipeline stop sending most rows to a model.

2. **Rule order is explicit and matches the documented priority.** The original
   Python override chain ran refund -> consulting -> formal -> dues ->
   membership, while the prompt next to it documented refund -> formal ->
   consulting -> dues -> meeting food -> membership. The ordering lives in one
   list rather than an if/elif chain.

   That order has since been revised twice by treasury rulings (2026-08-24).
   The exact tier now runs memo -> dues -> reimbursement:
   `rule_refund` is gone, because an outgoing transfer is a committee's
   expenditure rather than a ledger bucket, and the memo rule sits ahead of
   dues because the Fall 2026 rates collide with formal ticket amounts. Both
   are explained where the rules are defined.

   Card numbers are no longer rules here at all (2026-09-23). A card on the
   roster decides its row outright, ahead of everything in this module, but
   the roster is data -- `config/card_roster.json`, scoped by officer cohort --
   so it lives in `scoring.CardRegistry` rather than in hard-coded strings like
   the old "card 8408" and "card 8313/5718" markers.

Every function in this module is pure: same input, same output, no I/O. That is
what makes them the highest-value tests in the repository.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Callable

from ...config.categories import (
    ACCOUNT_VENMO,
    committee_label,
    normalize_account,
)
from ..money import equals_any, parse_amount

# --- Reference data ----------------------------------------------------------

# The rates in force when this categorizer was written (Fall 2024). These remain
# the fallback for any caller that supplies no `DuesSchedule`, so behaviour is
# bit-for-bit unchanged unless per-term rates are actually provided.
DUES_AMOUNTS: tuple[Decimal, ...] = (Decimal("35.00"), Decimal("52.50"))

# Venmo takes a cut, so dues paid over Venmo arrive NET of fees. The three
# historical amounts on record are 24.43, 29.34 and 39.14 against gross rates of
# $25, $30 and $40 -- shortfalls of 2.28%, 2.20% and 2.15%.
#
# Those three points do NOT fit one rate-plus-fixed-fee formula to the cent
# (solving from the $25 and $40 pairs predicts 0.67 for $30 where the observed
# fee is 0.66), so the exact schedule Venmo applied cannot be recovered from the
# data available. A bounded window below the gross rate is therefore the honest
# model: a fee can only ever reduce the amount received, and 3% clears all three
# observations with margin while staying far below the gap to the next rate.
VENMO_FEE_TOLERANCE: Decimal = Decimal("0.03")


@dataclass(frozen=True)
class DuesWindow:
    """The dues rates in force for one term."""

    term_id: str
    semester: str
    start: date
    end: date
    rates: tuple[Decimal, ...]
    # False means "nobody has confirmed these are the rates that term actually
    # charged" -- they were seeded from the old module constant. Surfaced by
    # `quality.check_unverified_dues_rates` rather than assumed correct.
    verified: bool = False


def _reach_back(windows: "list[DuesWindow]") -> tuple[DuesWindow, ...]:
    """
    Stretch each term's dues window back to the day after the previous term ended.

    Treasury, 2026-10-01: nobody pays dues for a semester that is over, so a
    payment made between terms is for the next one. Real Fall 2026 dues started
    arriving 08/12, three days before the term's 08/15 start, and fell outside
    every window. The first term keeps its own start -- there is nothing before
    it to reach back to.
    """
    out: list[DuesWindow] = []
    for window in windows:
        if out and out[-1].end + timedelta(days=1) < window.start:
            window = replace(window, start=out[-1].end + timedelta(days=1))
        out.append(window)
    return tuple(out)


class DuesSchedule:
    """
    Which dues amounts are valid, and when.

    `DUES_AMOUNTS` was a module constant pinned to Fall 2024. The VP Treasury
    Handbook shows the rate changing nearly every term ($20/$40 -> $25/$40 ->
    $30/$50 -> $35/$52.50), and because `rule_dues` tests *exact* equality, the
    first term after a rate change silently stops categorizing dues altogether:
    no error is raised, the rows simply fall through to the review queue and
    dues income appears to collapse. Rates belong beside the term, as data.

    An empty schedule behaves exactly like the old constant, which is what every
    caller that passes nothing continues to get.
    """

    def __init__(
        self,
        windows: "tuple[DuesWindow, ...] | list[DuesWindow]" = (),
        *,
        default_rates: tuple[Decimal, ...] = DUES_AMOUNTS,
        accept_venmo_net: bool = False,
        venmo_fee_tolerance: Decimal = VENMO_FEE_TOLERANCE,
    ) -> None:
        self._windows = _reach_back(sorted(windows, key=lambda w: w.start))
        self._default_rates = tuple(default_rates)
        # OFF by default: accepting net-of-fee amounts books income that exact
        # matching would leave for a human, and that is a treasurer's call.
        # See HANDOFF section 4.1 / docs/treasury-questions.md.
        self._accept_venmo_net = accept_venmo_net
        self._venmo_fee_tolerance = venmo_fee_tolerance

    def __bool__(self) -> bool:
        return bool(self._windows)

    # Read-only throughout: `dues.schedule_from_terms` memoizes and hands the
    # same instance to every caller, so a settable flag here would let one page
    # silently change how another page books income.
    @property
    def windows(self) -> tuple[DuesWindow, ...]:
        return self._windows

    @property
    def accept_venmo_net(self) -> bool:
        return self._accept_venmo_net

    @property
    def venmo_fee_tolerance(self) -> Decimal:
        return self._venmo_fee_tolerance

    def window_for(self, when: date | None) -> DuesWindow | None:
        if when is None:
            return None
        for window in self._windows:
            if window.start <= when <= window.end:
                return window
        return None

    def rates_for(self, when: date | None) -> tuple[Decimal, ...]:
        """
        Rates in force on `when`, falling back to the default.

        Falling back rather than returning nothing is deliberate: a transaction
        dated outside every configured term must not silently stop being dues.
        """
        window = self.window_for(when)
        if window is None or not window.rates:
            return self._default_rates
        return window.rates

    def matches(self, amount: Decimal, when: date | None, *, is_venmo: bool = False) -> bool:
        """Does `amount` look like a dues payment made on `when`?"""
        rates = self.rates_for(when)
        if equals_any(amount, rates):
            return True
        if not (self.accept_venmo_net and is_venmo):
            return False
        # A fee only ever reduces what arrives, so the window is one-sided.
        return any(
            gross * (Decimal(1) - self.venmo_fee_tolerance) <= amount < gross
            for gross in rates
        )


FOOD_MERCHANT_KEYWORDS: tuple[str, ...] = (
    "publix", "piesanos", "chipotle", "panda express", "chick-fil-a", "chick fil a",
    "pizza", "grill", "kitchen", "deli", "cafe", "restaurant", "food", "sushi",
    "asian", "mexic", "menchies", "mr and mrs crab", "hana sushi", "las carretas",
    "escapology", "bagel", "bakery", "taco", "wings", "subway", "jimmy john",
    "moe's",
    "panera", "sam's club", "walmart", "wm supercenter", "costco", "target",
    # Toast, the restaurant point-of-sale: its charges read "TST*<restaurant>",
    # and the restaurant name is often cut off ("TST*MI APA LATIN C" on a $641
    # meeting-day order). Bars use Toast too; `looks_like_bar` still wins.
    "tst*",
)

BAR_LIQUOR_KEYWORDS: tuple[str, ...] = (
    "macdintons", "salty dog", "saloon", "arcade bar", "the grove", "grove - ga",
    "gator beverage", "abc fine wine", "total wine", "liquor", "spirits",
    "bottle shop", "tavern", "lounge", "first magnitud", "lil rudy", "brewery",
    "brewing", "cantina", "pub house",
)

# Grocery/big-box merchants that must never be read as a bar even when a
# generic bar keyword appears somewhere in the description.
NEVER_BAR_KEYWORDS: tuple[str, ...] = (
    "publix", "piesanos", "chipotle", "panda express", "walmart",
    "wm supercenter", "target", "costco", "sam's club",
)

# Monday added 2026-11-03 (treasury: "meeting food days should include
# monday"). Food for a Tuesday GBM is often bought the day before -- the $443.68
# Moe's order on Monday 10/26 -- and 18% of Meeting Food's history is Mondays.
# The one list: scoring and bulk import it from here.
MEETING_WEEKDAYS = frozenset({"Monday", "Tuesday", "Wednesday"})

COMMITTEE_PURPOSE: dict[int, str] = {
    1: "Dues",
    2: "Misc.",
    3: "Transfers",
    5: "Food & Drink",
    7: "Professional Development",
    8: "Meeting Food",
    13: "Merch",
    14: "Road Trip",
    17: "Refunded",
    18: "Formal",
}

# --- Memo vocabulary ---------------------------------------------------------
#
# Treasury, asked whether a payment memo is enough to book a transfer on:
# "Absolutely look at the memos they will clarify it well."
#
# A memo is safe to key on where a payer's *name* is not. `merchants.merchant_key`
# deliberately refuses to learn from an incoming transfer, because a rule keyed
# on one member would mis-categorise everything that member ever pays. A memo is
# the opposite: it is the payer's own statement of what this one payment was
# for, and it travels with that payment only.
#
# ORDER AGAINST `rule_dues` IS LOAD-BEARING. From Fall 2026 the dues rates are
# $50 and $65, and treasury warned that formal payments land near them --
# "there are also going to be some formal dues that might be around those
# amounts but the actual dues are a very specific amount". An amount collision
# between dues and formal is therefore expected rather than hypothetical, and
# the memo is the only evidence that can break the tie. So the memo rule runs
# BEFORE the amount rule: what the payer says it was for outranks what the
# amount happens to equal.
#
# Keywords are matched longest-first within a group so "semi formal" cannot be
# claimed by a shorter overlapping entry. Groups are tried in listed order.
#
# NOT here, deliberately: "headshot" (6 rows). Treasury left the Professional
# Development vs Membership call open, and a keyword rule cannot guess it.
# Likewise bare "ticket" -- it appears on formal, road trip and event payments
# alike, so on its own it names no committee.
MEMO_COMMITTEE_KEYWORDS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    # Bare "semi" is matched in the memo only -- see MEMO_WORD_COMMITTEE_KEYWORDS.
    (("semi-formal", "semi formal", "semiformal", "formal"), 18, "formal"),
    (
        (
            "crewneck", "crew neck", "sweatshirt", "sweater", "t-shirt", "tshirt",
            "t shirt", "shirt", "hoodie", "merch",
        ),
        13,
        "merch",
    ),
    (
        ("saint augustine", "st. augustine", "st augustine", "road trip", "roadtrip"),
        14,
        "road trip",
    ),
    # Added 2026-09-08 from the real Fall 2026 statement, where
    # "MEMBERSHIP REIMBURSEMENT SOCIAL" fell all the way through to the model
    # tier despite naming its committee in the memo.
    (("social",), 5, "social"),
    # "GBM" is General Body Meeting. The one occurrence in that statement --
    # "GBM #1 MEETING FOOD REIMBURSEMENT" -- was already resolved, but only
    # because "meeting food" happens to trip the food-merchant keyword. Naming
    # it makes the obvious case deterministic instead of incidental.
    (("gbm", "general body meeting"), 8, "GBM"),
)

# "membership" IS NOT IN THE TABLE ABOVE, and this is the important part.
#
# It is the obvious keyword to add after seeing "MEMBERSHIP REIMBURSEMENT
# SOCIAL" fall through, and adding it would be a serious bug. Checked against
# the real Fall 2026 statement: "membership" appears in **21 memos, 20 of them
# incoming dues payments** -- "MEMBERSHIP DUES", "AIS FALL 26 MEMBERSHIP",
# "MEMBERSHIP FEE". Everything in this table runs *ahead* of `rule_dues`, so a
# "membership" keyword would book roughly $1,000 of dues revenue into the
# Membership expense committee and strip the term attribution off every one of
# those rows.
#
# "social" is safe for exactly the reason "membership" is not: it appears once,
# on an outgoing transfer, and never in a dues memo. The test for a keyword
# belonging here is not "does it name a committee" but "can it collide with
# dues", and that has to be checked against real memos rather than assumed.

# Dues memos are handled separately by `rule_dues_memo`, NOT here, and the
# distinction is the whole design.
#
# Treasury asked for a dues memo keyword "in addition" to the amount rule. In
# addition is the operative word: it must not *replace* the schedule. So this
# group is not in the table above, because everything above runs ahead of
# `rule_dues` and would therefore shadow it. `rule_dues_memo` runs *after*
# instead, which means the schedule still gets first refusal and keeps its term
# attribution, and the memo only speaks for amounts the schedule turned down.
#
# The thing that made this safe to add: `quality.check_possible_dues_rate_change`
# reads raw amounts against the schedule, not `budget_category`, so booking
# these rows as dues does not hide a rate change from it. They are in fact the
# strongest possible evidence of one -- someone naming the payment themselves at
# an amount no term is known to have charged.
DUES_MEMO_RE = re.compile(r"\b(dues|membership fee)\b", re.I)


# A dues-amount payment is booked as dues unless its memo uses one of these.
# Each names something a member pays the chapter for that is NOT dues. Matched
# as word prefixes against the memo only (never the payer's name), so "reimburs"
# catches reimbursement and reimburse.
#
# Deliberately absent, because they appear on real dues payments in the Fall
# 2026 statements: "registration", "entry", "entrance", "enrollment",
# "application", "fee". The committee keywords (formal, merch, road trip,
# social, gbm) are not repeated here -- `rule_memo_committee` runs first and
# books those outright.
DUES_CONFLICT_KEYWORDS: tuple[str, ...] = (
    "ticket", "refund", "reimburs", "pay back", "payback", "paying back", "owe",
    "rent", "deposit", "donat", "sponsor", "gift", "food", "pizza", "dinner",
    "lunch", "banquet", "gala", "event", "trip", "shirt", "jersey", "conference",
    "loan", "split",
)

# Confidence on a dues-amount payment with nothing in its memo arguing.
DUES_AMOUNT_CONFIDENCE = 0.99

# "ZELLE FROM JANE DOE ON 08/19 REF # BACX1234 <memo>" -- everything the payer
# typed comes after the reference number. Wells Fargo puts the payer's name
# before it, which is why the memo has to be sliced out rather than searched
# for in the whole string.
_AFTER_REF_RE = re.compile(r"ref\s*#\s*\S+\s*(.*)$", re.I | re.S)


def extract_memo(details: object) -> str:
    """
    The part of a transfer description the payer wrote.

    Returns "" when there is no memo, which is the common case and must not be
    confused with a memo naming nobody in particular.
    """
    if details is None:
        return ""
    text = _WHITESPACE_RE.sub(" ", str(details)).strip()

    match = _AFTER_REF_RE.search(text)
    if match:
        return match.group(1).strip()

    # Venmo: "txn id | note | from | to"
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) >= 2:
            return parts[1]
    return ""


def mentions_dues(details: object) -> bool:
    """Does this transfer's memo call itself dues?"""
    return bool(DUES_MEMO_RE.search(_text(details)))


# Memo keywords trusted on OUTGOING transfers only. "membership" is the reason
# this table exists: on an incoming payment it means dues (20 of 21 real memos),
# which is why it is barred from the table above. But dues are never paid *out*,
# so on a reimbursement -- "MEMBERSHIP REIMBURSEMENT" -- it can only mean the
# Membership committee's spending. Real examples, Checking (5).csv, 09/14/2026.
OUTGOING_MEMO_COMMITTEE_KEYWORDS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("meeting food",), 8, "meeting food"),
    (("membership",), 5, "membership"),
)

# Outgoing memos naming Consulting go to Consulting, whatever else they say
# (treasury, 2026-10-12: "if the memos say consulting it should go to
# consulting"). Checked ahead of every other keyword, so "CONSULTING MEETING
# FOOD REIMBURSEMENT" is Consulting's food, not GBM food. Spring 2026 had
# $911 of "CONSULTING FOOD" reimbursements booked to Meeting Food by scoring.
#
# Outgoing only: the one incoming memo mentioning it in four years of data is
# "PAYMENT FOR AIS HOODIE WITH CONSULTING COM..." -- merch, not Consulting.
OUTGOING_FIRST_MEMO_KEYWORDS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("consulting",), 7, "consulting"),
)


def committee_from_memo(
    details: object, *, outgoing: bool = False
) -> tuple[int, str, str] | None:
    """
    The committee a transfer memo names, if it names one.

    Returns `(committee_id, group_label, matched_keyword)` so the rule can say
    which word it acted on -- a treasurer overturning it needs to see that.
    """
    text = _text(details)
    if not text:
        return None
    if outgoing:
        tables = (
            OUTGOING_FIRST_MEMO_KEYWORDS
            + MEMO_COMMITTEE_KEYWORDS
            + OUTGOING_MEMO_COMMITTEE_KEYWORDS
        )
    else:
        tables = MEMO_COMMITTEE_KEYWORDS
    for keywords, committee_id, label in tables:
        for keyword in keywords:
            if _keyword_in(keyword, text):
                return committee_id, label, keyword
    memo = extract_memo(details).lower()
    for keywords, committee_id, label in MEMO_WORD_COMMITTEE_KEYWORDS:
        for keyword in keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", memo):
                return committee_id, label, keyword
    return None


# Keywords too short to trust as substrings, so they are matched as whole words
# in the memo the payer typed -- never in the payer's name or a merchant.
# "SEMI" alone was the commonest Fall 2026 ticket memo ("AIS SEMI", "YAY SEMI");
# as a substring it would also hit names like "Semira" (treasury, 2026-11-03:
# "the tag lines literally say AIS semi").
MEMO_WORD_COMMITTEE_KEYWORDS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("semi",), 18, "formal"),
)


def is_dues_amount(record: dict, schedule: "DuesSchedule | None" = None) -> bool:
    """Whether a positive amount is one of the dues rates in force on its date."""
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return False
    if schedule is None:
        return equals_any(amount, DUES_AMOUNTS)
    details, account = record.get("details"), record.get("account")
    return schedule.matches(amount, record_date(record), is_venmo=is_venmo(details, account))
def _keyword_in(keyword: str, text: str) -> bool:
    """Substring match, except short keywords ("gbm", "semi") must be whole words."""
    if len(keyword) > 4:
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None

# Real Wells Fargo descriptions pad with runs of spaces:
#   "PURCHASE                    AUTHORIZED ON   05/17 MERCHANT ..."
# The original literal "purchase authorized on " never matched a real statement,
# so the embedded purchase date was never found, the weekday was always
# "Unknown", and the Meeting Food rule could not fire at all. Whitespace is
# collapsed before matching, and "RECURRING PAYMENT" is accepted alongside
# "PURCHASE".
_PURCHASE_RE = re.compile(
    r"(?:purchase|recurring\s+payment)\s+authorized\s+on\s+(\d{1,2}/\d{1,2})",
    re.I,
)
_WHITESPACE_RE = re.compile(r"\s+")
_BAR_WORD_RE = re.compile(r"\b(bar|pub)\b")


# --- Result type -------------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    """The outcome of categorizing one transaction."""

    committee_id: int | None
    purpose: str | None
    rule: str
    confidence: float
    source: str  # "rule" | "scored" | "llm" | "none" ("merchant" on rows stored before 2026-09-23)

    @property
    def is_assigned(self) -> bool:
        return self.committee_id is not None


UNMATCHED = Classification(
    committee_id=None, purpose=None, rule="", confidence=0.0, source="none"
)


def _assign(committee_id: int, rule: str, *, confidence: float, source: str = "rule") -> Classification:
    return Classification(
        committee_id=committee_id,
        purpose=COMMITTEE_PURPOSE[committee_id],
        rule=rule,
        confidence=confidence,
        source=source,
    )


# --- Field helpers -----------------------------------------------------------

def extract_purchase_date(details: object) -> str:
    """
    The MM/DD that Wells Fargo embeds in 'PURCHASE AUTHORIZED ON 09/17 ...'.

    Returns "" when there is no embedded date.
    """
    if not isinstance(details, str):
        return ""
    match = _PURCHASE_RE.search(_WHITESPACE_RE.sub(" ", details))
    return match.group(1) if match else ""


def weekday_from_details(details: object, row_date: object) -> str:
    """
    Weekday of the embedded purchase date.

    The posting date on the statement row is often a day or two after the
    purchase, which is why the original went looking inside the description --
    a Tuesday GBM grocery run frequently posts on Thursday.
    """
    date_text = extract_purchase_date(details)
    if not date_text:
        return "Unknown"
    try:
        import pandas as pd

        anchor = pd.to_datetime(row_date, errors="coerce")
        if pd.isna(anchor):
            return "Unknown"
        month, day = (int(part) for part in date_text.split("/"))
        year = int(anchor.year)
        # A purchase on 12/30 can post on 01/02 of the next year, so roll the
        # year back when the embedded month is far ahead of the posting month.
        if month - anchor.month > 6:
            year -= 1
        parsed = datetime(year, month, day)
        return parsed.strftime("%A")
    except (ValueError, TypeError):
        return "Unknown"


def _text(value: object) -> str:
    return "" if value is None else str(value).lower()


def _has_any(haystack: str, needles: tuple[str, ...]) -> bool:
    return any(needle in haystack for needle in needles)


def is_venmo_or_zelle(details: object, account: object) -> bool:
    text = _text(details)
    canonical = normalize_account(account if account is None else str(account))
    return "venmo" in text or "zelle" in text or canonical == ACCOUNT_VENMO


def is_venmo(details: object, account: object) -> bool:
    """
    Venmo specifically, not Zelle.

    Only Venmo takes a cut, so only Venmo rows are eligible for net-of-fee dues
    matching. Zelle transfers arrive whole.
    """
    canonical = normalize_account(account if account is None else str(account))
    return "venmo" in _text(details) or canonical == ACCOUNT_VENMO


def record_date(record: dict) -> date | None:
    """
    The transaction's own date, for schedule lookups.

    Distinct from the purchase date `rule_meeting_food` digs out of the
    description: that one is needed for the weekday, this one for "which term
    was this in". Accepts the ISO strings SQLite stores, datetimes, and
    anything with a `.date()`.
    """
    value = record.get("transaction_date")
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_date = getattr(value, "date", None)  # pandas.Timestamp and friends
    if callable(to_date):
        try:
            return to_date()
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def looks_like_bar(details: object) -> bool:
    text = _text(details)
    if _has_any(text, NEVER_BAR_KEYWORDS):
        return False
    return _has_any(text, BAR_LIQUOR_KEYWORDS) or bool(_BAR_WORD_RE.search(text))


# Food-list merchants whose counter here sells something else. "target" is on
# the food list for grocery runs; Target's copy centre prints flyers (two 9570
# charges in Checking (5).csv, 09/16/2026).
NOT_FOOD_KEYWORDS: tuple[str, ...] = ("target copy",)


def looks_like_food_merchant(details: object) -> bool:
    text = _text(details)
    return _has_any(text, FOOD_MERCHANT_KEYWORDS) and not _has_any(text, NOT_FOOD_KEYWORDS)


# --- Individual rules --------------------------------------------------------
#
# Each takes the record dict and returns a Classification or None.

# An outgoing transfer whose memo names no committee. Not a classification --
# `committee_id` is None, so the pipeline keeps looking -- but it carries a
# `rule` string, which is what the review queue shows. Before treasury's ruling
# these rows were booked to 17 (Refunded) and disappeared from budget-vs-actual;
# now they are a question, and the queue has to be able to ask it in words.
REIMBURSEMENT_UNRESOLVED = Classification(
    committee_id=None,
    purpose=None,
    rule=(
        "Outgoing transfer — a reimbursement or a direct payment. It belongs to "
        "the committee whose expense it covered, and the memo does not say which."
    ),
    confidence=0.0,
    source="none",
)


def rule_reimbursement(record: dict) -> Classification | None:
    """
    Outgoing Venmo/Zelle: a committee's expenditure, not a ledger bucket.

    This rule used to be `rule_refund`, and it booked every negative Venmo or
    Zelle to committee 17 (Refunded) at full confidence. 17 is `kind="ledger"`,
    so those rows sat outside budget-vs-actual entirely -- meaning every dollar
    a member fronted on a personal card and was paid back for was invisible to
    the budget of the committee that actually spent it. The 2023 treasurer had
    booked them the other way, and 18 labeled rows disagreed with the app.

    Treasury settled it: "Reimbursements should go to the committee they
    represent. If someone spends money on a personal card and gets reimbursed
    then it was a committee expenditure."

    So there is no default committee here any more. The memo is tried first;
    failing that the row falls through to the card roster and scoring like any
    other, and if nothing resolves it, a human is asked. That is a real loss of
    automatic coverage, and it is the correct trade: the previous coverage was
    manufactured by answering a question nobody had asked.

    Committee 17 keeps its meaning for money coming *back* -- a merchant return
    is still a refund (see `bulk.propose`) -- which is why this rule is renamed
    rather than deleted.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None or amount >= 0:
        return None
    if not is_venmo_or_zelle(record.get("details"), record.get("account")):
        return None
    return REIMBURSEMENT_UNRESOLVED


def rule_internal_transfer(record: dict) -> Classification | None:
    """
    The organisation moving its own money between its own accounts.

    A Zelle into checking whose memo says "Venmo" is the Venmo balance being
    cashed out to the bank. Treasury ruled (2026-10-01) that these are not
    income: the money was already ours and only changed accounts. Booked as
    Transfers (a ledger committee), and `budgets._totals_for` keeps that
    committee out of income and expenses.

    Zelle only. A payment arriving *on* the Venmo account whose memo happens to
    say "venmo" is not evidence of anything.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None:
        return None
    text = _WHITESPACE_RE.sub(" ", _text(record.get("details")))
    # Venmo's own ACH lines on the checking side: a cash-out of the Venmo
    # balance ("VENMO CASHOUT", $11,628.80 when Venmo was retired in April 2026)
    # and the micro-deposits Venmo sends to verify a linked bank account.
    for marker in VENMO_ACH_TRANSFERS:
        if text.startswith(marker):
            return _assign(
                3,
                f"Transfer ({marker.upper()} -- the chapter's Venmo account and checking)",
                confidence=1.0,
            )
    for marker in PAYPAL_ACH_TRANSFERS:
        if text.startswith(marker):
            return _assign(
                3,
                f"Transfer ({marker.upper()} -- the chapter's PayPal account and checking)",
                confidence=1.0,
            )
    if "zelle" not in text or "venmo" not in text:
        return None
    direction = "in" if amount > 0 else "out"
    return _assign(
        3,
        f"Transfer (Zelle {direction} whose memo says Venmo -- the chapter's own money moving between accounts)",
        confidence=1.0,
    )


VENMO_ACH_TRANSFERS: tuple[str, ...] = ("venmo cashout", "venmo acctverify")

# The PayPal balance moved into checking ("PAYPAL TRANSFER 261020 ... ASSOCIATION
# FOR INFORM", $963.93 on 10/20/2026). Treasury, 2026-11-03: "a transfer,
# non-income".
PAYPAL_ACH_TRANSFERS: tuple[str, ...] = ("paypal transfer",)

# The bank's own charges. Treasury owns them -- the 2023 ledger and the old app
# both booked bank fees to committee 2.
BANK_FEE_MARKERS: tuple[str, ...] = ("monthly service fee", "overdraft fee", "wire transfer fee")


def rule_bank_fee(record: dict) -> Classification | None:
    """A charge from the bank itself."""
    amount = parse_amount(record.get("amount"))
    if amount is None or amount >= 0:
        return None
    text = _WHITESPACE_RE.sub(" ", _text(record.get("details"))).strip()
    for marker in BANK_FEE_MARKERS:
        if text.startswith(marker):
            return _assign(2, f"Bank fee ('{marker}')", confidence=1.0)
    return None


def rule_memo_committee(record: dict) -> Classification | None:
    """
    A transfer whose memo says what the money was for.

    Applies in both directions, and that symmetry is the point. Incoming, it
    books a member's "hoodie" payment to Merch. Outgoing, it books a "formal"
    reimbursement to Formal -- which under the old `rule_refund` would have gone
    to Refunded and vanished from the Formal budget.

    Supersedes the former `rule_formal`, which was this rule with a single
    keyword and a positive-amount guard.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None:
        return None
    details, account = record.get("details"), record.get("account")
    if not is_venmo_or_zelle(details, account):
        return None
    found = committee_from_memo(details, outgoing=amount < 0)
    if found is None:
        return None
    committee_id, label, keyword = found
    direction = "reimbursement" if amount < 0 else "payment"
    return _assign(
        committee_id,
        f"{label.title()} ({direction} memo says '{keyword}')",
        confidence=1.0,
    )


def dues_conflict(details: object) -> str | None:
    """The word in a transfer's memo that says it is something other than dues."""
    memo = extract_memo(details).lower()
    if not memo:
        return None
    for keyword in DUES_CONFLICT_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}", memo):
            return keyword
    return None


def rule_dues(record: dict, schedule: DuesSchedule | None = None) -> Classification | None:
    """
    A dues amount arriving by Venmo or Zelle -- unless the memo says otherwise.

    `schedule` supplies the rates in force for the transaction's term. Omitting
    it falls back to `DUES_AMOUNTS`, which is what the rule did before rates
    became per-term data.

    Treasury, 2026-10-01: a Zelle at a dues amount is dues "ninety-nine percent
    of the time", so it books at DUES_AMOUNT_CONFIDENCE -- with no memo, or a
    memo that is only a name, included. The other one percent is a memo that
    says something else ("formal ticket", "pay back"); that row is not booked
    as dues but handed on with the reason, for the model to decide and a human
    after it.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    details, account = record.get("details"), record.get("account")
    if not is_venmo_or_zelle(details, account):
        return None

    if schedule is None:
        if not equals_any(amount, DUES_AMOUNTS):
            return None
        where = ""
    else:
        when = record_date(record)
        if not schedule.matches(amount, when, is_venmo=is_venmo(details, account)):
            return None
        window = schedule.window_for(when)
        where = f" for {window.semester}" if window is not None else ""

    conflict = dues_conflict(details)
    if conflict is not None:
        return Classification(
            committee_id=None,
            purpose=None,
            rule=(
                f"Dues amount (${amount:,.2f}{where}), but the memo says "
                f"'{conflict}' -- may not be dues"
            ),
            confidence=0.0,
            source="none",
        )
    return _assign(
        1, f"Dues (dues rate{where} via Venmo or Zelle)", confidence=DUES_AMOUNT_CONFIDENCE
    )


def rule_dues_memo(record: dict) -> Classification | None:
    """
    An incoming transfer the payer themselves called dues.

    Runs *after* `rule_dues`, so by the time it fires the amount has already
    failed to match any rate on file for that term. Treasury asked for this "in
    addition" to the amount rule, and after is what "in addition" has to mean:
    ahead of it, the memo would shadow the per-term schedule and every dues row
    would lose the term attribution in its reason string.

    What it catches is the case the schedule cannot: a rate nobody recorded, a
    member paying a partial or late amount, or the first payment of a term whose
    rate just changed. The reason string always names the amount, because a dues
    row at an amount no term charged is a fact worth seeing rather than
    smoothing over.

    Positive only. A negative transfer memoed "dues" is a refund of dues, which
    is a different question and goes to a human.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    details, account = record.get("details"), record.get("account")
    if not is_venmo_or_zelle(details, account):
        return None
    if not mentions_dues(details):
        return None
    return _assign(
        1,
        f"Dues (memo says dues; ${amount:,.2f} is not a rate on file for this term)",
        confidence=1.0,
    )


EVENT_PRICES_PATH = Path(__file__).resolve().parents[2] / "config" / "event_prices.json"
EVENT_PRICE_CONFIDENCE = 0.9


@functools.lru_cache(maxsize=1)
def _event_prices() -> tuple[dict, ...]:
    """config/event_prices.json's events, parsed once."""
    data = json.loads(EVENT_PRICES_PATH.read_text(encoding="utf-8"))
    events = []
    for event in data.get("events", []):
        events.append(
            {
                "name": str(event["name"]),
                "committee": int(event["committee"]),
                "start": date.fromisoformat(event["start"]),
                "end": date.fromisoformat(event["end"]),
                "amounts": tuple(float(a) for a in event["amounts"]),
            }
        )
    return tuple(events)


def rule_event_price(record: dict) -> Classification | None:
    """
    A memo-less member payment at a merch or event price, inside its dates.

    Runs after the memo and dues rules, so it only sees what they left: a
    transfer whose memo is blank or just a name. The first matching event in
    config/event_prices.json wins, which is how a $40 that is both a merch
    bundle and a semi-formal ticket gets decided.
    """
    amount = parse_amount(record.get("amount"))
    if amount is None or amount <= 0:
        return None
    details, account = record.get("details"), record.get("account")
    if not is_venmo_or_zelle(details, account):
        return None
    when = record_date(record)
    if when is None:
        return None
    for event in _event_prices():
        if event["start"] <= when <= event["end"] and equals_any(amount, event["amounts"]):
            return _assign(
                event["committee"],
                f"{event['name']} (${amount:,.2f} is one of its prices)",
                confidence=EVENT_PRICE_CONFIDENCE,
            )
    return None


def rule_meeting_food(record: dict) -> Classification | None:
    """
    GBM meeting food: a food merchant, on a meeting weekday, that is not a bar.

    This is the rule the original delegated to the model. Everything it needs is
    local, so it runs here and the model never sees these rows.
    """
    details = record.get("details")
    if looks_like_bar(details) or not looks_like_food_merchant(details):
        return None
    weekday = weekday_from_details(details, record.get("transaction_date"))
    if weekday not in MEETING_WEEKDAYS:
        return None
    return _assign(
        8, f"Meeting food ({weekday} food merchant, not a bar)", confidence=0.9
    )


def rule_membership_bar(record: dict) -> Classification | None:
    if not looks_like_bar(record.get("details")):
        return None
    return _assign(5, "Membership (bar or liquor merchant)", confidence=0.85)


# Rules keyed on an unambiguous marker: an exact dues amount, a memo, the sign
# of a transfer. These are certainties, not guesses. The one certainty that
# outranks them -- a card on the roster -- is data rather than code, and runs
# ahead of this tier in `pipeline.categorize_records`.
#
# ORDER. `rule_memo_committee` must precede `rule_dues`: from Fall 2026 dues are
# $50/$65 and formal payments land near those amounts, so an amount collision is
# expected and only the memo can break it. `rule_reimbursement` comes last
# because it resolves nothing -- it only labels an outgoing transfer the memo
# could not place, and must not pre-empt a rule that could have placed it.
EXACT_RULES: tuple[Callable[[dict], Classification | None], ...] = (
    rule_internal_transfer,
    rule_bank_fee,
    rule_memo_committee,
    rule_dues,
    rule_dues_memo,
    rule_event_price,
    rule_reimbursement,
)

# Keyword and weekday heuristics. Confident, but each beatable by something
# more specific listed first. The cardholder default that used to sit between
# them (`rule_membership_card`, hard-coding cards 8313/5718) is gone: a card on
# the roster now decides its row before any of these run.
HEURISTIC_RULES: tuple[Callable[[dict], Classification | None], ...] = (
    rule_meeting_food,
    rule_membership_bar,
)

# Documented priority order. First match wins.
RULES: tuple[Callable[[dict], Classification | None], ...] = EXACT_RULES + HEURISTIC_RULES


def first_match(
    record: dict, rules: tuple[Callable[[dict], Classification | None], ...]
) -> Classification:
    for rule in rules:
        result = rule(record)
        if result is not None:
            return result
    return UNMATCHED


def exact_rules(
    dues: DuesSchedule | None = None,
) -> tuple[Callable[[dict], Classification | None], ...]:
    """
    The exact-rule chain, with `rule_dues` bound to a schedule if one is given.

    Binding here rather than giving every rule a context parameter keeps the
    other three rules plain single-argument callables, and keeps the no-schedule
    path returning the exact same tuple object it always did.
    """
    if dues is None:
        return EXACT_RULES
    return (
        rule_internal_transfer,
        rule_bank_fee,
        rule_memo_committee,
        partial(rule_dues, schedule=dues),
        rule_dues_memo,
        rule_event_price,
        rule_reimbursement,
    )


def classify_exact(record: dict, *, dues: DuesSchedule | None = None) -> Classification:
    """Only the unambiguous rules. Returns UNMATCHED if none fire."""
    return first_match(record, exact_rules(dues))


def classify_deterministic(record: dict) -> Classification:
    """Apply every rule in priority order. Returns UNMATCHED if none fire."""
    return first_match(record, RULES)
