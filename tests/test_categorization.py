"""
Categorization rules and pipeline.

These are the highest-value tests in the repository: pure functions, no mocking,
and they encode the business rules that decide where money is booked.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain.categorize.merchants import MerchantMemory, merchant_key
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.scoring import CARD_DEFAULT_CONFIDENCE, CardAssignment, CardRegistry
from ais_fmd.domain.categorize.predicates import (
    DuesSchedule,
    DuesWindow,
    classify_deterministic,
    classify_exact,
    extract_purchase_date,
    looks_like_bar,
    looks_like_food_merchant,
    weekday_from_details,
)


def wells(merchant: str, purchase: str = "09/16", card: str = "1234") -> str:
    return f"PURCHASE AUTHORIZED ON {purchase} {merchant} GAINESVILLE FL S3045 CARD {card}"


def venmo(note: str, sender: str = "Ava Mitchell") -> str:
    return f"3421987654321098 | {note} | {sender} | UF AIS"


# --- Field extraction --------------------------------------------------------

def test_extract_purchase_date():
    assert extract_purchase_date(wells("PUBLIX", "09/16")) == "09/16"
    assert extract_purchase_date("no marker here") == ""
    assert extract_purchase_date(None) == ""


def test_weekday_uses_the_embedded_purchase_date_not_the_posting_date():
    """
    A Tuesday grocery run often posts on Thursday. The rule must read the date
    embedded in the description, which is why the original went looking for it.
    """
    # 2025-09-16 was a Tuesday; posting date deliberately differs.
    details = wells("PUBLIX", "09/16")
    assert weekday_from_details(details, "2025-09-18") == "Tuesday"


def test_weekday_is_unknown_without_an_embedded_date():
    assert weekday_from_details("DEPOSIT", "2025-09-18") == "Unknown"


# --- Merchant classification -------------------------------------------------

@pytest.mark.parametrize("merchant", ["PUBLIX", "CHIPOTLE", "PIESANOS", "HANA SUSHI"])
def test_food_merchants_recognised(merchant):
    assert looks_like_food_merchant(wells(merchant))


@pytest.mark.parametrize("merchant", ["MACDINTONS PUB", "SALTY DOG SALOON", "TOTAL WINE"])
def test_bar_merchants_recognised(merchant):
    assert looks_like_bar(wells(merchant))


def test_grocery_stores_are_never_read_as_bars():
    """'PUBLIX ... BAR HARBOR' must not trip the generic bar keyword."""
    assert not looks_like_bar(wells("PUBLIX SUPER MARKET"))
    assert not looks_like_bar(wells("WALMART SUPERCENTER"))


# --- Rules -------------------------------------------------------------------

def test_outgoing_transfer_is_no_longer_booked_to_refunded():
    """
    Treasury's ruling: a reimbursement is the committee's expenditure.

    The old rule booked every negative Venmo/Zelle to 17 (Refunded), which is
    `kind="ledger"` and therefore outside budget-vs-actual -- so reimbursed
    spending never counted against the budget of the committee that spent it.
    With no committee named, the row is now a question rather than an answer.
    """
    result = classify_deterministic(
        {"amount": -45.0, "details": venmo("Reimbursement"), "account": "Venmo"}
    )
    assert result.committee_id is None
    assert "committee" in result.rule.lower(), "the queue needs the reason in words"


def test_outgoing_transfer_takes_the_committee_its_memo_names():
    """The memo is what makes a reimbursement bookable."""
    result = classify_deterministic(
        {"amount": -120.0, "details": venmo("reimbursing hoodie order"), "account": "Venmo"}
    )
    assert result.committee_id == 13
    assert result.purpose == "Merch"


def test_dues_rule_matches_exact_amounts_only():
    """
    The amount rule on its own, with no memo to help it.

    The memo deliberately says nothing about dues: `rule_dues_memo` would
    otherwise catch the off-rate row and this would stop testing the amount
    matching it exists to test. That the memo rule *does* catch it is asserted
    in `test_treasury_decisions.py`.
    """
    dues = {"amount": 35.00, "details": venmo("payment"), "account": "Venmo"}
    assert classify_deterministic(dues).committee_id == 1

    not_dues = {**dues, "amount": 36.00}
    assert classify_deterministic(not_dues).committee_id != 1


def test_formal_memo_books_formal_in_both_directions():
    """
    A formal memo means Formal whichever way the money moved.

    Previously the negative case was caught by `rule_refund` and booked to
    Refunded, so reimbursing someone who fronted a formal cost dropped out of
    the Formal budget. Both directions are now the same committee.
    """
    formal = {"amount": 55.0, "details": venmo("Spring formal ticket"), "account": "Venmo"}
    assert classify_deterministic(formal).committee_id == 18

    negative = {**formal, "amount": -55.0}
    assert classify_deterministic(negative).committee_id == 18


# --- The card roster --------------------------------------------------------
#
# Treasury ruling, 2026-09-23: the card is the most highly valued evidence. A
# purchase on a confirmed card is that card's committee -- and each card only
# speaks for its own officer cohort, so a new cohort's cards replace the old
# ones instead of both voting. The roster here is fixed in the test so these
# assert behaviour, not this year's card numbers.

COHORT_2024 = dict(era="2024-2026", starts=None, ends=date(2026, 7, 31))
COHORT_2026 = dict(era="2026-2027", starts=date(2026, 8, 1), ends=date(2027, 7, 31))

ROSTER = CardRegistry(
    {
        "8408": CardAssignment(7, "Consulting VP", verified=True, **COHORT_2024),
        "5718": CardAssignment(5, "Membership VP", verified=True, **COHORT_2024),
        "4831": CardAssignment(7, "Consulting VPs", verified=True, **COHORT_2026),
        "0594": CardAssignment(5, "Membership VP", verified=True, **COHORT_2026),
    }
)


def carded(card: str, when: str, merchant: str = "ZOOM.US", purchase: str = "10/24",
           amount: float = -120.0) -> dict:
    return {"amount": amount, "details": wells(merchant, purchase, card=card),
            "account": "Wells Fargo", "transaction_date": when}


def booked(row: dict):
    return categorize_records([row], cards=ROSTER).classifications[0]


def test_a_confirmed_card_decides_the_committee_by_default():
    result = booked(carded("4831", "2026-10-26"))
    assert result.committee_id == 7
    assert result.source == "rule" and result.confidence == CARD_DEFAULT_CONFIDENCE


def test_a_full_meeting_food_reading_overrules_the_card():
    """
    Treasury, 2026-10-01: the card is taken "unless all of the other
    classifications point towards it not being the case" -- a member with their
    card on hand sometimes buys the meeting food. 09/15/2026 is a Tuesday; this
    is a catering-sized food purchase on 0594, every signal saying Meeting Food.
    """
    result = booked(carded("0594", "2026-09-16", "PUBLIX SUPER MAR", "09/15", -159.80))
    assert result.committee_id == 8
    assert result.source == "scored"
    assert "Card 0594" in result.rule, "the reason must name the card it overruled"


def test_a_partial_meeting_food_reading_leaves_the_card_standing():
    """Food on a Tuesday, but not catering-sized: not *all* the evidence, so the card holds."""
    result = booked(carded("0594", "2026-09-16", "PUBLIX SUPER MAR", "09/15", -40.00))
    assert result.committee_id == 5
    assert "lean Meeting Food" in result.rule, "the dissent must still be visible"


def test_consulting_lunch_on_a_meeting_day_stays_consulting():
    """The overlap that got merchant memory removed: same restaurants, different committees."""
    result = booked(carded("4831", "2026-09-16", "CHIPOTLE 1462", "09/15", -45.00))
    assert result.committee_id == 7


def test_consulting_catering_on_a_meeting_day_stays_consulting():
    """
    Consulting caters its own events from the same restaurants, on meeting
    days (treasury, 2026-11-03: "they also cater"). A full meeting-food reading
    on a Consulting card leaves the card standing, with the dissent noted.
    """
    run = categorize_records(
        [carded("4831", "2026-09-16", "CHIPOTLE 1462", "09/15", -210.00)], cards=ROSTER
    )
    result = run.classifications[0]
    assert result.committee_id == 7
    assert "lean Meeting Food" in result.rule and "never overruled" in result.rule
    assert not run.residual, "nothing goes to the model"


def test_the_consulting_exception_follows_the_committee_not_the_card():
    """A new Consulting card next year gets the same treatment with no code change."""
    roster = CardRegistry({"1111": CardAssignment(7, "Next VP", verified=True)})
    run = categorize_records(
        [carded("1111", "2026-09-16", "CHIPOTLE 1462", "09/15", -210.00)], cards=roster
    )
    assert run.classifications[0].committee_id == 7


def test_the_model_only_books_what_it_is_sure_of(monkeypatch):
    from ais_fmd.domain.categorize import pipeline
    from ais_fmd.domain.categorize.llm import LLMOutcome
    from ais_fmd.domain.categorize.predicates import Classification

    seen = []

    def fake(residual, **_):
        seen.extend(residual)
        return LLMOutcome(classifications={
            0: Classification(7, "Professional Development", "Model: consulting event", 0.9, "llm"),
            1: Classification(8, "Meeting Food", "Model: maybe a meeting", 0.55, "llm"),
        })

    monkeypatch.setattr(pipeline, "classify_residual", fake)
    rows = [
        {"amount": -60.00, "details": "ZELLE TO PAT LANE ON 09/16 REF # WFCT22NB0000",
         "account": "Wells Fargo", "transaction_date": "2026-09-16"},
        carded("9999", "2026-09-17", "SOMEWHERE ODD", "09/17", -30.00),
    ]
    run = categorize_records(rows, cards=ROSTER)
    assert run.classifications[0].committee_id == 7
    assert not run.classifications[1].is_assigned
    assert "Model suggests Meeting Food (55%)" in run.classifications[1].rule
    assert "Outgoing transfer" in seen[0][1]["upstream_note"], "the model is told what was recognised"


def test_an_old_cohorts_card_does_not_book_new_spending():
    """8408 was the 2024-2026 Consulting VP's card. It is not carried over."""
    result = booked(carded("8408", "2026-09-20"))
    assert result.committee_id != 7


