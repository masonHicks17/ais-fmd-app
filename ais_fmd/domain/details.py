"""
Readable transaction descriptions.

Bank text is written for the bank: "PURCHASE AUTHORIZED ON 08/18 PUBLIX SUPER
MAR GAINESVILLE FL S386230123 CARD 4417" says "Publix" in 50 characters. The
treasurer asked (2026-11-08) for the Transactions table to show just the part
a person reads. `tidy` does that for display only -- the stored `details` is
never changed, search still runs on it, and the original stays one toggle away.

Rules, in order (each is a line in RULES or a step in `tidy`):

  card purchase     "PURCHASE AUTHORIZED ON 08/18 X ..."      -> "X"
  recurring charge  "RECURRING PAYMENT AUTHORIZED ON 08/01 X" -> "X (recurring)"
  card refund       "PURCHASE RETURN AUTHORIZED ON 08/01 X"   -> "Refund: X"
  Zelle             "ZELLE FROM JANE DOE ON 08/14 REF # ABC"  -> "Zelle from Jane Doe"
  Venmo             "3000132... | Fall dues | Hana Kim | UF AIS" -> "Venmo: Hana Kim, Fall dues"
  references        "REF # ...", "REF NUMBER : ...", "REF12345", card and
                    terminal numbers ("S386230123", "CARD 4417")  -> dropped
  place             a trailing "GAINESVILLE FL" (or the bank's cut-off
                    "GAINESVI", "GAIN") and a trailing state code -> dropped
  store numbers     "#1560", "5521" at the end of a merchant      -> dropped
  processors        "SQ *BLUE BOTTLE" -> "Blue Bottle"; "CANVA* 04512 ..." -> "Canva"
  web addresses     "WWW.USMOBILE." -> dropped
  case              SHOUTING -> Title Case, short acronyms (UF, AIS, UAA) kept
"""

from __future__ import annotations

import re

_SPACES = re.compile(r"\s+")
_CARD_LEAD = re.compile(
    r"^(?P<kind>PURCHASE RETURN|RECURRING PAYMENT|PURCHASE)\s+AUTHORIZED ON\s+\d{2}/\d{2}\s*",
    re.IGNORECASE,
)
_ZELLE = re.compile(r"^ZELLE (?P<dir>FROM|TO)\s+(?P<name>.+?)(?:\s+ON\s+\d{2}/\d{2}.*)?$", re.IGNORECASE)
_REFERENCE = re.compile(
    r"\s*\bREF(?:ERENCE)?\s*(?:NUMBER|NO\.?|#)?\s*:?\s*#?\s*\w*\d\w*"  # REF # ABC123, REF NUMBER :408071, REF78877
    r"|\s+S\d{6,}\b"  # card terminal id
    r"|\s+S\d{1,4}(?:FAKE)?\b(?=\s+CARD|\s*$)"
    r"|\s+CARD\s+\d+\b",
    re.IGNORECASE,
)
_PLACE = re.compile(r"(?:\s+GAINESV?I?L?L?E?\w*|\s+GAIN)?(?:\s+[A-Z]{2})?\s*$")
_STORE_NUMBER = re.compile(r"(?:\s+#\s*\d+|\s+\d{3,})+\s*$")
_TRAILING_PUNCT = re.compile(r"[\s:.,;-]+$")

# Kept in capitals however short the word: these are names, not shouting.
ACRONYMS = {"US", "UF", "AIS", "UAA", "ABC", "ATM", "USA", "LLC", "CVS", "IHOP", "KFC", "UPS", "TJ", "BBQ", "ISOM"}
SMALL_WORDS = {"of", "and", "the", "on", "to", "from", "for", "at", "in", "a", "an"}


def _title(text: str) -> str:
    """'CHICK-FIL-A #02145' -> 'Chick-fil-a', 'DOMINO'S' -> "Domino's", 'UAA' stays."""
    if not text or any(ch.islower() for ch in text):
        return text  # already mixed case: someone typed it that way
    words = []
    for index, word in enumerate(text.split(" ")):
        bare = re.sub(r"[^A-Za-z]", "", word)
        if bare.upper() in ACRONYMS:
            words.append(word.upper())
        elif index and word.lower() in SMALL_WORDS:
            words.append(word.lower())
        else:
            words.append("-".join(_cap(part) for part in word.split("-")))
    return " ".join(words)


def _cap(word: str) -> str:
    """'o’toole' -> 'O’Toole', but "domino's" -> "Domino's" (one letter after the apostrophe)."""
    word = word[:1].upper() + word[1:].lower()
    return re.sub(r"(['’])([a-z]{2,})", lambda m: m.group(1) + m.group(2).capitalize(), word)


# Payment processors put their own name first: "SQ *BLUE BOTTLE", "TST* MIDTOWN".
_PROCESSOR = re.compile(r"^(?:SQ|TST|SP|PY|PAYPAL|DD|IC)\s*\*\s*", re.IGNORECASE)
# Billing descriptors after a star: "CANVA* 04512 SYDNEY AUS" is Canva.
_AFTER_STAR = re.compile(r"\*.*$")
_WEB = re.compile(r"\s+\S*(?:WWW\.|\.COM|\.NET|\.ORG)\S*", re.IGNORECASE)


def _merchant(text: str) -> str:
    text = _REFERENCE.sub("", text)
    text = _PROCESSOR.sub("", text)
    text = _AFTER_STAR.sub("", text)
    text = _WEB.sub("", text)
    for _ in range(2):  # "PUBLIX #1560 GAINESVILLE FL": place, then store number, then place again
        text = _PLACE.sub("", text) or text
        text = _STORE_NUMBER.sub("", text) or text
    return _title(_TRAILING_PUNCT.sub("", text).strip())


def _venmo(parts: list[str]) -> str | None:
    """'id | note | from | to' -- whichever side is not the club is the person."""
    fields = [p.strip() for p in parts if p.strip() and not p.strip().isdigit()]
    people = [p for p in fields if p.upper() not in {"UF AIS", "AIS", "UF AIS FMD"}]
    if not people:
        return None
    note, *others = people
    if not others:
        return f"Venmo: {note}"
    return f"Venmo: {', '.join(others)}, {note[:1].lower() + note[1:] if not note[:2].isupper() else note}"


def tidy(details: object) -> str:
    """The readable part of a bank description; the original if no rule applies."""
    if details is None or details != details:  # None or NaN
        return ""
    raw = _SPACES.sub(" ", str(details)).strip()
    if not raw:
        return ""

    if raw.count("|") >= 2:
        venmo = _venmo(raw.split("|"))
        if venmo:
            return venmo

    zelle = _ZELLE.match(raw)
    if zelle:
        name = _REFERENCE.sub("", zelle.group("name")).strip()
        return f"Zelle {zelle.group('dir').lower()} {_title(name)}"

    card = _CARD_LEAD.match(raw)
    if card:
        merchant = _merchant(raw[card.end():]) or "Card purchase"
        kind = card.group("kind").upper()
        if kind == "PURCHASE RETURN":
            return f"Refund: {merchant}"
        if kind == "RECURRING PAYMENT":
            return f"{merchant} (recurring)"
        return merchant

    # Anything else -- deposits, transfers, fees: drop references, fix the case.
    cleaned = _TRAILING_PUNCT.sub("", _REFERENCE.sub("", raw)).replace(" : ", " ").strip()
    if cleaned.isupper():
        cleaned = cleaned.lower()
        cleaned = cleaned[:1].upper() + cleaned[1:]
    return cleaned or raw
