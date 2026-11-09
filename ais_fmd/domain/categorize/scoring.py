"""
Module M18 -- weighted evidence scoring.

WHY THIS REPLACES THE HEURISTIC RULE CHAIN.

`predicates.RULES` is first-match-wins, so the *order* of the rules encodes
every tie-break. That broke in practice: the Membership card rule and the
meeting-food rule both fired on the same rows, and picking a winner meant
hand-ordering them from one fact a treasurer happened to mention (cards got
handed out messily, so meeting food has been bought on the wrong card). A rule
chain cannot express "card says Membership, weekday and merchant say Meeting
Food, so I am genuinely unsure" -- it just silently returns whichever rule was
listed first, at full confidence.

Scoring can. Every signal votes for a committee with a weight; the winner is
the highest total, and **confidence falls when signals conflict**. Two strong
opposing signals produce a low-confidence result that routes to the review
queue instead of being quietly booked. That is the behaviour that was actually
wanted: resolve the clear majority automatically, flag the genuine outliers.

CHANGED 2026-09-23. The card-versus-meeting-food conflict above is no longer
a conflict: treasury ruled that a confirmed card decides its row, so it is
settled by `CardRegistry.certain` before scoring runs. Scoring now weighs the
purchases whose card is not on the roster.

CONFIDENCE IS NOT A RULE CONSTANT. In `predicates.py` each rule carries a fixed
number (0.9, 0.85) that never changes regardless of what else is true about the
row. Here confidence is computed per transaction from two things:

  * evidence strength -- how much total weight landed on the winner
  * dominance         -- how decisively it beat the runner-up

so the same rule firing on a clean row and on a contested row yields different
confidence, which is the entire point.

A NOTE ON LEARNING FROM HISTORY. The obvious next step is to learn these
weights from previously-categorized transactions. That is deliberately NOT done
here, because in this database `budget_category` was written by
`categorize_frame` at import time (see `scripts/load_real_statement.py`), not by
a human. Learning from it would train the model on its own output and harden
existing mistakes into "evidence". `CardRegistry` therefore holds only
treasury-confirmed assignments, and any history-derived affinity must be marked
`verified=False` so it can never outweigh a confirmed one. Real ground truth
has to come from a human working the review queue, or from the historical
ledgers in Drive -- not from this table.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ...config.categories import LINE_BY_ID as COMMITTEE_BY_ID, line_name as committee_name
from ..money import parse_amount
from .predicates import (
    COMMITTEE_PURPOSE,
    Classification,
    looks_like_bar,
    looks_like_food_merchant,
    record_date,
    weekday_from_details,
)

MEETING_WEEKDAYS = frozenset({"Tuesday", "Wednesday"})

# Labels are tagged with the officer cohort they were decided under, because
# card assignments do not survive a handover. The cohort is no longer a
# constant here: it is `_current_era` in config/card_roster.json, read by
# `current_era()`, so starting a new cohort is one file edit.

# Auto-apply at or above this; anything below routes to the review queue with
# its proposal and evidence attached. Tuned against the real statement -- see
# `scripts/tune_scoring.py` for the coverage/√ risk trade-off at other values.
AUTO_APPLY_THRESHOLD = 0.75

# Confidence booked on a row the card decided. High, but deliberately not 1.0:
# unlike an exact rule it can be wrong when someone used the card for another
# committee's purchase, and the spot-check tools sample by confidence.
CARD_DEFAULT_CONFIDENCE = 0.95

# Weight scale: what "one unit" of evidence means.
#   2.0  strong structural signal (meeting weekday, food-on-a-meeting-day)
#   1.0  ordinary corroboration (catering-sized amount)
#   0.5  weak hint
#
# REMOVED 2026-09-23: W_MERCHANT_MEMORY (6.0) and W_MERCHANT_HISTORY (2.0).
# Treasury ruled that merchant memory comes out of categorization entirely:
# the same merchant is bought from by different committees too often --
# Consulting food and Meeting Food especially overlap at the same restaurants
# -- for a merchant's past filings to say anything about the next one. See
# `pipeline.categorize_records`.
W_FOOD_MERCHANT = 1.5
W_MEETING_WEEKDAY = 2.0
W_CATERING_SIZED = 1.0    # a large food purchase looks like feeding a GBM
W_INCIDENTAL_SIZED = 0.75  # a very small one does not
# REMOVED: W_BAR_MERCHANT = 2.5 ("bars are Membership"). Measured precision
# 38%, fitted weight 0.00. See the note in `collect_signals`.

# Food AND a meeting weekday corroborate each other: either alone is weak, but
# together they are the signature of a GBM food run -- on a purchase whose card
# is not on the roster. A card on the roster is decided before scoring runs.
W_MEETING_CONJUNCTION = 2.0

# The most any combination of the signals above can put behind one reading: a
# catering-sized food purchase on a meeting weekday.
MAX_NON_CARD_EVIDENCE = (
    W_FOOD_MERCHANT + W_MEETING_WEEKDAY + W_MEETING_CONJUNCTION + W_CATERING_SIZED
)

# THE CARD IS THE STRONGEST EVIDENCE THERE IS. Treasury ruling, 2026-09-23,
# replacing both earlier ones (2026-08-24 "meeting food is identified by timing,
# not by whose card paid", and 2026-09-08 "more emphasis on card", which raised
# this 2.5 -> 4.0 so a card merely *contested* meeting food). A purchase on a
# card treasury has confirmed for the current cohort belongs to that card's
# committee, whatever the merchant, the weekday or the amount suggest.
#
# CHANGED 2026-10-01: treasury softened this. The card is the default, not a
# certainty: members sometimes buy for another committee with the card they have
# on hand (meeting food especially), so it holds unless the *other* evidence
# agrees on a different committee. In the pipeline that is `categorize_records`,
# which scores the row without the card and only lets that reading displace the
# card when it clears the auto-apply gate. The weight here exists for
# callers that score a record directly (weight fitting, diagnostics), and is
# derived rather than chosen so it cannot quietly fall behind: at twice the
# largest competing total plus one, a confirmed card beats a full meeting-food
# reading by enough margin to clear the auto-apply gate on its own
# (`test_a_confirmed_card_outweighs_everything_scoring_can_say`).
W_CARD_VERIFIED = 2 * MAX_NON_CARD_EVIDENCE + 1.0

# How much the rest of the evidence needs to overrule a confirmed card in the
# pipeline: every signal that can speak, all for the same other committee.
# Treasury's words: the card is "taken unless all of the other classifications
# point towards it not being the case". Today the only reading that complete is
# a catering-sized food purchase on a meeting weekday -- a Membership VP's $40
# Tuesday snack run keeps its card, a $150 one is Meeting Food.
CARD_OVERRULE_EVIDENCE = MAX_NON_CARD_EVIDENCE

# Committees whose cards are never overruled automatically. When every other
# signal disagrees with one of these cards, the row goes to the model and then
# a human instead. Consulting caters its own events from the same restaurants
# Meeting Food uses, often on meeting days -- treasury, 2026-10-01: that "is
# absolutely going to happen multiple times" and must not land in Meeting Food.
# Keyed on the committee, not a card number, so it survives the next handover.
REVIEW_ON_CARD_CONFLICT = frozenset({7})

# Committees whose cardholder mostly buys meeting food. On these cards even a
# *partial* meeting-food reading -- food on a meeting day, but not
# catering-sized -- is not left on the card's default; it goes to the model and
# then a human. Operations: the VP buys the meeting food and "absolutely
# nothing" else except the odd thing for her own committee, such as a $66
# rewards dinner (treasury, 2026-10-01). A full reading still overrules the card
# outright, as on any other card.
REVIEW_MEETING_DAY_FOOD_ON = frozenset({6})

# Food bought on a meeting day: the meeting-food reading without the size signal.
MEETING_DAY_FOOD_EVIDENCE = W_FOOD_MERCHANT + W_MEETING_WEEKDAY + W_MEETING_CONJUNCTION
W_CARD_UNVERIFIED = 0.6   # inferred only, must never beat a verified signal

# A food purchase at or above this reads as catering for a meeting rather than
# an incidental snack run.
CATERING_MINIMUM = 75.0
INCIDENTAL_MAXIMUM = 12.0

# Evidence at or above this counts as "fully corroborated"; more does not raise
# confidence further. Keeps a pile-on of weak signals from reading as certainty.
EVIDENCE_SATURATION = 3.0

_CARD_RE = re.compile(r"\bcard\s+(\d{3,5})\b", re.I)


def card_number(details: object) -> str | None:
    """The last-4 of the card used, when the description carries one."""
    if details is None:
        return None
    match = _CARD_RE.search(str(details))
    return match.group(1) if match else None


@dataclass(frozen=True)
class CardAssignment:
    committee_id: int
    holder: str = ""
    verified: bool = False
    # The officer cohort this card was confirmed under, and the dates that
    # cohort held its cards. A card only speaks for transactions inside its
    # cohort's window: a new VP's card replaces the old one's rather than both
    # voting, and a cohort nobody has renewed stops being evidence on its own
    # end date instead of quietly booking the next cohort's spending.
    era: str = ""
    starts: date | None = None
    ends: date | None = None

    @property
    def weight(self) -> float:
        return W_CARD_VERIFIED if self.verified else W_CARD_UNVERIFIED

    def covers(self, when: date | None) -> bool:
        """
        Whether this assignment applies on `when`.

        An undated record is given the benefit of the doubt: it cannot be placed
        in a cohort, and refusing it would silently drop the card from every
        caller that scores a bare description.
        """
        if when is None:
            return True
        if self.starts is not None and when < self.starts:
            return False
        if self.ends is not None and when > self.ends:
            return False
        return True


# WHERE THE ROSTER LIVES. It used to be `CONFIRMED_CARDS`, a dict in this
# module, which meant adding a card was a code change -- and cards turn over
# every time the officers do. Run the real Fall 2026 statement through that
# version and not one of its three cards (0594, 3526, 3466) was known, so the
# card tier contributed nothing at all. The dict is gone rather than kept as a
# fallback: with a confirmed card now deciding a row outright, silently falling
# back to a stale copy of a previous cohort's cards is worse than having none.
# A missing or unreadable file means no card evidence at all, and
# `quality.check_card_roster_era` says so.
#
# `rule_consulting` ("card 8408 is Consulting, ignore everything else") is gone
# for the same reason -- it hard-coded one cohort's card as a certainty, and 8408
# belonged to the 2024-2026 Consulting VP. Every card, Consulting's included, is
# now a roster entry scoped to its cohort.
ROSTER_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "card_roster.json"

# Deliberately empty, and this is a decision rather than an omission.
#
# Seven cards appear in the real statements with no documented owner (0153,
# 7757, 9309, 3444, 7193, 1113, 5535), and treasury said to use a best guess
# until they supply the real numbers. There is no honest guess available yet.
# The only current-era evidence about those cards is this app's own
# `budget_category`, which `categorize_frame` wrote at import time -- inferring
# a roster from it would train the categorizer on its own output and harden
# its existing mistakes into "evidence", which is the one thing the module
# docstring above says never to do.
INFERRED_CARDS: dict[str, CardAssignment] = {}


@dataclass(frozen=True)
class RosterStatus:
    """What the roster file says about cohorts, and whether it could be read."""

    current_era: str = ""
    era_starts: date | None = None
    era_ends: date | None = None
    error: str = ""


def _parse_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _read_roster(path: Path | None) -> tuple[dict, str]:
    target = path or ROSTER_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, f"could not read {target.name}: {exc.strerror or exc}"
    except ValueError as exc:
        return {}, f"{target.name} is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return {}, f"{target.name} must be a JSON object"
    return raw, ""


def _era_windows(raw: dict) -> dict[str, tuple[date | None, date | None]]:
    windows: dict[str, tuple[date | None, date | None]] = {}
    for name, bounds in (raw.get("_eras") or {}).items():
        if isinstance(bounds, dict):
            windows[str(name)] = (_parse_date(bounds.get("start")), _parse_date(bounds.get("end")))
    return windows


def roster_status(path: Path | None = None) -> RosterStatus:
    """The current cohort and its window, for labels and the Data Quality check."""
    raw, error = _read_roster(path)
    era = str(raw.get("_current_era") or "")
    starts, ends = _era_windows(raw).get(era, (None, None))
    return RosterStatus(current_era=era, era_starts=starts, era_ends=ends, error=error)


def current_era(path: Path | None = None) -> str:
    """The officer cohort new labels are tagged with."""
    return roster_status(path).current_era


def load_card_roster(path: Path | None = None) -> dict[str, CardAssignment]:
    """
    The card roster, as data: `config/card_roster.json`.

    Editable by whoever actually knows the answer, without touching Python and
    without a deploy. Entries that do not parse are skipped rather than failing
    the whole file, so one bad edit costs one card, not every card.
    """
    raw, _error = _read_roster(path)
    windows = _era_windows(raw)

    roster: dict[str, CardAssignment] = {}
    for card, entry in raw.items():
        # Underscore keys are documentation -- see the file's own _README.
        if card.startswith("_") or not isinstance(entry, dict):
            continue
        try:
            committee = int(entry["committee"])
        except (KeyError, TypeError, ValueError):
            continue
        if committee not in COMMITTEE_BY_ID:
            continue
        era = str(entry.get("era") or "")
        starts, ends = windows.get(era, (None, None))
        roster[str(card)] = CardAssignment(
            committee_id=committee,
            holder=str(entry.get("holder") or ""),
            verified=bool(entry.get("verified", False)),
            era=era,
            starts=starts,
            ends=ends,
        )
    return roster


class CardRegistry:
    """
    Card -> committee, with provenance and cohort.

    Unverified entries are accepted so history-derived affinities can be tried,
    but they carry a fraction of the weight and are labelled in the evidence, so
    a reviewer can always see whether a suggestion rests on documentation or on
    a guess.
    """

    def __init__(self, assignments: dict[str, CardAssignment] | None = None) -> None:
        self._cards = dict(assignments if assignments is not None else load_card_roster())

    def get(self, card: str | None, when: date | None = None) -> CardAssignment | None:
        """The assignment for `card`, provided its cohort covers `when`."""
        assignment = self._cards.get(card) if card else None
        if assignment is None or not assignment.covers(when):
            return None
        return assignment

    def items(self) -> list[tuple[str, CardAssignment]]:
        return sorted(self._cards.items())

    def default_for(self, record: dict, *, note: str = "") -> Classification | None:
        """
        The committee a confirmed, in-cohort card points at -- or None.

        This is the *default*, not a verdict: the pipeline takes it unless the
        other evidence confidently names a different committee (treasury,
        2026-10-01 -- a member with their card on hand sometimes buys for
        another committee, meeting food above all). It never fires for an
        unverified card (that is a hint, weighed in scoring) or for a card
        outside its cohort's dates (that card belongs to someone else now).

        `note` is appended to the reason, so a reviewer can see when the card
        stood against some dissent.
        """
        card = card_number(record.get("details"))
        assignment = self.get(card, record_date(record))
        if assignment is None or not assignment.verified:
            return None
        holder = f" ({assignment.holder})" if assignment.holder else ""
        era = f" {assignment.era}" if assignment.era else ""
        return Classification(
            committee_id=assignment.committee_id,
            purpose=COMMITTEE_PURPOSE.get(assignment.committee_id, "Misc."),
            rule=f"Card {card}{holder} — on the{era} card roster{note}",
            confidence=CARD_DEFAULT_CONFIDENCE,
            source="rule",
        )

    def with_unverified(self, card: str, committee_id: int, holder: str = "") -> "CardRegistry":
        """A copy with one extra inferred assignment. Never overwrites a confirmed one."""
        if card in self._cards and self._cards[card].verified:
            return self
        merged = dict(self._cards)
        merged[card] = CardAssignment(committee_id, holder, verified=False)
        return CardRegistry(merged)


@dataclass(frozen=True)
class Signal:
    """One piece of evidence voting for one committee."""

    committee_id: int
    weight: float
    reason: str

    def describe(self) -> str:
        return f"{self.reason} (+{self.weight:.2f} → {committee_name(self.committee_id)})"


@dataclass
class ScoredResult:
    """The full picture: what won, by how much, and on what evidence."""

    signals: list[Signal] = field(default_factory=list)
    totals: dict[int, float] = field(default_factory=dict)

    @property
    def ranked(self) -> list[tuple[int, float]]:
        return sorted(self.totals.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def winner(self) -> int | None:
        ranked = self.ranked
        return ranked[0][0] if ranked else None

    @property
    def top_score(self) -> float:
        ranked = self.ranked
        return ranked[0][1] if ranked else 0.0

    @property
    def runner_up_score(self) -> float:
        ranked = self.ranked
        return ranked[1][1] if len(ranked) > 1 else 0.0

    @property
    def dominance(self) -> float:
        """
        How decisively the winner beat the runner-up, in [0.5, 1.0].

        Measured as the winner's margin *relative to its own score*, not as its
        share of the contested total. The share form was tried first and was
        too punishing: a well-corroborated winner (three agreeing signals) that
        happened to attract one weak dissenter fell below the threshold and got
        flagged, even though the evidence was one-sided. Margin-relative keeps
        a decisive win decisive while still collapsing to 0.5 on a true tie --
        which is what pushes a genuine conflict into the review queue.
        """
        if self.top_score <= 0:
            return 0.0
        margin = (self.top_score - self.runner_up_score) / self.top_score
        return 0.5 + 0.5 * margin

    @property
    def evidence_strength(self) -> float:
        """Absolute corroboration, capped so weak signals cannot pile up into certainty."""
        return min(1.0, self.top_score / EVIDENCE_SATURATION)

    @property
    def confidence(self) -> float:
        return round(self.evidence_strength * self.dominance, 4)

    @property
    def is_confident(self) -> bool:
        return self.winner is not None and self.confidence >= AUTO_APPLY_THRESHOLD

    def conflicting(self) -> list[tuple[int, float]]:
        """Runner-up candidates with real support -- what makes this contested."""
        return [(cid, score) for cid, score in self.ranked[1:] if score > 0]

    def explain(self) -> str:
        """One line a treasurer can act on, naming the competing reading."""
        if self.winner is None:
            return "No signal could be read from this transaction."
        parts = [
            f"{committee_name(self.winner)} scored {self.top_score:.2f}"
            f" (confidence {self.confidence:.0%})"
        ]
        contest = self.conflicting()
        if contest:
            rival, score = contest[0]
            parts.append(f"contested by {committee_name(rival)} at {score:.2f}")
        return "; ".join(parts) + "."

    def as_classification(self) -> Classification:
        """The scored outcome in the shape the rest of the pipeline speaks."""
        if self.winner is None:
            from .predicates import UNMATCHED

            return UNMATCHED
        return Classification(
            committee_id=self.winner,
            purpose=COMMITTEE_PURPOSE.get(self.winner, "Misc."),
            rule=f"Scored: {self.explain()}",
            confidence=self.confidence,
            source="scored",
        )


def collect_signals(record: dict, *, cards: CardRegistry | None = None) -> list[Signal]:
    """
    Every piece of evidence this transaction offers.

    Deliberately additive and order-independent: unlike a rule chain, adding a
    signal here cannot change what an unrelated signal does.
    """
    registry = cards or CardRegistry()
    details = record.get("details")
    signals: list[Signal] = []

    card = card_number(details)
    assignment = registry.get(card, record_date(record))
    if assignment is not None:
        label = "on the card roster" if assignment.verified else "inferred, unconfirmed"
        holder = f" ({assignment.holder})" if assignment.holder else ""
        signals.append(
            Signal(assignment.committee_id, assignment.weight, f"Card {card}{holder} — {label}")
        )

    is_bar = looks_like_bar(details)
    is_food = looks_like_food_merchant(details)

    # NOTE: a bar no longer votes for Membership.
    #
    # `Signal(5, W_BAR_MERCHANT, ...)` used to sit here at weight 2.5, asserting
    # that bar and liquor spend belongs to Membership. Measured against real
    # human decisions that signal had **38% precision**, and fitting the weights
    # against the label set gave it **0.00** -- it earned nothing. The largest
    # single confusion it caused was Membership vs Consulting, because bar and
    # restaurant spend on consulting projects is ordinary.
    #
    # The detection itself is still worth having, because knowing a merchant is
    # a bar is real information -- it is just not information about *which*
    # committee. So it keeps its one defensible job below: suppressing the
    # meeting-food reading. Which committee a bar charge belongs to is decided by
    # the card or the memo -- and failing both, by a human.
    if is_food and not is_bar:
        signals.append(Signal(8, W_FOOD_MERCHANT, "Food merchant"))

        weekday = weekday_from_details(details, record.get("transaction_date"))
        if weekday in MEETING_WEEKDAYS:
            signals.append(Signal(8, W_MEETING_WEEKDAY, f"Purchased on a {weekday}"))
            signals.append(
                Signal(
                    8,
                    W_MEETING_CONJUNCTION,
                    f"Food on a {weekday} is the GBM signature",
                )
            )

        amount = parse_amount(record.get("amount"))
        if amount is not None:
            magnitude = abs(float(amount))
            if magnitude >= CATERING_MINIMUM:
                signals.append(
                    Signal(8, W_CATERING_SIZED, f"Catering-sized (${magnitude:,.2f})")
                )
            elif magnitude <= INCIDENTAL_MAXIMUM:
                # Too small to be feeding a chapter meeting -- nudges away from 8
                # by supporting the social/incidental reading instead.
                signals.append(
                    Signal(5, W_INCIDENTAL_SIZED, f"Too small for catering (${magnitude:,.2f})")
                )

    return signals


def score(record: dict, *, cards: CardRegistry | None = None) -> ScoredResult:
    """Collect evidence and tally it per committee."""
    signals = collect_signals(record, cards=cards)
    totals: dict[int, float] = {}
    for signal in signals:
        if signal.committee_id not in COMMITTEE_BY_ID:
            continue
        totals[signal.committee_id] = totals.get(signal.committee_id, 0.0) + signal.weight
    return ScoredResult(signals=signals, totals=totals)
