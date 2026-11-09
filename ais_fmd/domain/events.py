"""
Events: the club calendar, read from the event schedule spreadsheets (or an
.ics export of the Google Calendar), and which spending belongs to which event.

Why events at all (2026-11-08): the treasurer reviews transactions without
knowing what they were for, and VPs plan next semester without knowing what
last semester's events cost. Both questions are answered by tying a purchase to
the event it paid for. The calendar already exists -- Membership keeps "AIS
Event Schedule" workbooks, one sheet per semester, that feed the Google
Calendar -- so the app reads that rather than asking anyone to re-enter events.

Schedule sheet layout (MEMBERSHIP 2025-2026 / 2026-2027 workbooks):

  row 0   title ("Fall 2026 AIS Event Schedule")
  row 1   headers: Week/Date, Event Title, Progress, Location, Start Time,
          End Time, Duration or Public?, Owner, Audience, Attendance,
          Event Description, Calculation Materials (Calendar ID) ...
  then    one row per day; "Week N" rows split the weeks; most days are empty

Rows are read by header name, not position, because the Fall and Spring sheets
differ (Duration vs Public?). A row is an event when it has a real date and a
title. Each event belongs to the term whose dates contain it; rows dated
outside the sheet's own semester are skipped and counted, because the
2026-2027 workbook's "Spring 2027" sheet is still a copy of Spring 2026.

Matching spending to events (`suggest_links`) is rules, not a model: date
nearness, the merchant appearing in the event's location or title, the kind of
merchant (groceries for a GBM, a bar for a tab), and the committee it is
already categorized to. Each suggestion carries a plain-English reason, which
is the shape a Haiku explanation would take later; nothing is linked until a
person confirms it.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from .details import tidy

# Committee ids (config/categories.py). The club's seven committees since
# 2026-11-09. Every event is Membership's or Consulting's; GBMs, Passport and
# Formal are Membership's.
TREASURY = 2
PRESIDENT = 4
MEMBERSHIP = 5
OPERATIONS = 6
CONSULTING = 7
MARKETING = 9
PROFESSIONAL_DEVELOPMENT = 10

SEMESTER_SHEET = re.compile(r"^(Fall|Spring|Summer)\s+(\d{4})$", re.IGNORECASE)


@dataclass
class Event:
    event_date: date
    title: str
    start_time: str | None = None
    end_time: str | None = None
    location: str | None = None
    owner: str | None = None
    audience: str | None = None
    status: str | None = None
    attendance: int | None = None
    description: str | None = None
    committee_id: int | None = None
    term_id: str | None = None
    source: str = ""
    calendar_id: str | None = None

    @property
    def key(self) -> str:
        """Stable identity across syncs: the day and the normalised title."""
        return f"{self.event_date.isoformat()}|{_norm(self.title)}"

    def as_row(self) -> dict:
        return {
            "external_key": self.key,
            "term_id": self.term_id,
            "event_date": self.event_date.isoformat(),
            "title": self.title,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "location": self.location,
            "owner": self.owner,
            "audience": self.audience,
            "status": self.status,
            "committee_id": self.committee_id,
            "attendance": self.attendance,
            "attendance_source": "schedule" if self.attendance is not None else None,
            "description": self.description,
            "source": self.source,
            "calendar_id": self.calendar_id,
        }


@dataclass
class ParseResult:
    events: list[Event] = field(default_factory=list)
    skipped_outside_term: dict[str, int] = field(default_factory=dict)  # sheet -> rows
    sheets_read: list[str] = field(default_factory=list)
    calendar_id: str | None = None
    notes: list[str] = field(default_factory=list)


# --- Small parsers -------------------------------------------------------------

def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ""


def _text(value: object) -> str | None:
    return None if _blank(value) else str(value).strip()


def _time_text(value: object) -> str | None:
    """Times arrive as time objects, datetimes, bare hours (7), or free text."""
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, (int, float)) and float(value).is_integer() and 0 < value <= 12:
        return f"{int(value)}:00"  # a bare hour, as typed on the Spring sheets
    text = str(value).strip()
    return text[:-3] if re.fullmatch(r"\d{1,2}:\d{2}:00", text) else text


def _attendance(value: object) -> int | None:
    """35 -> 35; "20-25" -> 22 (the middle of a range); anything else -> None."""
    if _blank(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    numbers = [int(n) for n in re.findall(r"\d+", str(value))]
    if not numbers:
        return None
    return round(sum(numbers[:2]) / len(numbers[:2]))


def _event_day(value: object, year: int) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text or text.lower().startswith("week"):
        return None
    # "Thurs, Sep 24" -- a typed date with no year: the sheet's year.
    text = re.sub(r"^[A-Za-z]+\.?,\s*", "", text)  # "Thurs, " is not a weekday pandas knows
    stamp = pd.to_datetime(f"{text} {year}", errors="coerce", format="mixed")
    return None if pd.isna(stamp) else stamp.date()


def infer_committee(title: str, audience: str | None, owner: str | None) -> int:
    """
    Which committee's budget an event most likely draws on.

    Audience first, because the schedule says it outright for committee events;
    then the event's name; Membership for everything else, including GBMs,
    Passport and Formal (treasurer: every event is Membership's or Consulting's), since the general-body
    socials on these schedules are the Membership VPs' events.
    """
    audience_text = _norm(audience)
    title_text = _norm(title)
    if "consulting" in audience_text or "consulting" in title_text:
        return CONSULTING
    if "marketing" in audience_text:
        return MARKETING
    if "workshop" in audience_text or any(
        word in title_text for word in ("workshop", "networking", "headshot", "mixer", "info session")
    ):
        return PROFESSIONAL_DEVELOPMENT
    return MEMBERSHIP


def term_for_day(terms: pd.DataFrame, day: date) -> tuple[str | None, str | None]:
    """(TermID, Semester) whose start and end dates contain `day`."""
    if terms is None or terms.empty:
        return None, None
    starts = pd.to_datetime(terms["start_date"], errors="coerce").dt.date
    ends = pd.to_datetime(terms["end_date"], errors="coerce").dt.date
    hit = terms[(starts <= day) & (ends >= day)]
    if hit.empty:
        return None, None
    return str(hit.iloc[0]["TermID"]), str(hit.iloc[0]["Semester"])


# Events run a little outside the term's own dates: orientation socials the
# week before classes, a graduation the week after.
LEAD_DAYS, TRAIL_DAYS = 30, 10


def sheet_term(terms: pd.DataFrame, semester: str) -> tuple[str | None, tuple[date, date]]:
    """The TermID a semester sheet belongs to, and the days its events may fall on."""
    if terms is None or terms.empty:
        return None, (date.min, date.min)
    row = terms[terms["Semester"].astype(str) == semester]
    if row.empty:
        return None, (date.min, date.min)
    start = pd.to_datetime(row.iloc[0]["start_date"]).date()
    end = pd.to_datetime(row.iloc[0]["end_date"]).date()
    return str(row.iloc[0]["TermID"]), (start - timedelta(days=LEAD_DAYS), end + timedelta(days=TRAIL_DAYS))


# --- Readers -------------------------------------------------------------------

def _header_row(sheet: pd.DataFrame) -> int | None:
    for index in range(min(len(sheet), 6)):
        values = [_norm(v) for v in sheet.iloc[index].tolist()]
        if "event title" in values:
            return index
    return None


def _calendar_id(sheet: pd.DataFrame) -> str | None:
    for row in sheet.itertuples(index=False):
        cells = list(row)
        for position, cell in enumerate(cells[:-1]):
            if _norm(cell) == "calendar id" and not _blank(cells[position + 1]):
                return str(cells[position + 1]).strip()
    return None


def parse_schedule_workbook(data: bytes, filename: str, terms: pd.DataFrame) -> ParseResult:
    """Every event in an AIS Event Schedule workbook, one sheet per semester."""
    result = ParseResult()
    workbook = pd.ExcelFile(io.BytesIO(data))
    for sheet_name in workbook.sheet_names:
        match = SEMESTER_SHEET.match(sheet_name.strip())
        if not match:
            continue  # "Fall Idea List", "spring ideas list"
        semester = f"{match.group(1).title()} {match.group(2)}"
        year = int(match.group(2))
        sheet = pd.read_excel(workbook, sheet_name=sheet_name, header=None)
        header_at = _header_row(sheet)
        if header_at is None:
            continue
        result.sheets_read.append(sheet_name)
        term_id, window = sheet_term(terms, semester)
        if term_id is None:
            result.notes.append(f"{sheet_name}: no {semester} term yet (add it on Treasury → Terms).")
        result.calendar_id = result.calendar_id or _calendar_id(sheet)

        # First occurrence of each header wins: "Start Time" appears twice, the
        # second being a computed column out past the calculation materials.
        columns: dict[str, int] = {}
        for position, cell in enumerate(sheet.iloc[header_at].tolist()):
            name = _norm(cell)
            if name and name not in columns:
                columns[name] = position

        def cell(row: list, name: str) -> object:
            position = columns.get(name)
            return row[position] if position is not None and position < len(row) else None

        for row in sheet.iloc[header_at + 1 :].itertuples(index=False):
            row = list(row)
            title = _text(cell(row, "event title"))
            day = _event_day(cell(row, "week date"), year)
            if not title or day is None:
                continue
            if term_id is None or not (window[0] <= day <= window[1]):
                result.skipped_outside_term[sheet_name] = result.skipped_outside_term.get(sheet_name, 0) + 1
                continue
            audience = _text(cell(row, "audience"))
            owner = _text(cell(row, "owner"))
            result.events.append(
                Event(
                    event_date=day,
                    title=title,
                    start_time=_time_text(cell(row, "start time")),
                    end_time=_time_text(cell(row, "end time")),
                    location=_text(cell(row, "location")),
                    owner=owner,
                    audience=audience,
                    status=_text(cell(row, "progress")),
                    attendance=_attendance(cell(row, "attendance")),
                    description=_text(cell(row, "event description")),
                    committee_id=infer_committee(title, audience, owner),
                    term_id=term_id,
                    source=f"Spreadsheet: {filename}",
                )
            )
    for event in result.events:
        event.calendar_id = result.calendar_id
    return result


def _ics_unescape(text: str) -> str:
    return text.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def parse_ics(data: bytes, filename: str, terms: pd.DataFrame) -> ParseResult:
    """
    Events from an .ics file -- what Google Calendar's "Export" gives you.

    Enough of RFC 5545 for a Google export: folded lines, DTSTART as a date or
    a date-time (UTC "Z" times are shown as given), SUMMARY, LOCATION,
    DESCRIPTION. No recurrence expansion: a weekly meeting appears once.
    """
    result = ParseResult(sheets_read=[filename])
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)  # unfold
    for block in re.findall(r"BEGIN:VEVENT\n(.*?)END:VEVENT", text, flags=re.DOTALL):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields.setdefault(name.split(";")[0].upper(), value)
        title = _ics_unescape(fields.get("SUMMARY", ""))
        start = fields.get("DTSTART", "")
        if not title or len(start) < 8:
            continue
        day = datetime.strptime(start[:8], "%Y%m%d").date()
        start_time = f"{start[9:11]}:{start[11:13]}" if len(start) >= 13 else None
        end = fields.get("DTEND", "")
        end_time = f"{end[9:11]}:{end[11:13]}" if len(end) >= 13 else None
        term_id, _ = term_for_day(terms, day)
        description = _ics_unescape(fields.get("DESCRIPTION", "")) or None
        result.events.append(
            Event(
                event_date=day,
                title=title,
                start_time=start_time,
                end_time=end_time,
                location=_ics_unescape(fields.get("LOCATION", "")) or None,
                description=description,
                committee_id=infer_committee(title, description, None),
                term_id=term_id,
                source=f"Calendar file: {filename}",
            )
        )
    return result


def parse_upload(data: bytes, filename: str, terms: pd.DataFrame) -> ParseResult:
    if filename.lower().endswith(".ics"):
        return parse_ics(data, filename, terms)
    return parse_schedule_workbook(data, filename, terms)


# --- Spending -> events ----------------------------------------------------------

# Kinds of merchant, and the kinds of event they usually pay for. Lower-case
# substrings of the bank description.
MERCHANT_KINDS: dict[str, tuple[str, ...]] = {
    "food": ("publix", "costco", "walmart", "target", "chick-fil-a", "domino", "pizza",
             "chipotle", "menchies", "insomnia", "dunkin", "starbucks", "krispy", "panera",
             "jersey mike", "wawa", "food park", "catering"),
    "bar": ("salty dog", "total wine", "barcade", "abc fine wine", "macdinton", "social",
            "the backyard", "first magnitude", "bourbon", "dog"),
    "venue": ("reitz", "o'connell", "bowl", "dave & buster", "dave and buster", "ichetucknee"),
    "print": ("sticker mule", "canva", "vistaprint", "fedex"),
}
EVENT_KINDS: dict[str, tuple[str, ...]] = {
    "food": ("gbm", "brunch", "coffee", "snack", "pastr", "donut", "ice cream", "picnic",
             "study", "meeting", "kickoff", "workshop", "mixer"),
    "bar": ("tab", "pregame", "after hours", "social", "champagne", "wine", "party",
            "tailgate", "mystery", "watch party", "barcade"),
    "venue": ("bowling", "trip", "dave", "springs", "game"),
    "print": ("headshot", "mixer", "gbm"),
}
STOPWORDS = {"the", "and", "with", "after", "game", "party", "event", "hough", "room",
             "apartment", "apt", "place", "house", "general", "body", "meeting", "night",
             "gainesville", "purchase", "authorized", "card"}

HIGH, MEDIUM, LOW = "High", "Medium", "Low"


def _kind(text: str, table: dict[str, tuple[str, ...]]) -> set[str]:
    lowered = text.lower()
    return {kind for kind, needles in table.items() if any(n in lowered for n in needles)}


def _tokens(*texts: object) -> set[str]:
    words = set()
    for text in texts:
        words |= {w for w in _norm(text).split() if len(w) >= 4 and w not in STOPWORDS}
    return words


AUTHORIZED_ON = re.compile(r"AUTHORIZED ON (\d{2})/(\d{2})", re.IGNORECASE)


def spend_day(details: object, posted: date) -> date:
    """
    The day the money was spent: "PURCHASE AUTHORIZED ON 08/18" when the bank
    says so (posting lags a day or two), else the posting date.
    """
    match = AUTHORIZED_ON.search(str(details or ""))
    if not match:
        return posted
    try:
        day = date(posted.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return posted
    return day.replace(year=posted.year - 1) if day > posted + timedelta(days=7) else day


def purchase_label(details: object) -> str:
    """The readable merchant name: the Transactions table's rules (domain/details.py)."""
    return tidy(details)


