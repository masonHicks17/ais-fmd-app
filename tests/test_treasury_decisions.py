"""
The treasury rulings, asserted one by one.

Every test here exists because a human answered a question in
`docs/treasury-questions.md`, and each one names the answer it encodes. That
matters more than usual: these are not properties derived from the data, they
are *decisions*, and a decision can be reversed. When one is, the test that
fails should be the one that tells you which ruling you just contradicted --
not some incidental assertion three modules away.

Answered 2026-08-24. Still open, and deliberately untested here: the disputed
purpose mappings (5a), and whether a "headshot" memo means Professional
Development or Membership.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ais_fmd.domain import dues as dues_module
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.predicates import (
    DuesSchedule,
    DuesWindow,
    classify_deterministic,
    classify_exact,
    committee_from_memo,
)


def venmo(note: str, sender: str = "Ava Mitchell") -> str:
    return f"3421987654321098 | {note} | {sender} | UF AIS"


def zelle_from(name: str = "Jane Doe") -> str:
    return f"ZELLE FROM {name.upper()} ON 07/08 REF # A1"


def zelle_to(name: str = "John Smith") -> str:
    return f"ZELLE TO {name.upper()} ON 07/09 REF # A2"


def row(details: str, amount: float, when: str = "2026-09-10") -> dict:
    return {
        "amount": amount,
        "details": details,
        "account": "Venmo",
        "transaction_date": when,
    }


FALL_2026 = DuesWindow(
    term_id="FA26",
    semester="Fall 2026",
    start=date(2026, 8, 15),
    end=date(2026, 12, 18),
    rates=(Decimal("35.00"), Decimal("50.00"), Decimal("65.00")),
    verified=True,
)


# --- Q1: per-term dues rates -------------------------------------------------
#
# "Last term was 35 early and 52.5 late. This term it's 50 and 65."

def test_fall_2026_rates_are_recorded_and_confirmed():
    """$35 is the exec rate -- official "for at least this semester" (2026-10-01)."""
    assert dues_module.CONFIRMED_DUES_RATES["Fall 2026"] == (
        Decimal("35.00"),
        Decimal("50.00"),
        Decimal("65.00"),
    )


def test_the_new_rates_are_recognised_as_dues():
    """
    The failure this fixes was silent.

    Dues match on an exact amount. Fall 2026 began on 2026-08-15 charging $50
    and $65 while the app still assumed $35/$52.50, so every dues payment of the
    new term fell through to the review queue and dues income read as collapsed.
    """
    schedule = DuesSchedule([FALL_2026])
    for amount in (35.00, 50.00, 65.00):
        result = classify_exact(row(zelle_from(), amount), dues=schedule)
        assert result.committee_id == 1, f"${amount} should be dues in Fall 2026"


def test_the_old_rates_stop_being_dues_once_the_term_changed():
    """A rate that is no longer charged must not keep matching. $52.50 was Spring 2026's."""
    schedule = DuesSchedule([FALL_2026])
    assert classify_exact(row(zelle_from(), 52.50), dues=schedule).committee_id != 1


# --- Dues: 99% on the amount, unless the memo argues (2026-10-01) ---------------

