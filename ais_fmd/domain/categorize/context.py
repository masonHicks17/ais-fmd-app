"""
Module M20 -- the model's context, generated from human decisions.

THE POINT. A hand-written prompt describing the categorisation rules goes stale
the moment an officer changes or a rule is corrected, and nobody remembers to
update it. This builds the model's briefing from `labeled_examples` instead, so
it always reflects what has actually been decided. Working the review queue
updates the prompt as a side effect.

WHAT GOES IN, AND WHY EACH PART EARNS ITS TOKENS:

  * the committee vocabulary          -- the model cannot pick an id it has not been given
  * the current card roster           -- the single most decisive feature, and it changes yearly
  * merchants already decided         -- prevents re-litigating settled questions
  * the closest past decisions        -- lets the model reason from precedent rather than from rules
  * known confusions                  -- the specific mistakes the scorer makes, so the model checks them

The last two are why this beats a static document. Retrieval means the model
sees "here is what you decided about this exact merchant last March", which is
the thing that actually transfers judgement.

COST. This only ever runs on the residual -- rows the exact rules, merchant
memory and scoring could not settle, currently well under a quarter of a
statement. Retrieval is capped so a large label set cannot inflate the prompt
without bound.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from ...config.categories import LINE_BY_ID as COMMITTEE_BY_ID, line_name as committee_name
from .merchants import merchant_key
from .scoring import CardRegistry, card_number, current_era

# Caps, so the prompt cannot grow without bound as labels accumulate.
MAX_MERCHANTS = 40
MAX_PRECEDENTS = 8
MAX_CONFUSIONS = 6


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(t) > 2}


@dataclass(frozen=True)
class Precedent:
    """A past human decision, with why it was retrieved."""

    details: str
    committee_id: int
    purpose: str | None
    overlap: int

    def render(self) -> str:
        purpose = f" / {self.purpose}" if self.purpose else ""
        return f'  "{self.details[:110]}" -> {committee_name(self.committee_id)}{purpose}'


def merchant_decisions(labels: list[dict], limit: int = MAX_MERCHANTS) -> list[tuple[str, int, int]]:
    """
    (merchant key, committee, times decided) for merchants a human has settled.

    Merchants decided *inconsistently* are excluded rather than reported at
    their majority: telling the model "PUBLIX is Meeting Food" when it is
    Meeting Food 6 times and Membership 5 would teach it a confidence nobody
    has. Those belong in the contested list instead.
    """
    tally: dict[str, Counter] = defaultdict(Counter)
    for label in labels:
        committee_id = label.get("committee_id")
        if committee_id is None:
            continue
        key = merchant_key(label.get("details"))
        if key:
            tally[key][int(committee_id)] += 1

    settled: list[tuple[str, int, int]] = []
    for key, counts in tally.items():
        total = sum(counts.values())
        committee_id, top = counts.most_common(1)[0]
        if total >= 2 and top / total >= 0.8:
            settled.append((key, committee_id, total))
    settled.sort(key=lambda item: -item[2])
    return settled[:limit]


def contested_merchants(labels: list[dict]) -> list[tuple[str, dict[int, int]]]:
    """Merchants a human has split between committees -- genuinely ambiguous ones."""
    tally: dict[str, Counter] = defaultdict(Counter)
    for label in labels:
        committee_id = label.get("committee_id")
        if committee_id is None:
            continue
        key = merchant_key(label.get("details"))
        if key:
            tally[key][int(committee_id)] += 1

    out = []
    for key, counts in tally.items():
        total = sum(counts.values())
        if total >= 3 and counts.most_common(1)[0][1] / total < 0.8:
            out.append((key, dict(counts)))
    return sorted(out, key=lambda item: -sum(item[1].values()))


def find_precedents(
    details: object, labels: list[dict], limit: int = MAX_PRECEDENTS
) -> list[Precedent]:
    """
    The past decisions most similar to this description.

    Similarity is token overlap -- crude, but it is matching bank descriptions
    against bank descriptions, where the merchant name is the bulk of the
    signal. An exact merchant-key match is ranked above any token overlap.
    """
    target_tokens = _tokens(details)
    target_key = merchant_key(details)
    if not target_tokens:
        return []

    scored: list[Precedent] = []
    for label in labels:
        committee_id = label.get("committee_id")
        if committee_id is None:
            continue
        candidate = label.get("details")
        overlap = len(target_tokens & _tokens(candidate))
        if target_key and merchant_key(candidate) == target_key:
            overlap += 10
        if overlap > 0:
            scored.append(
                Precedent(
                    details=str(candidate),
                    committee_id=int(committee_id),
                    purpose=label.get("purpose"),
                    overlap=overlap,
                )
            )

    scored.sort(key=lambda p: -p.overlap)
    # One example per (merchant, committee) pair, so a merchant with 30 past
    # decisions cannot crowd out every other precedent.
    seen: set[tuple[str, int]] = set()
    unique: list[Precedent] = []
    for precedent in scored:
        identity = (merchant_key(precedent.details), precedent.committee_id)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(precedent)
        if len(unique) >= limit:
            break
    return unique


def card_roster() -> list[str]:
    """The current cohort's cards, from config/card_roster.json."""
    era = current_era()
    lines = []
    for card, assignment in CardRegistry().items():
        if assignment.era and assignment.era != era:
            continue
        holder = assignment.holder or "unknown holder"
        status = "" if assignment.verified else " (unconfirmed)"
        lines.append(
            f"  card {card} — {holder}, {committee_name(assignment.committee_id)}{status}"
        )
    return lines


def build_context(
    labels: list[dict],
    *,
    allowed_committees: tuple[int, ...] | None = None,
) -> str:
    """The standing briefing: vocabulary, roster, and settled decisions."""
    committees = allowed_committees or tuple(sorted(COMMITTEE_BY_ID))
    parts: list[str] = []

    parts.append("COMMITTEES you may choose from:")
    for committee_id in committees:
        committee = COMMITTEE_BY_ID.get(committee_id)
        if committee:
            parts.append(f"  {committee_id:>2} {committee.name}")

    parts.append("")
    parts.append(
        "CARDS currently issued. A purchase on a confirmed card is already booked "
        "to that\ncard's committee and is never sent to you; a card not listed "
        "here says nothing\nabout which committee a purchase belongs to:"
    )
    parts.extend(card_roster())

    settled = merchant_decisions(labels)
    if settled:
        parts.append("")
        parts.append("MERCHANTS already settled by a human (follow these):")
        for key, committee_id, count in settled:
            parts.append(f"  {key} -> {committee_name(committee_id)}  ({count} decisions)")

    contested = contested_merchants(labels)
    if contested:
        parts.append("")
        parts.append(
            "MERCHANTS a human has split between committees -- do not assume, "
            "weigh the\nother evidence and say so if it is genuinely unclear:"
        )
        for key, counts in contested[:MAX_CONFUSIONS]:
            breakdown = ", ".join(
                f"{committee_name(cid)} x{n}" for cid, n in sorted(counts.items(), key=lambda kv: -kv[1])
            )
            parts.append(f"  {key}: {breakdown}")

    return "\n".join(parts)


def build_prompt_for(details: object, labels: list[dict], **kwargs) -> str:
    """Standing context plus the precedents most relevant to one transaction."""
    sections = [build_context(labels, **kwargs)]
    precedents = find_precedents(details, labels)
    if precedents:
        sections.append("")
        sections.append("PAST DECISIONS closest to the transaction in question:")
        sections.extend(precedent.render() for precedent in precedents)
    return "\n".join(sections)
