"""
What kind of spending a charge was: food, a bar tab, supplies, and so on.

Used by My Committee's "Where the money went" (2026-11-09). A VP asks "what did
we spend it on?", not "which of forty restaurants did we pay?", so charges are
grouped by kind, read from the merchant name (`details.tidy`).

Big stores keep their own name. A Publix or Costco receipt could be food,
drinks or supplies, and the bank line does not say which, so calling it "Food"
would be a guess. "Publix" is honest.

The keyword lists are the treasurer's to adjust: a merchant that lands in
"Other" just needs adding to the right list below.
"""

from __future__ import annotations

import re

import pandas as pd

from .details import tidy

# Stores that sell everything: shown under their own name.
BIG_STORES: dict[str, str] = {
    "publix": "Publix",
    "costco": "Costco",
    "walmart": "Walmart",
    "target": "Target",
    "sam's club": "Sam's Club",
    "sams club": "Sam's Club",
    "amazon": "Amazon",
    "aldi": "Aldi",
    "trader joe": "Trader Joe's",
    "whole foods": "Whole Foods",
    "winn-dixie": "Winn-Dixie",
}

PAID_BACK = "Paid back to members"
OTHER = "Other"

# Checked in order; the first kind with a matching word wins. Each entry matches
# at the start of a word ("pub" finds "Macdintons Pub", not "Republic").
KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Drinks (liquor stores)", ("total wine", "abc fine wine", "liquor", "beer ", "wine shop")),
    ("Bar tabs", ("the grove", "macdinton", "salty dog", "brewing", "brewery", "pub", "tavern",
                  "saloon", "barcade", "arcade bar", "bar", "bourbon", "backyard", "lounge", "taproom")),
    ("Food & catering", ("jimmy john", "piesano", "chipotle", "panda", "menchie", "carretas", "sushi",
                         "chick-fil", "domino", "pizza", "dunkin", "insomnia", "starbucks", "krispy",
                         "panera", "jersey mike", "wawa", "food park", "catering", "restaurant", "cafe",
                         "coffee", "bagel", "taco", "grill", "wings", "mexic", "subway", "papa john",
                         "mcdonald", "wendy", "bakery", "donut", "doughnut", "cookie", "ice cream")),
    ("Supplies & printing", ("staples", "office depot", "vistaprint", "sticker mule", "customink",
                             "fedex", "michaels", "hobby lobby", "dollar tree", "party city", "ups store",
                             "print")),
    ("Venues & activities", ("reitz", "o'connell", "bowl", "dave & buster", "dave and buster",
                             "ichetucknee", "state park", "springs", "ticket", "stadium", "escape room",
                             "topgolf")),
    ("Conferences & travel", ("eventbrite", "delta", "american air", "southwest", "united air",
                              "marriott", "hilton", "hyatt", "hotel", "airbnb", "uber", "lyft",
                              "enterprise rent")),
    ("Software & ads", ("zoom", "canva", "meta platforms", "facebook", "google", "adobe", "squarespace",
                        "mailchimp", "linktree", "notion")),
)
_PATTERNS = [(kind, re.compile(r"\b(?:" + "|".join(re.escape(w.strip()) for w in words) + r")", re.I))
             for kind, words in KINDS]


def spend_kind(details: object) -> str:
    """'PURCHASE AUTHORIZED ON 09/02 PIESANOS ...' -> 'Food & catering'; Publix -> 'Publix'."""
    name = tidy(details)
    lowered = name.lower()
    if lowered.startswith(("venmo:", "zelle to")):
        return PAID_BACK
    for needle, store in BIG_STORES.items():
        if needle in lowered:
            return store
    for kind, pattern in _PATTERNS:
        if pattern.search(name):
            return kind
    return OTHER


def kind_split(spending: pd.DataFrame) -> pd.DataFrame:
    """
    Spending per kind, largest first: Kind, Amount (positive), Charges, Share (% of spending).

    `spending` should already be scoped to one committee and term with transfers
    removed; only negative amounts (money out) are counted.
    """
    columns = ["Kind", "Amount", "Charges", "Share"]
    if spending is None or spending.empty:
        return pd.DataFrame(columns=columns)
    out = spending[pd.to_numeric(spending["amount"], errors="coerce") < 0]
    if out.empty:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame({"Kind": out["details"].map(spend_kind),
                          "Amount": -pd.to_numeric(out["amount"], errors="coerce")})
    grouped = (frame.groupby("Kind", as_index=False)
               .agg(Amount=("Amount", "sum"), Charges=("Amount", "size"))
               .sort_values("Amount", ascending=False, ignore_index=True))
    grouped["Share"] = grouped["Amount"] / grouped["Amount"].sum() * 100
    return grouped[columns]
