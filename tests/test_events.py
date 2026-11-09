"""Events (domain/events.py): reading the schedule workbook, .ics, and matching spending."""

from __future__ import annotations

import io
from datetime import date, datetime, time

import pandas as pd

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import events as ev

TERMS = pd.DataFrame([
    {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18"},
])
HEADERS = [None, "Week/Date", "Event Title", "Progress", "Location", "Start Time", "End Time",
           "Duration", "Owner", "Audience", "Attendance", "Event Description", "Calculation Materials", "x"]


def _workbook(rows: list[list], sheet: str = "Fall 2026") -> bytes:
    title = [None, f"{sheet} AIS Event Schedule"] + [None] * (len(HEADERS) - 2)
    frame = pd.DataFrame([title, HEADERS, *rows])
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer) as writer:
        frame.to_excel(writer, sheet_name=sheet, header=False, index=False)
        pd.DataFrame([["idea"]]).to_excel(writer, sheet_name="Fall Idea List", header=False, index=False)
    return buffer.getvalue()


def _row(day, title, location=None, start=None, audience="General Body", attendance=None, extra=None):
    return [None, day, title, "On Calendar", location, start, None, None, "Maria & Austin",
            audience, attendance, None, "Calendar ID", extra]


def test_reads_events_by_header_and_skips_week_rows_and_idea_sheets():
    data = _workbook([
        _row("Week 1", "Mod 1 Start"),
        _row(datetime(2026, 8, 16), "Brunch at 4th Ave", "4th Ave Food Park", time(12, 0), attendance="20-25"),
        _row(datetime(2026, 8, 19), "GBM # 1", "Hough #120", time(18, 30)),
        _row("Thurs, Sep 24", "Workshop #1:", audience="Workshop"),
        _row(datetime(2026, 8, 20), None),
    ])
    result = ev.parse_schedule_workbook(data, "schedule.xlsx", TERMS)
    assert [e.title for e in result.events] == ["Brunch at 4th Ave", "GBM # 1", "Workshop #1:"]
    brunch, gbm, workshop = result.events
    assert brunch.start_time == "12:00" and brunch.attendance == 22 and brunch.term_id == "FA26"
    assert gbm.committee_id == ev.MEMBERSHIP
    assert workshop.event_date == date(2026, 9, 24)
    assert workshop.committee_id == ev.PROFESSIONAL_DEVELOPMENT
    assert result.sheets_read == ["Fall 2026"]


def test_rows_left_over_from_last_year_are_skipped_and_counted():
    data = _workbook([_row(datetime(2025, 8, 16), "MS-ISOM Orientation"),
                      _row(datetime(2026, 8, 10), "Orientation Social")])  # before term start: kept
    result = ev.parse_schedule_workbook(data, "schedule.xlsx", TERMS)
    assert [e.title for e in result.events] == ["Orientation Social"]
    assert result.skipped_outside_term == {"Fall 2026": 1}


def test_a_sheet_with_no_matching_term_says_so():
    result = ev.parse_schedule_workbook(_workbook([_row(datetime(2027, 1, 9), "Tab")], "Spring 2027"),
                                        "s.xlsx", TERMS)
    assert result.events == [] and "Spring 2027" in result.notes[0]


def test_calendar_id_is_read_from_the_calculation_column():
    result = ev.parse_schedule_workbook(
        _workbook([_row(datetime(2026, 8, 16), "Brunch", extra="c_abc@group.calendar.google.com")]),
        "s.xlsx", TERMS)
    assert result.calendar_id == "c_abc@group.calendar.google.com"


def test_ics_export():
    ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20260916T183000Z\r\nDTEND:20260916T200000Z\r\n"
        "SUMMARY:AIS Mixer\r\nLOCATION:Press Deck\\, Ben Hill\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20260917\r\nSUMMARY:Consulting\r\n Kickoff\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    events = ev.parse_upload(ics, "ais.ics", TERMS).events
    assert [(e.title, e.start_time, e.location) for e in events] == [
        ("AIS Mixer", "18:30", "Press Deck, Ben Hill"), ("ConsultingKickoff", None, None)]