@pytest.mark.parametrize("memo", ["", " JACOB TASTENSEN", " AIS DUES", " ENTRY PAYMENT", " ENROLLMENT FOR JACOB"])
def test_a_dues_amount_is_dues_at_99_percent(memo):
    result = classify_exact(row(zelle_from() + memo, 50.00), dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1
    assert result.confidence == 0.99


@pytest.mark.parametrize("memo", ["FORMAL TICKET", "PAY BACK FOR DINNER", "REIMBURSEMENT", "GALA TICKET"])
def test_a_dues_amount_whose_memo_says_otherwise_is_not_booked_as_dues(memo):
    result = classify_exact(row(f"{zelle_from()} {memo}", 50.00), dues=DuesSchedule([FALL_2026]))
    assert result.committee_id != 1


def test_a_conflicting_memo_reaches_the_model_with_the_reason(monkeypatch):
    from ais_fmd.domain.categorize import pipeline
    from ais_fmd.domain.categorize.llm import LLMOutcome

    seen = []
    monkeypatch.setattr(pipeline, "classify_residual", lambda r, **_: seen.extend(r) or LLMOutcome())
    run = categorize_records([row(zelle_from() + " PAY BACK FOR DINNER", 50.00)],
                             dues=DuesSchedule([FALL_2026]))
    assert not run.classifications[0].is_assigned
    assert "pay back" in seen[0][1]["upstream_note"]


def test_the_payers_name_is_never_read_as_a_conflict():
    """'Event' or 'Gift' could be a surname; only the memo is checked."""
    result = classify_exact(row("ZELLE FROM ROSE GIFT ON 08/19 REF # A1 AIS DUES", 50.00),
                            dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1


# --- Dues windows reach back to the previous term's end (2026-10-01) -------------

SPRING_2026 = DuesWindow("SP26", "Spring 2026", date(2026, 1, 5), date(2026, 5, 1),
                         (Decimal("35.00"), Decimal("52.50")), verified=True)


def test_dues_paid_before_the_term_starts_count_for_it():
    """Real Fall 2026 dues began arriving 08/12; the term starts 08/15."""
    schedule = DuesSchedule([SPRING_2026, FALL_2026])
    result = classify_exact(row(zelle_from(), 50.00, when="2026-08-12"), dues=schedule)
    assert result.committee_id == 1
    assert "Fall 2026" in result.rule
    assert schedule.window_for(date(2026, 5, 2)).semester == "Fall 2026"
    assert schedule.window_for(date(2026, 5, 1)).semester == "Spring 2026"


def test_the_dues_pages_count_early_payments_toward_the_coming_term():
    terms = pd.DataFrame([
        {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05",
         "end_date": "2026-05-01", "dues_rates": "35.00,52.50", "dues_rates_verified": 1},
        {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15",
         "end_date": "2026-12-18", "dues_rates": "35.00,50.00,65.00", "dues_rates_verified": 1},
    ])
    txns = pd.DataFrame([
        {"TransactionID": 1, "transaction_date": "2026-08-12", "amount": 50.00,
         "details": "ZELLE FROM JANE DOE ON 08/12 REF # A1 AIS DUES", "account": "Wells Fargo",
         "budget_category": 1},
    ])
    assert dues_module.summarize(txns, terms, "Fall 2026").payment_count == 1


def test_dues_skip_summer_but_keep_terms_without_confirmed_rates():
    """July dues count toward Fall; a term nobody confirmed rates for still has dues."""
    from ais_fmd.domain.terms import attach_semester, dues_terms

    terms = pd.DataFrame([
        {"Semester": "Fall 2025", "start_date": "2025-08-20", "end_date": "2026-01-04", "dues_rates": None},
        {"Semester": "Spring 2026", "start_date": "2026-01-05", "end_date": "2026-05-01", "dues_rates": "35.00,52.50"},
        {"Semester": "Summer 2026", "start_date": "2026-05-02", "end_date": "2026-08-14", "dues_rates": None},
        {"Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18", "dues_rates": "35.00,50.00,65.00"},
    ])
    txns = pd.DataFrame({"transaction_date": ["2025-09-03", "2026-07-08"]})
    tagged = attach_semester(txns, dues_terms(terms))
    assert list(tagged["Semester"]) == ["Fall 2025", "Fall 2026"]


def test_repeat_dues_payers_are_surfaced():
    from ais_fmd.domain.quality import check_repeat_dues_payers

    terms = pd.DataFrame([
        {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18"},
    ])
    txns = pd.DataFrame([
        {"transaction_date": "2026-08-20", "amount": 35.00, "budget_category": 1,
         "details": "ZELLE FROM JESSE DOE ON 08/20 REF # A1 AIS"},
        {"transaction_date": "2026-08-30", "amount": 15.00, "budget_category": 1,
         "details": "ZELLE FROM JESSE DOE ON 08/30 REF # A2 AIS DUES"},
        {"transaction_date": "2026-08-30", "amount": 50.00, "budget_category": 1,
         "details": "ZELLE FROM ONE TIMER ON 08/30 REF # A3 DUES"},
    ])
    issue = check_repeat_dues_payers(txns, pd.DataFrame(), terms)
    assert issue is not None and issue.count == 1
    assert issue.rows.iloc[0]["Total"] == 50.00


# --- Internal transfers ---------------------------------------------------------
#
# "Those Venmo transfers are not income -- that was existing finances we had
# access to. We just moved it from different accounts." (2026-10-01)

@pytest.mark.parametrize("memo", ["VENMO REIMBURSEMENT", "VENMO TRANSFER"])
def test_venmo_cashed_out_by_zelle_is_a_transfer(memo):
    details = f"ZELLE FROM SAMPLE PERSON ON 07/01 REF # JFR0TQ6QPUIM {memo}"
    result = classify_exact(row(details, 500.00, when="2026-07-01"))
    assert result.committee_id == 3


@pytest.mark.parametrize("details, amount", [
    ("VENMO            CASHOUT    260421 1049771990944 JORDAN REVIS", 11628.80),
    ("VENMO ACCTVERIFY 260418 1049698300353 GRADY SVETLY", -0.35),
])
def test_venmos_own_ach_lines_are_transfers(details, amount):
    assert classify_exact(row(details, amount, when="2026-04-22")).committee_id == 3


def test_a_monthly_service_fee_is_treasury():
    assert classify_exact(row("MONTHLY SERVICE FEE", -15.00)).committee_id == 2


def test_a_member_zelle_is_not_a_transfer():
    assert classify_exact(row(zelle_from() + " AIS DUES", 50.00)).committee_id != 3


def test_transfers_are_neither_income_nor_expense():
    from ais_fmd.domain.budgets import _totals_for

    frame = pd.DataFrame([
        {"amount": 500.00, "budget_category": 3},
        {"amount": 65.00, "budget_category": 1},
        {"amount": -40.00, "budget_category": 8},
    ])
    totals = _totals_for(frame)
    assert totals["income"] == 65.00
    assert totals["expenses"] == 40.00


def test_uncategorized_rows_still_count_toward_totals():
    """The nullable-integer trap: NA != 3 is NA, and a mask drops NA rows."""
    from ais_fmd.domain.budgets import _totals_for

    frame = pd.DataFrame({"amount": [15.00, 65.00, 500.00],
                          "budget_category": pd.array([None, 1, 3], dtype="Int64")})
    assert _totals_for(frame)["income"] == 80.00


def test_earlier_terms_are_left_unconfirmed_on_purpose():
    """
    Treasury answered for two terms, not for nine.

    Spring 2025 carried both rate pairs in the real data and nobody has said
    which counted. Recording a guess here would move about $1,870 of income, so
    the absence is the point -- `quality.check_unverified_dues_rates` keeps
    reporting the rest until someone confirms them.
    """
    assert set(dues_module.CONFIRMED_DUES_RATES) == {"Spring 2026", "Fall 2026"}
    assert "Spring 2025" not in dues_module.CONFIRMED_DUES_RATES


# --- Q1 fallout: the amount collision treasury warned about ------------------
#
# "There are also going to be some formal dues that might be around those
# amounts but the actual dues are a very specific amount."

def test_a_formal_memo_beats_a_matching_dues_amount():
    """$50 is both the Fall 2026 dues rate and a plausible formal ticket."""
    contested = row(venmo("spring formal ticket"), 50.00)
    assert classify_exact(contested, dues=DuesSchedule([FALL_2026])).committee_id == 18


def test_the_same_amount_without_a_memo_is_still_dues():
    """The memo breaks the tie; it does not disable the amount rule."""
    plain = row(zelle_from(), 50.00)
    assert classify_exact(plain, dues=DuesSchedule([FALL_2026])).committee_id == 1


def test_a_dues_memo_books_an_off_schedule_amount():
    """
    Treasury asked for a dues memo keyword "in addition" to the amount rule.

    $36 is not a Fall 2026 rate, so the schedule rejects it. The payer calling
    it dues is enough.
    """
    odd = row(zelle_from() + " fall dues", 36.00)
    result = classify_exact(odd, dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1
    assert "36.00" in result.rule, "the odd amount belongs in the reason"


def test_the_schedule_still_gets_first_refusal():
    """
    "In addition" means after, not instead.

    A row the schedule recognises must keep the schedule's reasoning, naming the
    term it matched. If the memo rule ran first it would shadow that on every
    dues row that happens to carry the word.
    """
    on_rate = row(zelle_from() + " dues", 50.00)
    result = classify_exact(on_rate, dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1
    assert "Fall 2026" in result.rule, "the schedule should have claimed this row"


def test_a_dues_memo_does_not_outrank_a_formal_memo():
    """The collision treasury warned about survives the addition."""
    both = row(venmo("formal ticket - dues"), 50.00)
    assert classify_exact(both, dues=DuesSchedule([FALL_2026])).committee_id == 18


def test_an_outgoing_dues_memo_is_not_income():
    """A negative transfer memoed "dues" is a refund, which is a human's call."""
    refund = row(zelle_to() + " dues refund", -50.00)
    assert classify_exact(refund, dues=DuesSchedule([FALL_2026])).committee_id != 1


FA26_TERMS = pd.DataFrame(
    [
        {
            "TermID": "FA26",
            "Semester": "Fall 2026",
            "start_date": "2026-08-01",
            "end_date": "2026-12-18",
            "dues_rates": "50.00,65.00",
            "dues_rates_verified": 1,
        }
    ]
)


def _off_rate_rows(memo: str, amount: float = 45.00, n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "TransactionID": i,
                "transaction_date": "2026-09-12",
                "details": f"ZELLE FROM MEMBER{i} ON 09/12 REF # A{i} {memo}".strip(),
                "amount": amount,
                "account": "Wells Fargo",
            }
            for i in range(n)
        ]
    )


def test_off_rate_dues_are_still_reported_but_not_alarming():
    """
    Booking these must not silence the check, and must not shout either.

    Treasury: "Execs pay the discounted rate, some people pay less for other
    reasons." Off-rate dues are routine, so a HIGH alarm on every discount would
    train a treasurer to ignore this check and take the real signal down with
    it. They are already counted as income, so the finding is informational and
    points at the fix: add the rate to the term.
    """
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    issue = check_possible_dues_rate_change(
        _off_rate_rows("fall dues"), pd.DataFrame(), FA26_TERMS
    )
    assert issue is not None
    assert issue.severity == "low"
    assert "already counted as dues income" in issue.detail


def test_an_unexplained_cluster_is_still_high():
    """
    The signal the check exists for, preserved.

    Several people sending one unrecognised amount with no memo saying dues is
    what an unrecorded rate change looks like from inside the data.
    """
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    issue = check_possible_dues_rate_change(
        _off_rate_rows(""), pd.DataFrame(), FA26_TERMS
    )
    assert issue is not None
    assert issue.severity == "high"


def test_one_unexplained_row_keeps_the_cluster_high():
    """A mixed cluster is not explained; the quiet reading must not win."""
    from ais_fmd.domain.quality import check_possible_dues_rate_change

    rows = pd.concat(
        [_off_rate_rows("fall dues", n=3), _off_rate_rows("", n=1)], ignore_index=True
    )
    rows["TransactionID"] = range(len(rows))
    issue = check_possible_dues_rate_change(rows, pd.DataFrame(), FA26_TERMS)
    assert issue is not None
    assert issue.severity == "high"


# --- Q3: reimbursements belong to the committee they repaid ------------------
#
# "Reimbursements should go to the committee they represent. If someone spends
# money on a personal card and gets reimbursed then it was a committee
# expenditure."

def test_an_outgoing_transfer_is_no_longer_booked_to_refunded():
    result = classify_deterministic(row(zelle_to(), -45.00))
    assert result.committee_id is None, "17/Refunded is no longer the default"


def test_an_unresolved_reimbursement_explains_itself_to_the_queue():
    """
    A row with no answer still has to carry a question.

    The review queue renders `match_rule`. Left as UNMATCHED these rows would
    show an empty cell, which is indistinguishable from a parse failure -- and
    working this queue is the single highest-value thing anyone can do with the
    app right now.
    """
    run = categorize_records([row(zelle_to(), -45.00)])
    rule = run.classifications[0].rule
    assert rule, "an unresolved reimbursement must not be a blank cell"
    assert "committee" in rule.lower()


@pytest.mark.parametrize(
    "memo, committee",
    [
        ("reimbursing the hoodie order", 13),
        ("road trip gas", 14),
        ("formal deposit", 18),
    ],
)
def test_a_reimbursement_takes_the_committee_its_memo_names(memo, committee):
    assert classify_deterministic(row(venmo(memo), -120.00)).committee_id == committee


def test_reimbursed_spend_now_lands_in_a_budgeted_committee():
    """
    The accounting consequence, stated as a test.

    17 (Refunded) is `kind="ledger"` and sits outside budget-vs-actual, so under
    the old rule every reimbursed dollar was invisible to the budget of the
    committee that spent it. Committees 13/14/18 are all `kind="committee"`.
    """
    from ais_fmd.config.categories import COMMITTEE_BY_ID

    result = classify_deterministic(row(venmo("crewneck order"), -300.00))
    assert COMMITTEE_BY_ID[result.committee_id].kind == "committee"


def test_a_merchant_return_is_still_a_refund():
    """
    Money coming back is not a reimbursement.

    17 keeps its meaning for the incoming direction, which is why the rule was
    renamed rather than deleted.
    """
    from ais_fmd.domain.categorize import bulk

    rows = [
        {
            "details": "PURCHASE RETURN AUTHORIZED ON 04/12 AMAZON.COM",
            "amount": 37.39,
            "transaction_date": "2026-04-12",
            "account": "Wells Fargo",
        }
    ]
    assert bulk.propose(bulk.group_residual(rows)[0]).committee_id == 17


# --- Q4: Venmo, deprecated going forward, counted net historically -----------
#
# "Venmo will now be depricated and no dues or money will come from it. For
# venmo it should still be counted historically taking into account those fees
# (if accurate)."

def test_historical_venmo_dues_are_counted_net_of_fees():
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00,30.00,40.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    assert schedule.accept_venmo_net, "treasury enabled this"
    for net in ("24.43", "29.34", "39.14"):
        assert schedule.matches(Decimal(net), date(2026, 3, 1), is_venmo=True)


def test_zelle_is_untouched_by_the_venmo_window():
    """Only Venmo took a cut, and Zelle is what remains in use."""
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    assert not schedule.matches(Decimal("24.43"), date(2026, 3, 1), is_venmo=False)


def test_the_fee_window_still_refuses_amounts_it_cannot_justify():
    """
    "If accurate" is honoured, not assumed away.

    The observed net amounts do not fit one fee formula to the cent, so this is
    a bounded window below gross rather than a reconstructed schedule. It must
    not swallow an unrelated amount that merely happens to sit nearby.
    """
    terms = pd.DataFrame(
        [
            {
                "TermID": "X",
                "Semester": "Test",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "dues_rates": "25.00,40.00",
                "dues_rates_verified": 1,
            }
        ]
    )
    schedule = dues_module.schedule_from_terms(terms)
    when = date(2026, 3, 1)
    assert not schedule.matches(Decimal("20.00"), when, is_venmo=True)
    assert not schedule.matches(Decimal("33.00"), when, is_venmo=True)
    # A fee only ever reduces what arrives.
    assert not schedule.matches(Decimal("41.00"), when, is_venmo=True)


# --- Q5b: memos are enough to book a transfer --------------------------------
#
# "Absolutely look at the memos they will clarify it well."

@pytest.mark.parametrize(
    "memo, committee",
    [
        ("crewneck", 13),
        ("hoodie", 13),
        ("t-shirt order", 13),
        ("merch", 13),
        ("road trip", 14),
        ("roadtrip st augustine", 14),
        ("st. augustine", 14),
        ("semi formal", 18),
        ("formal", 18),
    ],
)
def test_incoming_payments_are_booked_from_their_memo(memo, committee):
    assert classify_deterministic(row(venmo(memo), 45.00)).committee_id == committee


def test_a_payer_name_still_teaches_nothing():
    """
    The distinction that makes memo rules safe.

    A rule keyed on a member's name would mis-categorise everything that member
    ever pays. A memo travels with one payment, so it carries no such risk --
    which is why memos are used and names still are not.
    """
    from ais_fmd.domain.categorize.merchants import merchant_key

    assert not merchant_key(zelle_from("Jane Doe"))


@pytest.mark.parametrize("memo", ["headshot", "headshots for linkedin"])
def test_headshots_are_left_for_a_human(memo):
    """
    Professional Development or Membership -- treasury did not say.

    Both are defensible and a keyword cannot choose. Six rows, $35 total; the
    cost of leaving them in the queue is far below the cost of guessing.
    """
    assert committee_from_memo(memo) is None


def test_bare_ticket_names_no_committee():
    """"Ticket" appears on formal, road trip and event payments alike."""
    assert committee_from_memo("ticket payment") is None


# --- Q2: the card roster turns over with the officers ------------------------
#
# "The cards might change around with new VPs, I will input their numbers later
# go with your best guess now based on everything I have sent historically."

def test_no_card_was_guessed_from_the_apps_own_output():
    """
    The best guess available is "no guess", and that is the honest answer.

    The only current-era evidence about the seven undocumented cards is this
    app's own `budget_category`, written by `categorize_frame` at import time.
    Deriving a roster from it would train the categorizer on its own output and
    harden its mistakes into evidence -- the one thing `scoring.py` says never
    to do. The 2022-23 human labels are a different cohort with different cards.
    """
    from ais_fmd.domain.categorize.scoring import INFERRED_CARDS

    assert INFERRED_CARDS == {}


# --- 2026-09-23: the card decides, and the Fall 2026 cards ------------------
#
# "Apply the cards ruling -- make sure that it is the most highly valued one
# out of all of the categorizations ... we should not hard code the consulting
# one, that was more for the previous year ... the new cards are overriding the
# old ones." Also: "the US Mobile payment card was mine, so mark that down as
# treasury", and "get rid of ... the merchant rule. Things are going to be too
# varying ... consulting food and meeting food, there's going to be a lot of
# overlap."

FALL_2026_CARDS = {
    "4831": 7,  # Consulting -- E-Board Meeting 05 notes, 09/21/26
    "0594": 5,  # Membership
    "7101": 5,  # Membership
    "3526": 4,  # President
    "3466": 2,  # Treasury -- the treasurer's own card (US Mobile), 2026-09-23
    "9570": 8,  # VP Operations' card: Meeting Food by default, 2026-11-03
}


def test_the_fall_2026_cards_are_on_the_roster():
    from ais_fmd.domain.categorize.scoring import current_era, load_card_roster

    roster = load_card_roster()
    era = current_era()
    for card, committee in FALL_2026_CARDS.items():
        assert card in roster, f"card {card} is missing from card_roster.json"
        assignment = roster[card]
        assert assignment.committee_id == committee, card
        assert assignment.verified, f"card {card} must be confirmed"
        assert assignment.era == era, f"card {card} must belong to the current cohort"


def test_the_consulting_card_is_not_carried_over_from_last_year():
    """8408 books 2024-2026 statements only; from 2026-08-01 it decides nothing."""
    this_year = row("PURCHASE AUTHORIZED ON 09/18 ZOOM.US CARD 8408", -120.00)
    this_year["transaction_date"] = "2026-09-20"
    last_year = dict(this_year, transaction_date="2025-09-20")
    run = categorize_records([this_year, last_year])
    assert run.classifications[0].committee_id != 7
    assert run.classifications[1].committee_id == 7


def test_a_membership_card_at_a_tuesday_food_run_is_membership():
    """The reversal of the 2026-08-24 ruling that timing beats the card."""
    record = row("PURCHASE AUTHORIZED ON 09/01 SAMS CLUB #8155 GAINESVILLE FL CARD 0594", -133.07)
    record["transaction_date"] = "2026-09-01"
    run = categorize_records([record])
    assert run.classifications[0].committee_id == 5


@pytest.mark.parametrize("memo, committee", [
    ("MEETING FOOD REIMBURSEMENT", 8),
    ("MEMBERSHIP REIMBURSEMENT", 5),
])
def test_an_outgoing_memo_naming_its_committee_is_booked(memo, committee):
    assert classify_exact(row(f"{zelle_to()} {memo}", -105.44)).committee_id == committee


@pytest.mark.parametrize("memo", [
    "CONSULTING FOOD",
    "CONSULTING MEETING FOOD REIMBURSEMENT",
    "AIS CONSULTING TRAVEL REIMBURSEMENT",
    "CONSULTING GBM FOOD",
])
def test_an_outgoing_memo_naming_consulting_is_consulting(memo):
    """Treasury, 2026-10-12: consulting in the memo wins over every other word."""
    assert classify_exact(row(f"{zelle_to()} {memo}", -403.69)).committee_id == 7


def test_an_incoming_hoodie_payment_mentioning_consulting_stays_merch():
    details = zelle_from() + " PAYMENT FOR AIS HOODIE WITH CONSULTING COMMITTEE"
    assert classify_exact(row(details, 15.00)).committee_id == 13


def test_membership_on_an_incoming_payment_is_still_dues():
    """The trap the outgoing-only table exists to avoid."""
    result = classify_exact(row(zelle_from() + " MEMBERSHIP DUES", 50.00), dues=DuesSchedule([FALL_2026]))
    assert result.committee_id == 1


def _ops(merchant: str, purchase: str, amount: float, when: str) -> dict:
    record = row(f"PURCHASE AUTHORIZED ON {purchase} {merchant} GAINESVILLE FL CARD 9570", amount)
    record["transaction_date"] = when
    return record


def test_the_operations_card_defaults_to_meeting_food():
    """Treasury, 2026-11-03: the real $443.68 Moe's order, bought on a Monday."""
    record = _ops("MOE'S 167", "10/26", -443.68, "2026-10-30")
    assert categorize_records([record]).classifications[0].committee_id == 8


def test_a_meeting_sized_order_on_the_operations_card_is_meeting_food():
    """The real $641.24 Toast order on Tuesday 09/22."""
    record = _ops("TST*MI APA LATIN C 352-376-7020 FL", "09/22", -641.24, "2026-09-24")
    assert categorize_records([record]).classifications[0].committee_id == 8


def test_small_food_on_the_operations_card_is_meeting_food():
    """A $40 grocery run on any day is booked, not held for review."""
    record = _ops("PUBLIX SUPER MAR", "09/18", -40.00, "2026-09-19")
    assert categorize_records([record]).classifications[0].committee_id == 8


def test_a_social_on_the_operations_card_is_operations():
    """'with sole exceptions of socials we give them' -- a bar is a social."""
    record = _ops("SALTY DOG SALOON 352-3756969 FL", "10/09", -84.00, "2026-10-12")
    result = categorize_records([record]).classifications[0]
    assert result.committee_id == 6
    assert "a social" in result.rule


def test_monday_is_a_meeting_food_day():
    """Treasury, 2026-11-03: 'meeting food days should include monday'."""
    record = row("PURCHASE AUTHORIZED ON 10/26 PIESANOS 3 GAINESVILLE FL", -300.00)
    record["transaction_date"] = "2026-10-27"
    assert categorize_records([record]).classifications[0].committee_id == 8


@pytest.mark.parametrize("memo, amount", [
    ("AIS XL CREW NECK", 30.00),
    ("AIS FOREVER SWEATERS", 60.00),
    ("AIS SHIRT", 15.00),
])
def test_merch_memos_from_the_fall_2026_statement(memo, amount):
    assert classify_exact(row(f"{zelle_from()} {memo}", amount)).committee_id == 13


def test_other_cards_keep_their_default_on_small_meeting_day_food():
    record = row("PURCHASE AUTHORIZED ON 09/15 PUBLIX SUPER MAR GAINESVILLE FL CARD 0594", -40.00)
    record["transaction_date"] = "2026-09-16"
    assert categorize_records([record]).classifications[0].committee_id == 5


def test_the_treasurers_card_is_treasury():
    record = row("PURCHASE AUTHORIZED ON 08/09 US MOBILE WWW.USMOBILE. NY CARD 3466", -96.00)
    record["transaction_date"] = "2026-08-10"
    assert categorize_records([record]).classifications[0].committee_id == 2


# --- The roster check reads the roster file ---------------------------------

def _roster_file(tmp_path, cards: dict, *, era="2026-2027", start="2026-08-01", end="2027-07-31"):
    path = tmp_path / "card_roster.json"
    body = {"_current_era": era, "_eras": {era: {"start": start, "end": end}}}
    body.update({card: {"committee": c, "holder": "VP", "verified": True, "era": era}
                 for card, c in cards.items()})
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _purchase(card: str, when: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "TransactionID": 1, "transaction_date": when, "amount": -84.10, "budget_category": None,
        "details": f"PURCHASE AUTHORIZED ON 09/19 PUBLIX GAINESVILLE FL CARD {card}",
    }])


def test_spend_on_known_cards_is_not_flagged(tmp_path):
    from ais_fmd.domain.quality import check_card_roster_era

    path = _roster_file(tmp_path, {"0594": 5})
    issue = check_card_roster_era(_purchase("0594", "2026-09-20"), pd.DataFrame(), pd.DataFrame(),
                                  roster_path=path)
    assert issue is None


def test_an_unidentified_card_in_current_spending_is_named(tmp_path):
    """What the real Fall 2026 statement looked like before the cards were collected."""
    from ais_fmd.domain.quality import check_card_roster_era

    path = _roster_file(tmp_path, {"0594": 5})
    issue = check_card_roster_era(_purchase("3466", "2026-09-20"), pd.DataFrame(), pd.DataFrame(),
                                  roster_path=path)
    assert issue is not None
    assert issue.count == 1
    assert "3466" in issue.detail
    assert "card_roster.json" in issue.detail, "point at the file, not at Python"


def test_a_previous_cohorts_card_counts_as_unidentified_now(tmp_path):
    """A card from the last cohort is not evidence for this one, so it is flagged."""
    from ais_fmd.domain.quality import check_card_roster_era

    path = _roster_file(tmp_path, {"0594": 5})
    issue = check_card_roster_era(_purchase("8408", "2026-09-20"), pd.DataFrame(), pd.DataFrame(),
                                  roster_path=path)
    assert issue is not None and "8408" in issue.detail


def test_spend_after_the_cohort_ended_is_flagged(tmp_path):
    """
    Cards stop applying when their cohort ends, so a roster nobody renewed
    leaves new spending with no card evidence -- and says so.
    """
    from ais_fmd.domain.quality import check_card_roster_era

    path = _roster_file(tmp_path, {"0594": 5})
    issue = check_card_roster_era(_purchase("0594", "2027-09-20"), pd.DataFrame(), pd.DataFrame(),
                                  roster_path=path)
    assert issue is not None
    assert "ended" in issue.title.lower()
    assert "_current_era" in issue.detail


def test_an_unreadable_roster_is_high_severity(tmp_path):
    from ais_fmd.domain.quality import check_card_roster_era

    path = tmp_path / "card_roster.json"
    path.write_text("{ not json", encoding="utf-8")
    issue = check_card_roster_era(_purchase("0594", "2026-09-20"), pd.DataFrame(), pd.DataFrame(),
                                  roster_path=path)
    assert issue is not None
    assert issue.severity == "high"


def test_transfers_without_a_card_are_not_a_roster_problem():
    """Zelle rows carry no card, so they say nothing either way."""
    from ais_fmd.domain.quality import check_card_roster_era

    transactions = pd.DataFrame(
        [
            {
                "TransactionID": 1,
                "transaction_date": "2026-08-19",
                "details": "ZELLE FROM JANE DOE ON 08/19 REF # A1 AIS DUES",
                "amount": 50.00,
                "budget_category": 1,
            }
        ]
    )
    assert check_card_roster_era(transactions, pd.DataFrame(), pd.DataFrame()) is None


# --- 2026-11-03: semi-formal payments are not dues ---------------------------

@pytest.mark.parametrize("memo", ["AIS SEMI ANDREW MUNZ", "SEMI", "YAY SEMI"])
def test_a_semi_memo_is_a_formal_ticket(memo):
    """'the tag lines literally say AIS semi' -- the real Fall 2026 memos."""
    assert classify_exact(row(f"{zelle_from()} {memo}", 25.00)).committee_id == 18


def test_semi_inside_a_name_is_not_a_ticket():
    details = "ZELLE FROM SEMIRA JONES ON 10/26 REF # BACK4MXT4N6F"
    assert classify_exact(row(details, 25.00)).committee_id != 18


def test_the_model_cannot_book_dues_on_a_non_dues_amount(monkeypatch):
    from ais_fmd.domain.categorize import pipeline
    from ais_fmd.domain.categorize.llm import LLMOutcome, _render_row
    from ais_fmd.domain.categorize.predicates import Classification

    seen = []

    def fake(residual, **_):
        seen.extend(residual)
        return LLMOutcome(classifications={
            index: Classification(1, "Dues", "Model: member paying", 0.95, "llm")
            for index, _ in residual
        })

    monkeypatch.setattr(pipeline, "classify_residual", fake)
    ticket = row(zelle_from() + "", 25.00)
    ticket["transaction_date"] = "2026-10-26"
    run = categorize_records([ticket], dues=DuesSchedule([FALL_2026]))
    assert not run.classifications[0].is_assigned, "a $25 payment is never booked as dues"
    assert "not-a-dues-amount" in _render_row(0, seen[0][1]), "the model is told"


def test_the_paypal_balance_moving_to_checking_is_a_transfer():
    """Treasury, 2026-11-03: 'the 963 PayPal transfer is a transfer, non-income'."""
    details = "PAYPAL           TRANSFER   261020 1053880175841 ASSOCIATION FOR INFORM"
    assert classify_exact(row(details, 963.93)).committee_id == 3


# --- 2026-11-06: the model sees the rest of the upload ----------------------

def test_the_model_is_told_what_the_rest_of_the_upload_was(monkeypatch):
    """A no-memo $25 among semi-formal tickets: the context says $25 was a ticket price."""
    from ais_fmd.domain.categorize import pipeline
    from ais_fmd.domain.categorize.llm import LLMOutcome, residual_listing_chars

    sent = {}

    def fake(residual, **kwargs):
        sent.update(kwargs)
        return LLMOutcome()

    monkeypatch.setattr(pipeline, "classify_residual", fake)
    tickets = [row(f"{zelle_from(f'Member {n}')} SEMI", 25.00, when="2026-10-26") for n in range(3)]
    merch = row(f"{zelle_from('Member M')} AIS CREWNECK", 30.00, when="2026-10-22")
    unknown = row(zelle_from("Diana Perez"), 25.00, when="2026-11-03")
    run = categorize_records([*tickets, merch, unknown], dues=DuesSchedule([FALL_2026]))

    assert "18 Formal: 3 payment(s), $25.00 x3" in sent["context"]
    assert "13 Merch: 1 payment(s), $30.00 x1" in sent["context"]
    assert '"SEMI"' in sent["context"], "sample memos travel with it"
    assert run.context == sent["context"]
    # The run is sized with the context, so metering reserves for what is sent.
    assert residual_listing_chars(run.residual, run.context) > residual_listing_chars(run.residual)


def test_no_context_is_sent_when_nothing_is_left_for_the_model(monkeypatch):
    from ais_fmd.domain.categorize import pipeline
    from ais_fmd.domain.categorize.llm import LLMOutcome

    calls = []
    monkeypatch.setattr(pipeline, "classify_residual", lambda r, **kw: calls.append((r, kw)) or LLMOutcome())
    run = categorize_records([row(f"{zelle_from()} SEMI", 25.00)], dues=DuesSchedule([FALL_2026]))
    assert run.context == "" and run.classifications[0].committee_id == 18
    assert calls == [([], {})], "nothing to send, so no context either"
