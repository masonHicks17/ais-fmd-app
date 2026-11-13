"""
Events as an .ics file for Google Calendar (2026-11-13).

Google Calendar imports iCalendar files (Settings -> Import & export ->
Import). Each event carries a stable UID built from its event_id, so
importing the same file twice doesn't add copies, and an event moved here
and exported again is recognised as the same event.

Times: the schedule sheets hold "18:30", "7:00", "7 PM" and the odd word
("TBD"). Anything that reads as a time becomes a timed event in the club's
time zone; anything else exports as an all-day event. A bare hour from 1 to 7
with no am/pm is taken as evening -- club events don't start at 7 in the
morning, and the Spring sheets type "7" for 7 PM. A zero-padded "07:00"
(what a time picker or a real 24-hour clock writes) is kept as morning.

Money is never exported: every VP sees the club calendar, but an event's
cost belongs to its committee.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd

TIME_ZONE = "America/New_York"
PRODUCT = "-//AIS FMD//Events//EN"
UID_DOMAIN = "ais-fmd.events"
# Columns of the schedule sheet that .ics has no field for, written into the
# description as "Owner: ..." lines (and read back by domain/events.parse_ics).
LABELLED = (("Owner", "owner"), ("Audience", "audience"), ("Status", "status"))

# The time at the start of the cell: "18:30", "7", "7 PM", "1030", and the
# start of "9:30-11:00" or "4pmish".
_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2})|(\d{2}))?\s*(?:([ap])\.?\s*m?\b\.?)?", re.IGNORECASE)


def parse_time(text: object) -> tuple[int, int] | None:
    """(hour, minute) in 24-hour time, or None when `text` isn't a time."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return None
    match = _TIME.match(str(text))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or match.group(3) or 0)
    half = (match.group(4) or "").lower()
    if minute > 59 or hour > 23:
        return None
    if half:
        if hour < 1 or hour > 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    elif 1 <= hour <= 7 and not match.group(1).startswith("0"):
        hour += 12  # "7:00" typed on a sheet; "07:00" is a real 24-hour time
    return hour, minute


def resolve_times(start: object, end: object) -> tuple[tuple[int, int] | None, tuple[int, int] | None, bool]:
    """
    (start, end, end_is_filler) as a calendar would show them. An end that
    reads as before the start is taken as the evening ("7:30" to "09:00");
    no usable end means one hour, flagged as filler so a round trip through
    Google Calendar doesn't count that hour as a change.
    """
    begins, ends = parse_time(start), parse_time(end)
    if begins is None:
        return None, None, False
    if ends is not None and ends <= begins and ends[0] < 12 and (ends[0] + 12, ends[1]) > begins:
        ends = (ends[0] + 12, ends[1])
    if ends is None or ends <= begins:
        hour = min(begins[0] + 1, 23)
        return begins, (hour, begins[1] if hour > begins[0] else 59), True
    return begins, ends, False


def _escape(text: object) -> str:
    value = "" if text is None or (isinstance(text, float) and pd.isna(text)) else str(text)
    value = value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return value.replace("\r\n", "\\n").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space (RFC 5545)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        piece = char.encode("utf-8")
        if len(current) + len(piece) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += piece
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def _event_lines(row: dict, committee_names: dict[int, str], stamp: str) -> list[str]:
    day = pd.to_datetime(row["event_date"]).date()
    start, end, _ = resolve_times(row.get("start_time"), row.get("end_time"))
    lines = ["BEGIN:VEVENT", f"UID:event-{int(row['event_id'])}@{UID_DOMAIN}", f"DTSTAMP:{stamp}"]
    if start:
        begins = datetime(day.year, day.month, day.day, *start)
        ends = datetime(day.year, day.month, day.day, *end)
        lines += [
            f"DTSTART;TZID={TIME_ZONE}:{begins:%Y%m%dT%H%M%S}",
            f"DTEND;TZID={TIME_ZONE}:{ends:%Y%m%dT%H%M%S}",
        ]
    else:
        lines += [
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        ]
    lines.append(f"SUMMARY:{_escape(row['title'])}")
    if row.get("location") is not None and not pd.isna(row.get("location")):
        lines.append(f"LOCATION:{_escape(row['location'])}")
    committee = row.get("committee_id")
    committee_name = (
        committee_names.get(int(committee), "") if committee is not None and not pd.isna(committee) else ""
    )
    # The description, then the sheet's own columns as labelled lines, which
    # domain/events.parse_ics reads back -- so a file exported here and synced
    # back from Google Calendar matches what the app holds.
    notes = [str(row[k]) for k in ("description",) if row.get(k) is not None and not pd.isna(row.get(k))]
    labelled = [f"{label}: {row[key]}" for label, key in LABELLED
                if row.get(key) is not None and not pd.isna(row.get(key)) and str(row.get(key)).strip()]
    if committee_name:
        lines.append(f"CATEGORIES:{_escape(committee_name)}")
    # "None" too, so reading the file back doesn't guess a committee from the title.
    labelled.append(f"Committee: {committee_name or 'None'}")
    if labelled:
        notes += ([""] if notes else []) + labelled
    if notes:
        lines.append(f"DESCRIPTION:{_escape(chr(10).join(notes))}")
    status = str(row.get("status") or "").lower()
    if "cancel" in status:
        lines.append("STATUS:CANCELLED")
    elif "tentative" in status or "tbd" in status:
        lines.append("STATUS:TENTATIVE")
    lines.append("END:VEVENT")
    return lines


def to_ics(
    events: pd.DataFrame,
    committee_names: dict[int, str] | None = None,
    *,
    calendar_name: str = "AIS Events",
    now: datetime | None = None,
) -> bytes:
    """The events as one iCalendar file, CRLF line endings, UTF-8."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODUCT}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
        f"X-WR-TIMEZONE:{TIME_ZONE}",
    ]
    rows = [] if events is None or events.empty else events.sort_values("event_date").to_dict("records")
    for row in rows:
        lines += _event_lines(row, committee_names or {}, stamp)
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode("utf-8")


def changed_here(events: pd.DataFrame) -> pd.DataFrame:
    """Events added, moved or edited in the app since the calendar last synced them."""
    if events is None or events.empty or "edited_in_app" not in events:
        return events.iloc[0:0] if events is not None else pd.DataFrame()
    flag = pd.to_numeric(events["edited_in_app"], errors="coerce").fillna(0).astype(int)
    return events[flag == 1]


def file_name(scope: str, today: date | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-") or "events"
    return f"ais-events-{slug}-{(today or date.today()):%Y-%m-%d}.ics"
