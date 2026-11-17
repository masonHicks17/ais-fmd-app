"""
How a transaction reads on screen: merchant, card, date and amount.

Display only. Nothing here decides a committee, changes a stored value or feeds
the categorizer -- it turns what is already on the row into text a treasurer
can read at a glance. The raw bank line is always kept beside it.

  "PURCHASE AUTHORIZED ON 09/18 ARCADE BAR GNV GAINESVILLE FL S5721... CARD 1234"
      merchant  -> "Arcade Bar GNV"
      card      -> "Card 1234 · not on the card roster"
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from ais_fmd.domain.categorize.scoring import CardRegistry, card_number

# --- Merchant ------------------------------------------------------------------

_PURCHASE_PREFIX = re.compile(
    r"^(?:RECURRING\s+)?(?:PURCHASE|PAYMENT)(?:\s+RETURN)?\s+AUTHORIZED\s+ON\s+\d{1,2}/\d{1,2}\s+", re.I
)
# Wells Fargo's tail: a terminal/sequence id ("S572127510037"), then "CARD 1234".
_PURCHASE_TAIL = re.compile(r"\s+[SP]?\d{9,}\s+CARD\s+\d{3,5}\s*$", re.I)
_CARD_TAIL = re.compile(r"\s+CARD\s+\d{3,5}\s*$", re.I)
# "... GAINESVILLE FL" -- a city word and a two-letter state at the end.
_CITY_STATE = re.compile(r"\s+[A-Z][A-Z.'-]+\s+[A-Z]{2}$")
_ZELLE = re.compile(r"^ZELLE\s+(FROM|TO)\s+(.+?)(?:\s+ON\s+\d{1,2}/\d{1,2}.*)?$", re.I)
_TRANSFER = re.compile(r"^ONLINE\s+TRANSFER\s+(FROM|TO)\s+(.+?)(?:\s+(?:REF|ON)\b.*)?$", re.I)
_REF = re.compile(r"\s+(?:REF\s*#?\s*\w+|#\w+)\s*$", re.I)
_STORE_NUMBER = re.compile(r"^#?\d+$")
_VOWEL = re.compile(r"[AEIOUY]", re.I)


def _title(text: str) -> str:
    """Title-case a bank's upper-case name, leaving initialisms ("GNV", "UF") alone."""
    words = []
    for word in text.split():
        if word.isupper() and (len(word) <= 3 and not _VOWEL.search(word) or len(word) <= 2):
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:].lower())
    return " ".join(words)


def merchant(details: object) -> str:
    """A short, readable name for who the money went to or came from."""
    raw = " ".join(str(details or "").split())
    if not raw:
        return "No description"

    # Venmo-era rows: "<id> | <memo> | <from> | <to>".
    if raw.count("|") >= 3:
        parts = [part.strip() for part in raw.split("|")]
        sender, recipient = parts[2], parts[3]
        if sender.upper() == "UF AIS":
            return f"Venmo to {recipient}"
        return f"Venmo from {sender}"

    zelle = _ZELLE.match(raw)
    if zelle:
        return f"Zelle {zelle.group(1).lower()} {_title(_REF.sub('', zelle.group(2)))}"

    transfer = _TRANSFER.match(raw)
    if transfer:
        return f"Online transfer {transfer.group(1).lower()} {_title(transfer.group(2))}"

    if _PURCHASE_PREFIX.match(raw):
        name = _PURCHASE_PREFIX.sub("", raw)
        name = _PURCHASE_TAIL.sub("", name)
        name = _CARD_TAIL.sub("", name)
        trimmed = _CITY_STATE.sub("", name)
        name = trimmed if trimmed.strip() else name
        # "AMAZON MKTPL*2K4LM0" -> "AMAZON MKTPL"; "STAPLES 00114" -> "STAPLES".
        name = name.split("*")[0]
        words = [word for word in name.split() if not _STORE_NUMBER.match(word)]
        return _title(" ".join(words)) or raw[:40]

    name = _REF.sub("", _CARD_TAIL.sub("", raw))
    words = [word for word in name.split() if not _STORE_NUMBER.match(word)]
    short = " ".join(words)[:48].strip()
    return _title(short) or raw[:48]


# --- Card ----------------------------------------------------------------------

_REGISTRY: CardRegistry | None = None


def _registry() -> CardRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CardRegistry()
    return _REGISTRY


def _as_date(value: object) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(stamp) else stamp.date()


def card_line(details: object, when: object = None, amount: float | None = None) -> str:
    """
    Whose card paid, from the card roster -- or how the money moved.

    "Card 9570 · VP Operations", "Card 1234 · not on the card roster",
    "Zelle payment in". Empty when the line carries none of these.
    """
    raw = " ".join(str(details or "").split())
    card = card_number(raw)
    if card:
        assignment = _registry().get(card, _as_date(when))
        if assignment is None:
            return f"Card {card} · not on the card roster"
        holder = assignment.holder or "on the card roster"
        return f"Card {card} · {holder}" + ("" if assignment.verified else " (unconfirmed)")
    direction = ""
    if amount is not None and not pd.isna(amount):
        direction = " in" if float(amount) > 0 else " out"
    if raw.upper().startswith("ZELLE"):
        return f"Zelle payment{direction}"
    if raw.count("|") >= 3:
        memo = raw.split("|")[1].strip()
        return f"Venmo · {memo}" if memo else "Venmo"
    if raw.upper().startswith("ONLINE TRANSFER"):
        return f"Transfer{direction}"
    if raw.upper().startswith("DEPOSIT"):
        return "Deposit"
    return ""


# --- Date and amount -------------------------------------------------------------


def day(value: object, *, year: bool = False) -> str:
    """"Fri Sep 18" -- the weekday matters, because meeting days drive the rules."""
    when = _as_date(value)
    if when is None:
        return "—"
    text = f"{when:%a} {when:%b} {when.day}"
    return f"{text}, {when.year}" if year else text


def signed_money(amount: object) -> str:
    """"+$40.00" for money in, "−$411.72" for money out (a real minus sign)."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(value):
        return "—"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}${abs(value):,.2f}"


def is_money_in(amount: object) -> bool:
    try:
        return float(amount) > 0
    except (TypeError, ValueError):
        return False


# --- How a row was booked ------------------------------------------------------------


def how_booked(source: object, rule: object = "") -> str:
    """The categorizer's source code in words: card roster, rule, evidence or Haiku."""
    source = str(source or "")
    rule = str(rule or "")
    if source == "llm" or rule.startswith("Model"):
        return "Haiku"
    if source == "rule":
        return "Card roster" if rule.startswith("Card ") else "Rule"
    if source == "scored":
        return "Evidence score"
    if source == "proposed":
        return "Suggestion"
    return "Not booked"


def group_key(details: object) -> str:
    """Rows with the same merchant on the same card sit together and can be booked together."""
    return f"{merchant(details).lower()}|{card_number(details) or ''}"
