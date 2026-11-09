"""
Module M4 -- merchant memory.

The highest-leverage cost reduction available, because most transactions are
repeat merchants. Once "PUBLIX #1234 GAINESVILLE FL" has been resolved once, it
never needs a model again.

A merchant key is the bank description with all the per-transaction noise
stripped out: the purchase date, card number, transaction ID, store number,
trailing city/state, and reference numbers. What remains is the stable part --
the merchant name.

Corrections made in the review queue write back here, so the set of rows that
still need a model shrinks every time the treasurer works the queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .predicates import Classification

# Noise patterns, stripped in order.
#
# Every pattern uses \s+ rather than a literal space: real Wells Fargo
# descriptions pad with runs of spaces ("PURCHASE      AUTHORIZED ON   05/17"),
# and single-space patterns silently matched nothing on real statements.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:purchase|recurring\s+payment)\s+authorized\s+on\s+\d{1,2}/\d{1,2}", re.I),
    re.compile(r"\bcard\s+\d{3,5}\b", re.I),
    re.compile(r"\bxxxx\d+\b", re.I),
    re.compile(r"\bref\s*#?\s*\w+\b", re.I),        # "REF # JFR0TQ6QPUIM"
    re.compile(r"\b[a-z]\d{3,}\b", re.I),           # reference codes: S3045
    re.compile(r"\b\d{10,}\b"),                     # long numeric ids
    re.compile(r"\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b"),  # phone numbers
    re.compile(r"#\s*\d+"),                         # store numbers
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # dates
    re.compile(r"\brecurring\s+payment\b", re.I),
    re.compile(r"\bpurchase\b", re.I),
    re.compile(r"\bauthorized\b", re.I),
    re.compile(r"^\s*on\b", re.I),                  # leftover "ON" once the date goes
)

# Trailing two-letter state code only.
#
# This previously tried to strip the whole "CITY ST" suffix with
# `\s+[a-z .'-]+\s+(state)$`, but `[a-z .'-]+` is greedy and ate the merchant
# name as well: "WAL-MART SUPERCENTER GAINESVILLE FL" reduced to "wal", and
# "TST* HUEY MAGOOS ..." to "tst". Keys that short collide across unrelated
# merchants -- one "tst" rule would have mis-categorised every Toast-POS
# restaurant at once. Only the state code is stripped now; the token cap below
# does the rest of the work, and over-splitting is safe where collisions are not.
_TRAILING_STATE = re.compile(
    r"\s+(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|"
    r"ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    r"wa|wv|wi|wy)\s*$",
    re.I,
)

# Point-of-sale processor prefixes sitting in front of the real merchant name:
# "TST* MI APA", "SQ *COFFEE", "PP*STORE". The asterisk is required -- without
# it the pattern would strip legitimate leading words like "sp" in "sports".
_POS_PREFIX = re.compile(r"^(?:tst|sq|sp|pp|py|paypal|ecs|dd|ab)\s*\*\s*", re.I)

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")
# Trailing store/location numbers: "chipotle 1462" and "chipotle 1463" are the
# same merchant, so the number is dropped to let one rule cover both.
_TRAILING_DIGITS = re.compile(r"(?:\s+\d+)+$")

MIN_KEY_LENGTH = 4
# Enough tokens to stay distinctive. Splitting one merchant across two keys just
# means learning two rules; merging two merchants into one key mis-categorises.
MAX_KEY_TOKENS = 3

# Words that are never a merchant on their own. A single-token key from this set
# is rejected rather than becoming a rule that matches half the statement.
_STOPWORD_KEYS = frozenset(
    {
        "the", "and", "for", "from", "with", "inc", "llc", "corp", "co",
        "on", "ref", "tst", "sq", "sp", "pos", "pay", "payment", "purchase",
        "return", "refund", "deposit", "withdrawal", "transfer", "check",
        "debit", "credit", "card", "online", "mobile", "recurring", "www",
    }
)

# Person-to-person transfers have no merchant to remember. Learning from them
# would create one useless rule per member -- and worse, a rule keyed on a
# member's name that then mis-categorises unrelated payments from that person.
# These rows are handled by the deterministic dues/refund/formal rules instead.
#
# Venmo details are pipe-joined as "txn id | note | from | to".
_VENMO_SHAPED = re.compile(r"^\s*\d{10,}\s*\|")
# Zelle rows read "ZELLE FROM JANE DOE ON 07/08 REF # ..." -- confirmed against
# a real statement, where these were producing merchant keys like
# "zelle from doe jane on ref".
_TRANSFER_SHAPED = re.compile(r"^\s*(?:zelle|venmo)\s+(?:from|to)\b", re.I)


def merchant_key(details: object) -> str:
    """
    Reduce a bank description to a stable merchant key.

    Returns "" when nothing usable survives, which callers treat as
    "not eligible for merchant memory".
    """
    if details is None:
        return ""
    # Collapse the runs of spaces real statements use, before anything else.
    text = _WHITESPACE.sub(" ", str(details)).strip()

    if _VENMO_SHAPED.match(text) or _TRANSFER_SHAPED.match(text):
        return ""
    if "|" in text:
        parts = [part.strip() for part in text.split("|") if not part.strip().isdigit()]
        text = max(parts, key=len) if parts else ""
        if not text:
            return ""

    text = text.lower()
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _POS_PREFIX.sub("", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _TRAILING_STATE.sub("", text).strip()
    text = _TRAILING_DIGITS.sub("", text).strip()

    # Drop standalone numeric tokens -- store numbers like "CHIPOTLE 1462" vs
    # "CHIPOTLE 1893" are the same merchant. The first token is kept so names
    # that genuinely begin with a number ("7 ELEVEN", "5 GUYS") survive.
    tokens = [
        token
        for index, token in enumerate(text.split())
        if token and (index == 0 or not token.isdigit())
    ]
    if not tokens:
        return ""

    key = " ".join(tokens[:MAX_KEY_TOKENS])
    if len(key) < MIN_KEY_LENGTH:
        return ""
    # A single generic word would match far too much to be a safe rule.
    if len(tokens) == 1 and tokens[0] in _STOPWORD_KEYS:
        return ""
    return key


# How consistent a merchant's history must be before its mapping is applied
# without asking, and how many decisions that judgement needs to rest on.
#
# WHY THIS EXISTS. Merchant memory used to store one committee per merchant and
# apply it at 0.95 confidence, short-circuiting everything else. That is right
# for a merchant which genuinely only ever means one thing -- a website host is
# always Technology, a print shop is always Merch -- and wrong for one that
# legitimately spans committees. A bar can be a Membership social, a Consulting
# client dinner, or a Professional Development event, and the flat mapping had
# no way to express that: whichever committee was confirmed last won every
# future transaction outright.
#
# The evidence for this is in the project's own measurements. `salty dog saloon`
# is settled as **Consulting** across 10 human decisions, while the hardcoded
# "bars are Membership" signal measured 38% precision and earned a fitted weight
# of exactly 0.00. A single stored answer cannot represent "7 Consulting, 3
# Membership" -- and that split is the useful thing to know.
#
# So a rule now carries the whole distribution. Consistent merchants
# short-circuit exactly as before; split ones contribute weighted evidence to
# scoring instead and let the surrounding context (card, memo, weekday, amount)
# break the tie, which is what routes them to a human rather than a coin-flip.
CONSISTENCY_THRESHOLD = 0.8
MIN_DECISIONS_TO_TRUST = 3


def parse_committee_counts(text: object) -> dict[int, int]:
    """
    Read the stored distribution: "5:3,7:7" -> {5: 3, 7: 7}.

    A compact string rather than a join table, matching how `alt_keys` already
    stores its list on `members`. Unparseable fragments are skipped rather than
    raising -- a malformed counter must degrade to "no history", never take down
    categorisation for every row.
    """
    counts: dict[int, int] = {}
    for part in str(text or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        committee, _, tally = part.partition(":")
        try:
            key, value = int(committee), int(tally)
        except ValueError:
            continue
        if value > 0:
            counts[key] = counts.get(key, 0) + value
    return counts


def format_committee_counts(counts: dict[int, int]) -> str:
    """The inverse of `parse_committee_counts`, sorted so the value is stable."""
    return ",".join(f"{c}:{n}" for c, n in sorted(counts.items()) if n > 0)


@dataclass(frozen=True)
class MerchantRule:
    key: str
    canonical_name: str
    committee_id: int
    purpose: str
    hit_count: int = 0
    source: str = "learned"
    # committee_id -> how many times a human filed this merchant there. Empty
    # for a rule predating this column; `decisions` falls back to treating the
    # stored `committee_id` as a single decision so old rows still behave.
    committee_counts: dict[int, int] = field(default_factory=dict)

    @property
    def decisions(self) -> dict[int, int]:
        """The distribution, with a sensible reading of a legacy single-answer row."""
        if self.committee_counts:
            return self.committee_counts
        return {self.committee_id: max(self.hit_count, 1)}

    @property
    def decision_count(self) -> int:
        return sum(self.decisions.values())

    @property
    def consistency(self) -> float:
        """Share of decisions that went to the most common committee. 0.0-1.0."""
        decisions = self.decisions
        total = sum(decisions.values())
        if not total:
            return 0.0
        return max(decisions.values()) / total

    @property
    def dominant_committee(self) -> int:
        """The most-chosen committee; ties break on the lowest id, for determinism."""
        decisions = self.decisions
        if not decisions:
            return self.committee_id
        return min(decisions.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    @property
    def is_disputed(self) -> bool:
        """More than one committee has ever been chosen for this merchant."""
        return len([n for n in self.decisions.values() if n > 0]) > 1

    @property
    def is_settled(self) -> bool:
        """
        Consistent enough to apply without asking.

        **A unanimous merchant is settled at any count, including one.** That is
        deliberate, and an earlier version of this got it wrong by requiring
        three decisions unconditionally: it broke the case the feature exists
        for. When a treasurer ticks "Remember this merchant" in the review
        queue, that is an *instruction*, not an observation -- they are telling
        the app that a website host is Technology. Making them say it three
        times before it takes effect is the opposite of learning from them.

        The threshold applies only once a merchant is **disputed** -- once two
        different committees have genuinely been chosen for it. Then, and only
        then, does the count matter: one dissenting decision against nine should
        not unsettle a merchant, but a 4/3 split should, and that is a question
        about the weight of evidence rather than about the merchant's identity.
        """
        if not self.is_disputed:
            return self.decision_count >= 1
        return (
            self.decision_count >= MIN_DECISIONS_TO_TRUST
            and self.consistency >= CONSISTENCY_THRESHOLD
        )

    def explain_split(self) -> str:
        """'7x Consulting, 3x Membership' -- what a treasurer needs to see."""
        from ...config.categories import line_name as committee_name

        parts = sorted(self.decisions.items(), key=lambda kv: (-kv[1], kv[0]))
        return ", ".join(f"{n}x {committee_name(c)}" for c, n in parts)


class MerchantMemory:
    """In-memory index over the merchants table."""

    def __init__(self, rules: list[MerchantRule] | None = None) -> None:
        self._rules: dict[str, MerchantRule] = {}
        for rule in rules or []:
            self._rules[rule.key] = rule

    @classmethod
    def from_records(cls, records: list[dict]) -> "MerchantMemory":
        rules = [
            MerchantRule(
                key=str(record["merchant_key"]),
                canonical_name=str(record.get("canonical_name") or record["merchant_key"]),
                committee_id=int(record["committee_id"]),
                purpose=str(record.get("purpose") or ""),
                hit_count=int(record.get("hit_count") or 0),
                source=str(record.get("source") or "learned"),
                committee_counts=parse_committee_counts(record.get("committee_counts")),
            )
            for record in records
            if record.get("merchant_key") and record.get("committee_id") is not None
        ]
        return cls(rules)

    @classmethod
    def from_labels(cls, labels: list[dict]) -> "MerchantMemory":
        """
        Build memory from the decision log rather than the merchants table.

        `labeled_examples` records every review-queue decision with its original
        description and the committee the human chose, so the distribution can be
        derived from what actually happened instead of from a denormalised
        counter that can drift out of step with it. This is the honest source;
        the merchants table is the cache.

        Rows whose description yields no stable merchant key -- Venmo and Zelle
        transfers, which are person-specific -- are skipped, for the same reason
        `remember` refuses them: a rule keyed on one member's name would
        mis-categorise everything they ever pay.
        """
        counts: dict[str, dict[int, int]] = {}
        purposes: dict[str, str] = {}
        for label in labels:
            key = merchant_key(label.get("details"))
            if not key or label.get("committee_id") is None:
                continue
            try:
                committee = int(label["committee_id"])
            except (TypeError, ValueError):
                continue
            counts.setdefault(key, {})
            counts[key][committee] = counts[key].get(committee, 0) + 1
            if label.get("purpose"):
                purposes.setdefault(key, str(label["purpose"]))

        rules = [
            MerchantRule(
                key=key,
                canonical_name=key.title(),
                committee_id=min(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0],
                purpose=purposes.get(key, ""),
                hit_count=sum(tally.values()),
                source="derived",
                committee_counts=tally,
            )
            for key, tally in counts.items()
        ]
        return cls(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def rule_for(self, details: object) -> MerchantRule | None:
        """The stored rule for this description, settled or not."""
        key = merchant_key(details)
        if not key:
            return None
        return self._rules.get(key)

    def lookup(self, details: object) -> Classification | None:
        """
        A confident answer, or None.

        Returns a classification **only for a settled merchant** -- one a human
        has filed consistently, enough times to be a rule rather than an
        anecdote. A contested merchant deliberately returns None here so the row
        falls through to scoring, where `contested_signals` contributes its
        history as weighted evidence alongside the card, the memo and the
        weekday rather than overriding all of them.
        """
        rule = self.rule_for(details)
        if rule is None or not rule.is_settled:
            return None
        committee = rule.dominant_committee
        confidence = 0.95 if rule.consistency == 1.0 else 0.85
        detail = (
            f"Merchant memory: {rule.canonical_name}"
            if rule.consistency == 1.0
            else f"Merchant memory: {rule.canonical_name} ({rule.explain_split()})"
        )
        return Classification(
            committee_id=committee,
            purpose=rule.purpose or None,
            rule=detail,
            confidence=confidence,
            source="merchant",
        )

    def contested_signals(self, details: object) -> list[tuple[int, float, str]]:
        """
        A split merchant's history, as (committee_id, share, reason) triples.

        `share` is that committee's fraction of the decisions, so a 7/3 split
        contributes 0.7 to one committee and 0.3 to the other. The caller
        multiplies by a weight -- keeping the arithmetic in `scoring`, where
        every other weight already lives.

        Empty for a settled merchant (`lookup` already answered) and for an
        unknown one (there is nothing to say).
        """
        rule = self.rule_for(details)
        if rule is None or rule.is_settled:
            return []
        decisions = rule.decisions
        total = sum(decisions.values())
        if total < 2:
            # A single decision is not a distribution. Saying "100% Consulting"
            # off one data point is exactly the overconfidence being removed.
            return []
        return [
            (committee, count / total, f"{rule.canonical_name}: {count} of {total} decisions")
            for committee, count in sorted(decisions.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    def remember(
        self,
        details: object,
        committee_id: int,
        purpose: str | None,
        *,
        canonical_name: str | None = None,
    ) -> MerchantRule | None:
        """Learn a mapping. Returns the rule, or None if the key is unusable."""
        key = merchant_key(details)
        if not key:
            return None
        existing = self._rules.get(key)

        # Accumulate rather than overwrite. The previous version replaced
        # `committee_id` outright, so the most recent decision silently erased
        # every earlier one -- a merchant a treasurer had filed under Consulting
        # nine times became Membership the tenth time somebody chose it there,
        # with no trace that the disagreement had ever happened.
        counts = dict(existing.decisions) if existing else {}
        counts[committee_id] = counts.get(committee_id, 0) + 1

        rule = MerchantRule(
            key=key,
            canonical_name=canonical_name or (existing.canonical_name if existing else key),
            # Kept as the dominant reading so a legacy consumer of this column
            # still sees the merchant's most common committee, not its latest.
            committee_id=min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0],
            purpose=purpose or "",
            hit_count=(existing.hit_count if existing else 0) + 1,
            committee_counts=counts,
        )
        self._rules[key] = rule
        return rule

    def contested(self) -> list[MerchantRule]:
        """Merchants a human has filed inconsistently. The review-worthy set."""
        return sorted(
            (rule for rule in self._rules.values() if not rule.is_settled and rule.decision_count > 1),
            key=lambda rule: (-rule.decision_count, rule.key),
        )

    def settled(self) -> list[MerchantRule]:
        """Merchants consistent enough to apply without asking."""
        return sorted(
            (rule for rule in self._rules.values() if rule.is_settled),
            key=lambda rule: (-rule.decision_count, rule.key),
        )

    def as_records(self) -> list[dict]:
        return [
            {
                "merchant_key": rule.key,
                "canonical_name": rule.canonical_name,
                "committee_id": rule.committee_id,
                "purpose": rule.purpose,
                "hit_count": rule.hit_count,
                "source": rule.source,
                "committee_counts": format_committee_counts(rule.decisions),
            }
            for rule in sorted(self._rules.values(), key=lambda r: -r.hit_count)
        ]