def _txn(tid, posted, details, amount, category=None):
    return {"transactionid": tid, "transaction_date": posted, "details": details,
            "amount": amount, "budget_category": category}


EVENTS = pd.DataFrame([
    {"event_id": 1, "event_date": "2026-08-27", "title": "Pregame -> Barcade Karaoke", "location": None,
     "committee_id": ev.MEMBERSHIP},
    {"event_id": 2, "event_date": "2026-08-26", "title": "GBM # 2 - Trivia", "location": "Hough #120",
     "committee_id": ev.PRESIDENT},
])


def test_merchant_in_the_event_name_on_the_day_is_high_confidence():
    txns = pd.DataFrame([_txn(10, "2026-08-28", "PURCHASE AUTHORIZED ON 08/27 BARCADE GAINESVILLE FL S1 CARD 1234", -275)])
    [s] = ev.suggest_links(txns, EVENTS, pd.DataFrame())
    assert (s.event_id, s.confidence) == (1, ev.HIGH)
    assert "barcade" in s.reason.lower() and s.reason[0].isupper()


def test_food_categorized_to_the_events_committee_points_at_the_gbm():
    txns = pd.DataFrame([_txn(11, "2026-08-27", "PURCHASE AUTHORIZED ON 08/26 DOMINO'S 5521", -168, ev.PRESIDENT)])
    [s] = ev.suggest_links(txns, EVENTS, pd.DataFrame())
    assert s.event_id == 2 and s.confidence in (ev.HIGH, ev.MEDIUM)


def test_income_far_dates_and_linked_purchases_get_no_suggestion():
    txns = pd.DataFrame([
        _txn(12, "2026-08-27", "ZELLE FROM JANE DOE", 50),
        _txn(13, "2026-10-01", "PURCHASE AUTHORIZED ON 10/01 BARCADE", -20),
        _txn(14, "2026-08-28", "PURCHASE AUTHORIZED ON 08/27 BARCADE", -20),
    ])
    links = pd.DataFrame([{"transaction_id": 14, "event_id": 1}])
    assert ev.suggest_links(txns, EVENTS, links) == []


def test_spend_day_reads_the_authorized_date_across_new_year():
    assert ev.spend_day("PURCHASE AUTHORIZED ON 12/31 X", date(2027, 1, 2)) == date(2026, 12, 31)
    assert ev.purchase_label("PURCHASE AUTHORIZED ON 08/18 PUBLIX GAINESVILLE FL S386230123 CARD 4417") == \
        "Publix"


def test_sync_updates_in_place_and_keeps_links_and_attendance(tmp_path):
    backend = SqliteBackend(tmp_path / "events.db")
    event = ev.Event(event_date=date(2026, 9, 2), title="GBM # 3", term_id="FA26", source="Spreadsheet: a.xlsx")
    first = backend.sync_events([event.as_row()], "t@x")
    assert (first.updated, first.unchanged) == (1, 0)
    event_id = int(backend.fetch_events()["event_id"].iloc[0])
    backend.set_event_attendance(event_id, 80, "member database", "t@x")
    backend.link_transactions_to_events([(99, event_id)], "t@x")

    event.location = "Hough #120"
    again = backend.sync_events([event.as_row()], "t@x")
    assert (again.updated, again.unchanged) == (0, 1)
    stored = backend.fetch_events().iloc[0]
    assert int(stored["event_id"]) == event_id and stored["location"] == "Hough #120"
    assert int(stored["attendance"]) == 80
    assert backend.fetch_event_links()["event_id"].tolist() == [event_id]

    assert backend.unlink_transaction_from_event(99, "t@x").updated == 1
    assert backend.fetch_event_links().empty