def test_an_old_cohorts_card_still_categorizes_its_own_statements():
    assert booked(carded("8408", "2025-10-27")).committee_id == 7


def test_a_new_cohorts_card_does_not_reach_back_before_its_cohort():
    assert booked(carded("4831", "2026-03-02")).committee_id != 7


def test_a_card_not_on_the_roster_decides_nothing():
    result = booked(carded("1113", "2026-10-26", "PUBLIX GAINESVILLE", "10/24", -20.58))
    assert result.committee_id is None


def test_no_card_is_hard_coded_in_the_rules():
    """Card numbers live in config/card_roster.json, never in predicates."""
    for card in ("8408", "8313", "5718"):
        result = classify_deterministic(
            {"amount": -120.0, "details": wells("ZOOM.US", "10/24", card=card), "account": "Wells Fargo"}
        )
        assert result.committee_id is None, f"card {card} is still a hard-coded rule"


def test_meeting_food_runs_locally_without_a_model():
    """
    The original delegated this to the language model even though everything
    needed to decide it was local. Running it in Python is what lets the
    pipeline stop sending most rows to a model.
    """
    result = classify_deterministic(
        {
            "amount": -142.30,
            "details": wells("PUBLIX SUPER MAR", "09/16"),  # Tuesday
            "transaction_date": "2025-09-18",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id == 8
    assert result.source == "rule"


def test_meeting_food_does_not_fire_on_a_non_meeting_weekday():
    result = classify_deterministic(
        {
            "amount": -142.30,
            "details": wells("PUBLIX SUPER MAR", "09/19"),  # Friday
            "transaction_date": "2025-09-19",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id != 8


def test_bar_on_a_tuesday_is_membership_not_meeting_food():
    result = classify_deterministic(
        {
            "amount": -210.00,
            "details": wells("MACDINTONS PUB", "09/16"),
            "transaction_date": "2025-09-16",
            "account": "Wells Fargo",
        }
    )
    assert result.committee_id == 5


def test_memo_outranks_the_dues_amount():
    """
    The collision treasury warned about, asserted.

    From Fall 2026 dues are $50/$65 and formal payments land near them. A $50
    incoming transfer memoed "formal" matches the dues rate exactly, so the two
    rules genuinely both fire -- and the memo has to win, or formal ticket
    income is booked as dues.
    """
    schedule = DuesSchedule(
        [
            DuesWindow(
                term_id="FA26",
                semester="Fall 2026",
                start=date(2026, 8, 15),
                end=date(2026, 12, 18),
                rates=(Decimal("50.00"), Decimal("65.00")),
                verified=True,
            )
        ]
    )
    row = {
        "amount": 50.00,
        "details": venmo("formal ticket"),
        "account": "Venmo",
        "transaction_date": "2026-09-10",
    }
    assert classify_exact(row, dues=schedule).committee_id == 18

    # Same amount, same term, no memo: that one really is dues.
    plain = {**row, "details": venmo("")}
    assert classify_exact(plain, dues=schedule).committee_id == 1


# --- Merchant memory (M4) ----------------------------------------------------

def test_merchant_key_collapses_transaction_noise():
    first = merchant_key(wells("PUBLIX SUPER MAR", "09/16"))
    second = merchant_key(wells("PUBLIX SUPER MAR", "11/02"))
    assert first == second != ""


def test_merchant_key_strips_store_numbers():
    assert merchant_key(wells("CHIPOTLE 1462")) == merchant_key(wells("CHIPOTLE 1893"))


def test_merchant_key_skips_venmo_transfers():
    """Venmo details are person-specific -- learning them would be useless."""
    assert merchant_key(venmo("Fall dues", "Ava Mitchell")) == ""
    assert merchant_key(venmo("Fall dues", "Noah Patel")) == ""


def test_merchant_memory_round_trip():
    memory = MerchantMemory()
    details = wells("SWEETWATER BRANCH INN")
    assert memory.lookup(details) is None

    memory.remember(details, committee_id=14, purpose="Road Trip")
    found = memory.lookup(details)
    assert found is not None
    assert found.committee_id == 14
    assert found.source == "merchant"


def test_merchant_memory_generalises_to_new_transactions_of_the_same_merchant():
    memory = MerchantMemory()
    memory.remember(wells("SWEETWATER BRANCH INN", "01/04"), 14, "Road Trip")
    found = memory.lookup(wells("SWEETWATER BRANCH INN", "07/22"))
    assert found is not None and found.committee_id == 14


# --- Pipeline ordering -------------------------------------------------------

def test_pipeline_resolves_locally_and_sends_only_the_residual():
    records = [
        {"amount": 35.00, "details": venmo("dues"), "account": "Venmo"},
        {"amount": -120.0, "details": wells("ZOOM.US", card="8408"), "account": "Wells Fargo"},
        {"amount": -60.0, "details": wells("TOTALLY UNKNOWN VENDOR"), "account": "Wells Fargo"},
    ]
    run = categorize_records(records)
    assert run.rows_resolved_locally == 2
    assert run.rows_sent_to_model == 1
    # No model call happens in sandbox, so the residual stays unassigned.
    assert run.counts_by_source["none"] == 1


def test_pipeline_on_empty_input():
    run = categorize_records([])
    assert run.total == 0
    assert run.coverage == 0.0
