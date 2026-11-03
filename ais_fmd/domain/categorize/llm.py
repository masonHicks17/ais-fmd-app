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

GUARDRAILS (2026-10-20). Both passes now call Claude through the Anthropic SDK,
and a call happens only inside a metered run:

  * No gate, no call. `_call_model` refuses unless it is handed a
    `metering.ModelGate`, which only `metering.open_run` builds after checking
    the monthly cap, the daily run limit and the one-run-at-a-time lock. The
    Review Queue, the evaluation in `learning.py` and the seed all run the
    pipeline without a gate, so none of them can spend.
  * Bounded. Batches are counted before the first call and each is sent once;
    there is no loop that can ask again. The SDK retries once at most, and the
    gate's worst-case estimate assumes it does.
  * Schema-constrained. Replies must match a JSON schema (structured outputs),
    so a verdict can only be one of three words and a committee only a real ID.
  * Checked against the rows. Every result echoes the row's amount; a batch
    whose indices or amounts do not line up is thrown away whole rather than
    mapped onto the wrong transactions.
  * A refusal or a truncated reply fails its batch. It is reported, never
    retried automatically.

In sandbox mode this module never makes a network call. `anthropic` is not
even installed in the sandbox venv.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from ... import settings
from ...config.categories import COMMITTEE_BY_ID, committee_name
from .metering import ModelGate
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
  - Weekday_Hint of Monday, Tuesday or Wednesday plus a food merchant \
suggests 8 -- but food on a Consulting card is Consulting's own catering (7).
  - The VP of Operations' card buys the meeting food (8). Her own committee \
(6) gets only socials -- a bar, or a sit-down dinner for a few people.
  - A bar, brewery or liquor store is 5, never 8.
  - Dues (1) is a fixed price list. Local_Hints says dues-rate-amount when an \
incoming payment is at one of this term's dues rates, and not-a-dues-amount \
when it is not. Never choose 1 for a not-a-dues-amount row, whatever the memo \
or payer looks like: "AIS" in a memo or a member paying does not make it dues.
  - "SEMI", "SEMIFORMAL" or "FORMAL" in a memo is a semi-formal ticket (18). \
Shirt, crewneck, sweater or hoodie in a memo is merch (13).
  - An incoming payment whose memo is empty or only the payer's name, at a \
not-a-dues-amount, is usually a ticket (18) or merch (13), and the bank line \
cannot say which. Suggest the likelier one with confidence 0.5 or lower so a \
human checks it against the sign-up lists.
  - An incoming payment at a dues amount is dues unless its memo clearly says \
it paid for something else; then choose that.
  - Report confidence honestly. Anything under 0.75 is sent to a human with \
your suggestion, which is the right outcome when you are unsure.
  - When the transaction is unrecognisable, answer 0 rather than guessing.

