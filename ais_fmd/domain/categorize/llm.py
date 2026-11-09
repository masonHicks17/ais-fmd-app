"""
The model pass -- batched, schema-constrained, and loud about failure.

Three findings are addressed here.

FINDING F8. The original wrapped the entire call in `except Exception: return []`,
so a missing key, a rate limit, a network error and malformed JSON all produced
an empty list that was indistinguishable from "the model matched nothing". The
treasurer saw an uncategorized table with no reason to retry. Failures are now
returned as a typed `LLMOutcome` carrying the error, and the UI surfaces it.

FINDING F9. Every transaction went into one unbounded prompt with no chunking,
so a large statement silently exceeded the context window -- and one malformed
response discarded the results for the whole file. Work is now chunked, and a
failed batch only loses that batch.

FINDING (cost). The prompt encoded seven rules, five of which were then
re-implemented in Python and always overrode the model. Only the residual --
rows no deterministic rule and no merchant memory could resolve -- reaches this
module now, and the prompt only describes the distinction that is genuinely
ambiguous.

SPEND REVIEW. A second, separate pass: `review_spend` asks whether a booked
transaction *fits* its committee, using config/spend_review_prompt.md as the
system prompt. It returns verdicts and never a category -- categorization and
spend review must not change each other's output.

In sandbox mode this module never makes a network call. `openai` is not even
installed in the sandbox venv.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ... import settings
from ...config.categories import LINE_BY_ID as COMMITTEE_BY_ID, line_name as committee_name
from .predicates import (
    COMMITTEE_PURPOSE,
    Classification,
    extract_purchase_date,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)

# Any committee. The residual used to be only unknown merchants; since
# 2026-10-01 it also takes the rows the rules flagged as contested -- a dues
# amount whose memo says something else, a Consulting card at a meeting-day
# catering order -- and those can belong anywhere.
ALLOWED_COMMITTEES = tuple(sorted(COMMITTEE_BY_ID))

_SYSTEM_PROMPT = """\
You categorize bank transactions for a university student organization.

Deterministic rules have already been applied upstream. Every transaction you \
are given is one they could not settle: either nothing recognised it, or the \
evidence conflicts. When the rules noticed something, it is given as \
Upstream_Note -- read it first; it says what the conflict is.

Choose exactly one committee ID per transaction:
  1  Dues                    a member paying membership dues
  2  Treasury                bank fees, the treasurer's own operating costs
  3  Transfers               the chapter moving its own money between accounts
  4  President               president's discretionary spending
  5  Membership              bars, social venues, member social spending
  6  Operations              the VP of Operations' own committee spending (formerly Corporate Relations)
  7  Consulting              consulting-project costs, including catering its own events
  8  Meeting Food            groceries or restaurants bought for a general body meeting
  9  Marketing               advertising, flyers, design tools
  10 Professional Development  conferences, training, career events, headshots
  11 Sponsorship / Donation  money received from a sponsor or donor
  12 Overhead                general operating costs no committee owns
  13 Merch                   apparel, printing, promotional goods, or a member paying for them
  14 Road Trip               travel, lodging, transport, or a member paying for a trip
  15 Technology              software, hosting, domains, hardware
  16 Passport                the ISOM Passport program
  17 Refunded                money coming back from a merchant
  18 Formal                  the formal / semi-formal, or a member paying for a ticket
  0  Unknown                 genuinely cannot tell

Rules of thumb:
  - Weekday_Hint of Tuesday or Wednesday plus a food merchant suggests 8 -- \
but if the note says the card belongs to Consulting, a large order is often \
Consulting catering its own event. Only choose 8 over the card with a reason.
  - The VP of Operations' card buys mostly meeting food (8). Her own \
committee (6) gets the occasional rewards or appreciation outing -- a sit-down \
restaurant bill for a few people rather than a meeting-sized order.
  - A bar, brewery or liquor store is 5, never 8.
  - An incoming payment at a dues amount is dues unless its memo clearly says \
it paid for something else; then choose that.
  - Report confidence honestly. Anything under 0.75 is sent to a human with \
your suggestion, which is the right outcome when you are unsure.
  - When the transaction is unrecognisable, answer 0 rather than guessing.