@dataclass(frozen=True)
class Suggestion:
    transaction_id: int
    event_id: int
    score: float
    confidence: str
    reason: str


def _score(txn: dict, event: dict, committees: dict[int, str]) -> tuple[float, list[str]]:
    details = str(txn.get("details") or "")
    is_reimbursement = "reimburse" in details.lower()
    gap = (txn["_day"] - event["_day"]).days  # negative: spent before the event
    if gap == 0:
        score, when = 1.0, "same day"
    elif -1 <= gap <= 1:
        score, when = 0.8, f"{abs(gap)} day {'before' if gap < 0 else 'after'}"
    elif -6 <= gap <= 2:
        score, when = 0.5, f"{abs(gap)} days {'before' if gap < 0 else 'after'}"
    elif is_reimbursement and 0 < gap <= 21:
        score, when = 0.4, f"reimbursed {gap} days after"
    else:
        return 0.0, []
    reasons = [when]

    shared = _tokens(details) & _tokens(event.get("location"), event.get("title"))
    if shared:
        score += 0.7
        reasons.append(f"“{sorted(shared)[0].title()}” is in the event's location or name")
    kinds = _kind(details, MERCHANT_KINDS) & _kind(f"{event.get('title')} {event.get('location')}", EVENT_KINDS)
    if kinds:
        score += 0.25
        kind = sorted(kinds)[0]
        reasons.append({"food": "a food purchase for a food event", "bar": "a bar or drinks purchase for a social",
                        "venue": "a venue or activity charge", "print": "printing or design for this event"}[kind])

    category = txn.get("budget_category")
    if category is not None and not pd.isna(category) and event.get("committee_id") is not None:
        if int(category) == int(event["committee_id"]):
            score += 0.3
            reasons.append(f"already categorized to {committees.get(int(category), 'its committee')}")
        else:
            score -= 0.15
    return score, reasons


