"""
How a transaction reads on screen. Display only: these never decide a committee.
"""

from __future__ import annotations

import pytest

from ais_fmd.ui import txn_display as show


@pytest.mark.parametrize(
    "details,expected",
    [
        ("PURCHASE AUTHORIZED ON 09/18 ARCADE BAR GNV GAINESVILLE FL S572127510037 CARD 1234", "Arcade Bar GNV"),
        ("PURCHASE AUTHORIZED ON 02/03 STAPLES 00114 GAINESVILLE FL S669119220495 CARD 1234", "Staples"),
        ("PURCHASE AUTHORIZED ON 10/08 AMAZON MKTPL*2K4LM0 AMZN.COM/BILL WA S123456789012 CARD 4417", "Amazon Mktpl"),
        ("RECURRING PAYMENT AUTHORIZED ON 10/01 GOOGLE *GSUITE CC@GOOGLE.COM CA S384763829473 CARD 3466", "Google"),
        ("ZELLE FROM SEMIRA PATEL ON 10/14 REF # PP0XYZ SEMI FORMAL", "Zelle from Semira Patel"),
        ("ONLINE TRANSFER FROM PAYPAL REF #IB0XYZ", "Online transfer from Paypal"),
        ("3508969831601894 | Fall dues | Ava Mitchell | UF AIS", "Venmo from Ava Mitchell"),
        ("3479284563409983 | Reimbursement | UF AIS | Hana Kim", "Venmo to Hana Kim"),
        ("DEPOSIT CORPORATE SPONSORSHIP REF78877", "Deposit Corporate Sponsorship"),
        ("", "No description"),
        (None, "No description"),
    ],
)
def test_merchant_names_read_like_names(details, expected):
    assert show.merchant(details) == expected


def test_a_card_on_the_roster_names_its_holder():
    line = show.card_line("PURCHASE ... S111111111111 CARD 9570", "2026-10-01")
    assert line == "Card 9570 · VP Operations"


def test_a_card_off_the_roster_says_so():
    assert show.card_line("PURCHASE ... CARD 1234", "2026-10-01") == "Card 1234 · not on the card roster"


def test_money_movements_without_a_card():
    assert show.card_line("ZELLE FROM A PERSON ON 10/14", amount=40) == "Zelle payment in"
    assert show.card_line("1 | Fall dues | Ava | UF AIS") == "Venmo · Fall dues"
    assert show.card_line("SOMETHING ELSE") == ""


def test_dates_carry_the_weekday():
    assert show.day("2026-09-18") == "Fri Sep 18"
    assert show.day("2026-10-07", year=True) == "Wed Oct 7, 2026"
    assert show.day(None) == "—"


def test_amounts_are_signed():
    assert show.signed_money(40) == "+$40.00"
    assert show.signed_money(-1411.7) == "−$1,411.70"
    assert show.signed_money(0) == "$0.00"
    assert show.signed_money("x") == "—"


@pytest.mark.parametrize(
    "source,rule,expected",
    [
        ("rule", "Card 9570 (VP Operations) — on the 2026-2027 card roster", "Card roster"),
        ("rule", "SEMI in memo", "Rule"),
        ("scored", "Scored: ...", "Evidence score"),
        ("llm", "Model: ticket", "Haiku"),
        ("none", "", "Not booked"),
    ],
)
def test_how_a_row_was_booked_in_words(source, rule, expected):
    assert show.how_booked(source, rule) == expected


def test_look_alike_charges_share_a_group():
    first = "PURCHASE AUTHORIZED ON 09/18 ARCADE BAR GNV GAINESVILLE FL S572127510037 CARD 1234"
    second = "PURCHASE AUTHORIZED ON 10/02 ARCADE BAR GNV GAINESVILLE FL S582149537712 CARD 1234"
    other_card = "PURCHASE AUTHORIZED ON 10/02 ARCADE BAR GNV GAINESVILLE FL S582149537712 CARD 9570"
    assert show.group_key(first) == show.group_key(second)
    assert show.group_key(first) != show.group_key(other_card)