Respond with JSON only: {"results":[{"index":<int>,"amount":<the row's Amount, \
copied exactly>,"committee_id":<int>,"confidence":<0.0-1.0>,"reason":"<short>"}]} \
-- one result for every transaction.\
"""

_CATEGORIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "amount": {"type": "number"},
                    "committee_id": {"type": "integer", "enum": [0, *ALLOWED_COMMITTEES]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "amount", "committee_id", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

# An echoed amount within half a cent of the row's is the same amount.
_AMOUNT_TOLERANCE = 0.005


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
    if record.get("dues_amount") is True:
        hints.append("dues-rate-amount")
    elif record.get("dues_amount") is False:
        hints.append("not-a-dues-amount")
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


class MisalignedReply(ValueError):
    """The reply does not line up with the rows it was sent, so none of it is used."""


def _check_alignment(results: list, batch: list[tuple[int, dict]]) -> None:
    """
    Raise unless every result names a row of this batch, once, with its amount.

    A model that skips a row is fine -- that row is simply not answered. One
    that repeats an index, invents one, or attaches an answer to the wrong
    amount has lost track of which row is which, and then *no* answer in the
    batch can be trusted to belong where its index says.
    """
    amounts = {local: record.get("amount") for local, (_, record) in enumerate(batch, start=1)}
    seen: set = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if index not in amounts:
            raise MisalignedReply(f"reply names row {index!r}, which was not sent")
        if index in seen:
            raise MisalignedReply(f"reply answers row {index} twice")
        seen.add(index)
        try:
            echoed = float(item.get("amount"))
            sent = float(amounts[index])
        except (TypeError, ValueError):
            raise MisalignedReply(f"row {index} came back without its amount") from None
        if abs(echoed - sent) > _AMOUNT_TOLERANCE:
            raise MisalignedReply(f"row {index} came back with amount {echoed}, but {sent} was sent")


def _parse_response(payload: str, batch: list[tuple[int, dict]]) -> dict[int, Classification]:
    """Map a model response back onto the original row positions."""
    results = _load_results(payload)
    _check_alignment(results, batch)

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


def classify_residual(
    residual: list[tuple[int, dict]], *, gate: ModelGate | None = None
) -> LLMOutcome:
    """
    Classify the rows nothing else could resolve.

    `residual` is a list of (original_index, record) so results can be mapped
    back onto the caller's ordering without relying on positional alignment.
    Without a `gate` nothing is sent: see the module docstring.
    """
    outcome = LLMOutcome()
    if not residual:
        return outcome

    client, outcome.skipped_reason = _model_client(
        "Anthropic categorization request",
        gate=gate,
        disabled="these rows are left uncategorized for the review queue.",
    )
    if client is None:
        return outcome

    batches = _batches(residual)
    for batch in batches:
        listing = "\n".join(
            _render_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        outcome.batches_attempted += 1
        outcome.prompt_chars += len(listing)
        try:
            content = _call_model(
                client,
                gate,
                system=_SYSTEM_PROMPT,
                user=f"Transactions:\n{listing}",
                schema=_CATEGORIZE_SCHEMA,
                cache_system=len(batches) > 1,
            )
            outcome.classifications.update(_parse_response(content, batch))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome.batches_failed += 1
            outcome.errors.append(f"{type(exc).__name__}: {exc}")

    return outcome


def _batches(rows: list) -> list[list]:
    """Every batch the run will send, fixed before the first call."""
    size = settings.llm_batch_size()
    return [rows[start : start + size] for start in range(0, len(rows), size)]


class ModelRefused(RuntimeError):
    """The gate, the model's safety classifier, or a truncated reply stopped a call."""


def _model_client(what: str, *, gate: ModelGate | None, disabled: str):
    """
    An Anthropic client, or None with the reason no request may be made.

    Every guard lives here so both passes refuse in exactly the same places:
    disabled, sandbox, no metered run, package absent.
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

    if gate is None:
        return None, (
            "Not sent: the model is only called from a metered run the treasurer "
            "starts, never from a page load. " + disabled
        )

    try:
        import anthropic  # imported lazily; absent in the sandbox venv
    except ImportError:
        return None, (
            "The 'anthropic' package is not installed. The sandbox venv omits it "
            "deliberately so no request can be made."
        )

    # One retry at most (metering assumes it happens), and a call that hangs
    # is abandoned rather than waited on.
    return anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or settings._setting("ANTHROPIC_API_KEY"),
        max_retries=1,
        timeout=60.0,
    ), None


def _call_model(client, gate: ModelGate, *, system: str, user: str, schema: dict,
                cache_system: bool) -> str:
    """
    One request, admitted by the gate, recorded on it, and its JSON text back.

    Raises `ModelRefused` when the gate says no, the safety classifier declines,
    or the reply was cut off -- each of those fails the batch, and nothing here
    asks again.
    """
    refusal = gate.admit(len(system) + len(user))
    if refusal:
        raise ModelRefused(refusal)

    system_block = {"type": "text", "text": system}
    if cache_system:
        # Later batches of the same run read the instructions from cache.
        system_block["cache_control"] = {"type": "ephemeral"}

    # A call never outlives the run's deadline: its timeout is what is left of
    # the run, capped at the client's 60 seconds.
    left = gate.seconds_left()
    response = client.messages.create(
        timeout=60.0 if left is None else max(1.0, min(60.0, left)),
        model=gate.model,
        max_tokens=gate.max_output_tokens,
        system=[system_block],
        # Reasoning on, at medium effort (treasury, 2026-11-03). With thinking
        # off at low effort, Haiku read "AIS" and a member's name as dues on $25
        # semi-formal tickets instead of weighing the amount and the memo.
        # Thinking tokens count toward max_tokens, which is why the output
        # ceiling is 16,000 (settings.HARD_MAX_OUTPUT_TOKENS).
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": user}],
    )
    gate.record(getattr(response, "usage", None))

    if response.stop_reason == "refusal":
        raise ModelRefused("The model's safety classifier declined this batch.")
    if response.stop_reason == "max_tokens":
        raise ModelRefused("The reply was cut off at the output limit, so it is not used.")
    return next((block.text for block in response.content if block.type == "text"), "")


# --------------------------------------------------------------------------
# Spend review: does a booked transaction fit its committee?
# --------------------------------------------------------------------------

# Treasurer-owned, like card_roster.json. Read on every run rather than cached,
# so an edit to the file is the next review's prompt without a restart.
SPEND_REVIEW_PROMPT_FILE = (
    Path(__file__).resolve().parent.parent.parent / "config" / "spend_review_prompt.md"
)
VERDICTS = ("fits", "question", "other_committee")
DUES_COMMITTEE_ID = 1
_EDITOR_NOTES_RE = re.compile(r"<!--.*?-->", re.S)

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "amount": {"type": "number"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "suggested_committee_id": {
                        "anyOf": [
                            {"type": "integer", "enum": list(ALLOWED_COMMITTEES)},
                            {"type": "null"},
                        ]
                    },
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "message_to_vp": {"type": "string"},
                },
                "required": [
                    "index", "amount", "verdict", "suggested_committee_id",
                    "confidence", "reason", "message_to_vp",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def prompt_version(text: str | None = None) -> str:
    """A short fingerprint of the review prompt, stored with every verdict."""
    import hashlib

    body = text if text is not None else load_spend_review_prompt()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def fallback_message(record: dict) -> str:
    """
    What the VP is asked when the model flagged a row but wrote no message.

    In the first Haiku trial (2026-10-20) two of seven flags came back with an
    empty message, though the prompt asks for one on every flag.
    """
    try:
        amount = abs(float(record.get("amount")))
    except (TypeError, ValueError):
        amount = 0.0
    details = str(record.get("details") or "")
    when = extract_purchase_date(details)
    if not when:
        posted = pd.to_datetime(record.get("transaction_date"), errors="coerce")
        when = "" if pd.isna(posted) else posted.strftime("%m/%d")
    on = f" on {when}" if when else ""
    if (record.get("amount") or 0) > 0:
        return f"Hi! Could you tell us what the ${amount:,.2f} payment we received{on} was for?"
    return f"Hi! Could you tell us what the ${amount:,.2f} charge{on} was for?"


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
    def fully_failed(self) -> bool:
        return self.batches_attempted > 0 and self.batches_failed == self.batches_attempted

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
    results = _load_results(payload)
    _check_alignment(results, batch)

    positions = {local: original for local, (original, _) in enumerate(batch, start=1)}
    records = {local: record for local, (_, record) in enumerate(batch, start=1)}
    out: dict[int, SpendVerdict] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        original_index = positions.get(item.get("index"))
        verdict = str(item.get("verdict") or "").strip()
        if original_index is None or verdict not in VERDICTS:
            continue
        record = records[item.get("index")]
        suggested = None
        if verdict == "other_committee":
            try:
                suggested = int(item.get("suggested_committee_id"))
            except (TypeError, ValueError):
                suggested = None
            # Suggesting the committee it is already booked to is no suggestion.
            if suggested not in COMMITTEE_BY_ID or suggested == _booked_committee(record):
                suggested = None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        message = str(item.get("message_to_vp") or "").strip()
        # A dues question is the treasurer's own, and the prompt says to leave
        # its message empty -- so only a VP-facing flag gets the fallback.
        if verdict != "fits" and not message and _booked_committee(record) != DUES_COMMITTEE_ID:
            message = fallback_message(record)
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
    gate: ModelGate | None = None,
    on_batch: Callable[[dict[int, SpendVerdict]], None] | None = None,
) -> SpendReviewOutcome:
    """
    Ask whether each booked transaction fits its committee.

    `rows` is (original_index, record), as for `classify_residual`; a record
    carries `budget_category` -- the committee it is booked to. `dues_baseline`
    is `dues.DuesBaseline.render()` for the term under review; without it the
    prompt has the model infer the rate from the batch.

    `on_batch` receives each batch's verdicts as soon as they arrive, so the
    caller can save them before the next call -- a run that fails halfway keeps
    what it paid for, and the next run does not send those rows again.

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
        "Anthropic spend review request",
        gate=gate,
        disabled="no transactions are reviewed.",
    )
    if client is None:
        return outcome

    system_prompt = load_spend_review_prompt()
    baseline = f"\n\n{dues_baseline.strip()}" if dues_baseline else ""

    batches = _batches(reviewable)
    for batch in batches:
        listing = "\n".join(
            _render_review_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        outcome.batches_attempted += 1
        outcome.prompt_chars += len(listing)
        try:
            content = _call_model(
                client,
                gate,
                system=system_prompt,
                user=f"Transactions:\n{listing}{baseline}",
                schema=_REVIEW_SCHEMA,
                cache_system=len(batches) > 1,
            )
            verdicts = _parse_review_response(content, batch)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            outcome.batches_failed += 1
            outcome.errors.append(f"{type(exc).__name__}: {exc}")
            continue
        outcome.verdicts.update(verdicts)
        if on_batch is not None:
            on_batch(verdicts)

    return outcome


def review_listing_chars(rows: list[tuple[int, dict]], dues_baseline: str | None = None) -> int:
    """The largest prompt any one batch of `rows` will send -- for the run's reservation."""
    system = len(load_spend_review_prompt())
    baseline = len(dues_baseline or "") + 2
    largest = 0
    for batch in _batches(rows):
        listing = "\n".join(
            _render_review_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        largest = max(largest, len(listing))
    return system + baseline + largest + len("Transactions:\n")


def residual_listing_chars(residual: list[tuple[int, dict]]) -> int:
    """The largest prompt any one categorizer batch of `residual` will send."""
    largest = 0
    for batch in _batches(residual):
        listing = "\n".join(
            _render_row(local, record) for local, (_, record) in enumerate(batch, start=1)
        )
        largest = max(largest, len(listing))
    return len(_SYSTEM_PROMPT) + largest + len("Transactions:\n")
