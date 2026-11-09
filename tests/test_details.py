"""Readable transaction descriptions (domain/details.py). Display only; storage is untouched."""

import pytest

from ais_fmd.domain.details import tidy


@pytest.mark.parametrize("raw, shown", [
    ("PURCHASE AUTHORIZED ON 08/18 PUBLIX SUPER MAR GAINESVILLE FL S386230123 CARD 4417", "Publix Super Mar"),
    ("PURCHASE AUTHORIZED ON 09/11 PUBLIX #1560 GAINESVILLE FL", "Publix"),
    ("PURCHASE AUTHORIZED ON 09/02 CHICK-FIL-A #02145 GA", "Chick-Fil-A"),
    ("PURCHASE AUTHORIZED ON 08/26 DOMINO'S 5521 GAINESVILLE FL", "Domino's"),
    ("PURCHASE AUTHORIZED ON 08/19 UF REITZ UNION GAINES S123456789 CARD 1234", "UF Reitz Union"),
    ("PURCHASE AUTHORIZED ON 01/23 CHIPOTLE ONLINE CA CARD 0319", "Chipotle Online"),
    ("PURCHASE AUTHORIZED ON 08/09 US MOBILE WWW.USMOBILE. NY CARD 3466", "US Mobile"),
    ("PURCHASE AUTHORIZED ON 08/01 SQ *BLUE BOTTLE GAINESVILLE FL CARD 1234", "Blue Bottle"),
    ("RECURRING PAYMENT AUTHORIZED ON 08/01 CANVA* 04512 SYDNEY AUS CARD 4417", "Canva (recurring)"),
    ("PURCHASE RETURN AUTHORIZED ON 08/30 AMAZON MKTPL GAINESVILLE FL CARD 4417", "Refund: Amazon Mktpl"),
    ("ZELLE FROM CONALL O’TOOLE ON 08/15 REF # ABC123", "Zelle from Conall O’Toole"),
    ("ZELLE TO SMITH JOHN ON 09/02 REF # PP0QRS7", "Zelle to Smith John"),
    ("3357852072640540 | Fall dues | Diego Alvarez | UF AIS", "Venmo: Diego Alvarez, fall dues"),
    ("3125006443753681 | Reimbursement | UF AIS | Sofia Ramirez", "Venmo: Sofia Ramirez, reimbursement"),
    ("DEPOSIT CORPORATE SPONSORSHIP REF78877", "Deposit corporate sponsorship"),
    ("MOBILE DEPOSIT : REF NUMBER :408071", "Mobile deposit"),
    ("MONTHLY SERVICE FEE", "Monthly service fee"),
    ("Coffee for GBM, typed by hand", "Coffee for GBM, typed by hand"),
])
def test_tidy(raw, shown):
    assert tidy(raw) == shown


def test_empty_and_missing():
    assert tidy(None) == "" and tidy(float("nan")) == ""