def suggest_links(
    transactions: pd.DataFrame,
    events: pd.DataFrame,
    links: pd.DataFrame,
    committees: pd.DataFrame | None = None,
    *,
    minimum: float = 0.75,
) -> list[Suggestion]:
    """
    The likeliest event for each unlinked expense, where one is likely enough.

    Only spending (negative amounts) is considered. When two events score
    within 0.1 of each other the suggestion is marked Low however high the
    score, because "it's one of these two" is not something to confirm blind.
    """
    if transactions is None or transactions.empty or events is None or events.empty:
        return []
    linked = set(links["transaction_id"].astype(int)) if links is not None and not links.empty else set()
    names = (
        dict(zip(committees["CommitteeID"].astype(int), committees["Committee_Name"].astype(str)))
        if committees is not None and not committees.empty and "CommitteeID" in committees
        else {}
    )
    event_rows = events.to_dict("records")
    for event in event_rows:
        event["_day"] = pd.to_datetime(event["event_date"]).date()
    first = min(e["_day"] for e in event_rows) - timedelta(days=7)
    last = max(e["_day"] for e in event_rows) + timedelta(days=22)

    suggestions = []
    for txn in transactions.to_dict("records"):
        amount = pd.to_numeric(txn.get("amount"), errors="coerce")
        txn_id = txn.get("transactionid")
        if pd.isna(amount) or amount >= 0 or txn_id is None or int(txn_id) in linked:
            continue
        posted = pd.to_datetime(txn.get("transaction_date"), errors="coerce")
        if pd.isna(posted):
            continue
        txn["_day"] = spend_day(txn.get("details"), posted.date())
        if not (first <= txn["_day"] <= last):
            continue
        scored = sorted(
            ((score, reasons, event) for event in event_rows
             for score, reasons in [_score(txn, event, names)] if score > 0),
            key=lambda item: item[0],
            reverse=True,
        )
        if not scored or scored[0][0] < minimum:
            continue
        best_score, reasons, best = scored[0]
        close_second = len(scored) > 1 and best_score - scored[1][0] < 0.1
        if close_second:
            confidence = LOW
            reasons = reasons + [f"also close to “{scored[1][2]['title']}”"]
        elif best_score >= 1.5:
            confidence = HIGH
        elif best_score >= 1.05:
            confidence = MEDIUM
        else:
            confidence = LOW
        suggestions.append(
            Suggestion(
                transaction_id=int(txn_id),
                event_id=int(best["event_id"]),
                score=round(best_score, 2),
                confidence=confidence,
                reason=_sentence("; ".join(reasons)),
            )
        )
    return suggestions


def _sentence(text: str) -> str:
    return (text[:1].upper() + text[1:] + ".") if text else ""


def event_spending(transactions: pd.DataFrame, links: pd.DataFrame) -> pd.DataFrame:
    """event_id -> Spent (positive dollars) and Purchases, from confirmed links."""
    if links is None or links.empty or transactions is None or transactions.empty:
        return pd.DataFrame(columns=["event_id", "Spent", "Purchases"])
    joined = links.merge(
        transactions[["transactionid", "amount"]], left_on="transaction_id", right_on="transactionid"
    )
    joined["amount"] = pd.to_numeric(joined["amount"], errors="coerce").fillna(0)
    grouped = joined.groupby("event_id").agg(Spent=("amount", lambda s: -s.sum()), Purchases=("amount", "size"))
    return grouped.reset_index()