Respond with JSON only: {"results":[{"index":<int>,"committee_id":<int>,\
"confidence":<0.0-1.0>,"reason":"<short>"}]}\
"""


@dataclass
class LLMOutcome:
    """What happened, in enough detail for the UI to explain it."""

    classifications: dict[int, Classification] = field(default_factory=dict)
    batches_attempted: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    prompt_chars: int = 0

    @property
    def ran(self) -> bool:
        return self.batches_attempted > 0

    @property
    def partially_failed(self) -> bool:
        return self.batches_failed > 0 and self.batches_failed < self.batches_attempted

    @property
    def fully_failed(self) -> bool:
        return self.batches_attempted > 0 and self.batches_failed == self.batches_attempted


def _render_row(index: int, record: dict) -> str:
    details = str(record.get("details") or "")
    weekday = weekday_from_details(details, record.get("transaction_date"))
    hints = []
    if looks_like_food_merchant(details):
        hints.append("food-merchant-keyword")
    if looks_like_bar(details):
        hints.append("bar-keyword")
    line = (
        f'{index}. Details: {details[:180]} | Amount: {record.get("amount")} | '
        f'Account: {record.get("account") or ""} | Weekday_Hint: {weekday} | '
        f'Local_Hints: {",".join(hints) or "none"}'
    )
    note = str(record.get("upstream_note") or "")
    return f"{line} | Upstream_Note: {note[:200]}" if note else line


def _load_results(payload: str) -> list:
    """The `results` list of a JSON reply, tolerating a ```json fence."""
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()

    data = json.loads(text)
    return data.get("results", []) if isinstance(data, dict) else []


def _parse_response(payload: str, batch: list[tuple[int, dict]]) -> dict[int, Classification]:
    """Map a model response back onto the original row positions."""
    results = _load_results(payload)

    positions = {local: original for local, (original, _) in enumerate(batch, start=1)}
    out: dict[int, Classification] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        local_index = item.get("index")
        original_index = positions.get(local_index)
        if original_index is None:
            continue
        try:
            committee_id = int(item.get("committee_id", 0))
        except (TypeError, ValueError):
            continue
        if committee_id not in ALLOWED_COMMITTEES:
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        reason = str(item.get("reason") or "").strip()[:120]
        out[original_index] = Classification(
            committee_id=committee_id,
            purpose=COMMITTEE_PURPOSE.get(committee_id, "Misc."),
            rule=f"Model: {reason}" if reason else "Model",
            confidence=max(0.0, min(1.0, confidence)),
            source="llm",
        )
    return out


def classify_residual(residual: list[tuple[int, dict]]) -> LLMOutcome:
    """
    Classify the rows nothing else could resolve.

    `residual` is a list of (original_index, record) so results can be mapped
    back onto the caller's ordering without relying on positional alignment.
    """
    outcome = LLMOutcome()
    if not residual:
        return outcome

    client, outcome.skipped_reason = _model_client(
        "OpenAI categorization request",
        disabled="these rows are left uncategorized for the review queue.",
    )
    if client is None:
        return outcome

    model = settings.llm_model()
    batch_size = settings.llm_batch_size()

    for start in range(0, len(residual), batch_size):
        batch = residual[start : start + batch_size]
        listing = "\n".join(
            _render_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        outcome.batches_attempted += 1
        outcome.prompt_chars += len(listing)
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": f"Transactions:\n{listing}"},
                ],
            )
            content = response.choices[0].message.content or ""
            outcome.classifications.update(_parse_response(content, batch))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome.batches_failed += 1
            outcome.errors.append(f"{type(exc).__name__}: {exc}")

    return outcome


def _model_client(what: str, *, disabled: str):
    """
    An OpenAI client, or None with the reason no request may be made.

    Every guard lives here so both passes refuse in exactly the same places:
    disabled, sandbox, package absent.
    """
    if not settings.llm_enabled():
        return None, (
            "Model pass disabled. In sandbox mode no network call is ever made; "
            + disabled
        )

    try:
        settings.assert_external_call_allowed(what)
    except settings.SandboxViolation as exc:
        return None, str(exc)

    try:
        from openai import OpenAI  # imported lazily; absent in the sandbox venv
    except ImportError:
        return None, (
            "The 'openai' package is not installed. The sandbox venv omits it "
            "deliberately so no request can be made."
        )

    import os

    return OpenAI(api_key=os.environ["OPENAI_API_KEY"]), None


# --------------------------------------------------------------------------
# Spend review: does a booked transaction fit its committee?
# --------------------------------------------------------------------------

# Treasurer-owned, like card_roster.json. Read on every run rather than cached,
# so an edit to the file is the next review's prompt without a restart.
SPEND_REVIEW_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "spend_review_prompt.md"
)
VERDICTS = ("fits", "question", "other_committee")
_EDITOR_NOTES_RE = re.compile(r"<!--.*?-->", re.S)


def load_spend_review_prompt(path: Path | None = None) -> str:
    """The review's system prompt: the treasurer's file, minus its editor's notes."""
    text = (path or SPEND_REVIEW_PROMPT_FILE).read_text(encoding="utf-8")
    return _EDITOR_NOTES_RE.sub("", text).strip()


@dataclass(frozen=True)
class SpendVerdict:
    """The model's view of one booked transaction. Deliberately has no category."""

    verdict: str  # one of VERDICTS
    suggested_committee_id: int | None
    confidence: float
    reason: str
    message_to_vp: str

    @property
    def flagged(self) -> bool:
        return self.verdict != "fits"


@dataclass
class SpendReviewOutcome:
    verdicts: dict[int, SpendVerdict] = field(default_factory=dict)
    # Rows with no committee. The prompt reviews bookings, so these are the
    # categorizer's (or a human's) to settle first.
    not_reviewed: list[int] = field(default_factory=list)
    batches_attempted: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    prompt_chars: int = 0

    @property
    def ran(self) -> bool:
        return self.batches_attempted > 0

    @property
    def flagged(self) -> dict[int, SpendVerdict]:
        return {index: v for index, v in self.verdicts.items() if v.flagged}


def _booked_committee(record: dict) -> int | None:
    value = record.get("budget_category")
    try:
        committee_id = int(value)
    except (TypeError, ValueError):
        return None
    return committee_id if committee_id in COMMITTEE_BY_ID else None


def _render_review_row(index: int, record: dict) -> str:
    """One row as the prompt's 'Reading the bank line' section expects it."""
    details = " ".join(str(record.get("details") or "").split())
    committee_id = _booked_committee(record)
    posted = record.get("transaction_date") or ""
    purchased = extract_purchase_date(details)
    if purchased:
        weekday = weekday_from_details(details, posted)
    else:
        # No embedded purchase date (Zelle, Venmo, fees): the row date is it.
        when = pd.to_datetime(posted, errors="coerce")
        weekday = "Unknown" if pd.isna(when) else when.day_name()
    return (
        f"{index}. Booked_To: {committee_id} {committee_name(committee_id)} | "
        f"Posted: {posted} | Purchased: {purchased or 'same'} ({weekday}) | "
        f"Amount: {record.get('amount')} | Account: {record.get('account') or ''} | "
        f"Details: {details[:180]}"
    )


def _parse_review_response(
    payload: str, batch: list[tuple[int, dict]]
) -> dict[int, SpendVerdict]:
    """Map a review reply back onto the original row positions."""
    positions = {local: original for local, (original, _) in enumerate(batch, start=1)}
    out: dict[int, SpendVerdict] = {}
    for item in _load_results(payload):
        if not isinstance(item, dict):
            continue
        original_index = positions.get(item.get("index"))
        verdict = str(item.get("verdict") or "").strip()
        if original_index is None or verdict not in VERDICTS:
            continue
        suggested = None
        if verdict == "other_committee":
            try:
                suggested = int(item.get("suggested_committee_id"))
            except (TypeError, ValueError):
                suggested = None
            if suggested not in COMMITTEE_BY_ID:
                suggested = None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        message = str(item.get("message_to_vp") or "").strip()
        out[original_index] = SpendVerdict(
            verdict=verdict,
            suggested_committee_id=suggested,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(item.get("reason") or "").strip()[:200],
            message_to_vp=message[:400] if verdict != "fits" else "",
        )
    return out


def review_spend(
    rows: list[tuple[int, dict]],
    *,
    dues_baseline: str | None = None,
) -> SpendReviewOutcome:
    """
    Ask whether each booked transaction fits its committee.

    `rows` is (original_index, record), as for `classify_residual`; a record
    carries `budget_category` -- the committee it is booked to. `dues_baseline`
    is `dues.DuesBaseline.render()` for the term under review; without it the
    prompt has the model infer the rate from the batch.

    Returns verdicts only. Nothing here writes, or returns, a category.
    """
    outcome = SpendReviewOutcome()
    reviewable = []
    for index, record in rows:
        if _booked_committee(record) is None:
            outcome.not_reviewed.append(index)
        else:
            reviewable.append((index, record))
    if not reviewable:
        return outcome

    client, outcome.skipped_reason = _model_client(
        "OpenAI spend review request",
        disabled="no transactions are reviewed.",
    )
    if client is None:
        return outcome

    system_prompt = load_spend_review_prompt()
    model = settings.llm_model()
    batch_size = settings.llm_batch_size()
    baseline = f"\n\n{dues_baseline.strip()}" if dues_baseline else ""

    for start in range(0, len(reviewable), batch_size):
        batch = reviewable[start : start + batch_size]
        listing = "\n".join(
            _render_review_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        outcome.batches_attempted += 1
        outcome.prompt_chars += len(listing)
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Transactions:\n{listing}{baseline}"},
                ],
            )
            content = response.choices[0].message.content or ""
            outcome.verdicts.update(_parse_review_response(content, batch))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome.batches_failed += 1
            outcome.errors.append(f"{type(exc).__name__}: {exc}")

    return outcome
