"""
The calendar as a calendar app (2026-11-13): costs typed in the event panel,
adding an event, editing and moving one, deleting one, the drag and
double-click markup, and the .ics export for Google Calendar.

Drag and double-click themselves happen in the browser (ui/clickable.py); here
the markup they need is checked, and the storage they call is driven directly.
Uses the fixture from test_events_vp.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pandas as pd

from ais_fmd import auth
from ais_fmd.domain import calendar_export
from ais_fmd.ui import calendar_grid

from tests.test_events_vp import (  # noqa: F401 - fixtures
    CONSULTING, MEMBERSHIP, _event_id, events_db, page_text, run_as,
)
from tests.test_views import assert_clean, seeded_db, use_db  # noqa: F401

OPEN, ADD = "events_open", "events_add_day"


def _rows(db_path, sql: str, *args) -> list[tuple]:
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, args).fetchall()


def _reopen(app, event_id: int):
    """Run the pending click, then open the panel again (a change reruns the whole page in a test)."""
    app.session_state[OPEN] = event_id
    app = app.run()
    app.session_state[OPEN] = event_id
    return app.run()


def _submit(app, label: str):
    next(b for b in app.button if b.label == label).click()


# --- Costs in the panel ---------------------------------------------------------


def test_the_treasurer_adds_an_estimate_and_a_typed_cost(events_db):
    event = _event_id(events_db, "k-mem")
    app = run_as(auth.Role.TREASURER, events_open=event)
    assert_clean(app, "Treasurer opens an event")
    app.text_input(key=f"events_cost_label_{event}").input("Pizza")
    app.number_input(key=f"events_cost_amount_{event}").set_value(300.0)
    _submit(app, "Add cost")
    app = _reopen(app, event)
    app.text_input(key=f"events_cost_label_{event}").input("Cash for ice")
    app.number_input(key=f"events_cost_amount_{event}").set_value(40.0)
    app.selectbox(key=f"events_cost_kind_{event}").select("Paid, not on the statement")
    _submit(app, "Add cost")
    app = _reopen(app, event)
    assert sorted(_rows(events_db, "SELECT kind, label, amount FROM event_costs WHERE event_id = ?", event)) == [
        ("estimate", "Pizza", 300.0), ("manual", "Cash for ice", 40.0),
    ]
    text = (page_text(app) + "".join(str(e.proto) for e in app.get("html"))).replace(",", "")
    assert "Estimated" in text and "$300.00" in text
    # Spent = the linked $1,234.56 purchase + the $40 typed cost.
    assert "1274.56" in text


def test_a_cost_line_can_be_removed(events_db):
    event = _event_id(events_db, "k-mem")
    with sqlite3.connect(events_db) as db:
        db.execute("INSERT INTO event_costs (event_id, kind, label, amount) VALUES (?, 'estimate', 'Food', 50)", (event,))
    app = run_as(auth.Role.TREASURER, events_open=event)
    next(b for b in app.button if b.key and b.key.startswith("events_cost_del_")).click()
    _reopen(app, event)
    assert _rows(events_db, "SELECT * FROM event_costs WHERE event_id = ?", event) == []


def test_a_vp_types_costs_on_their_own_events_but_not_on_others(events_db):
    mine, theirs = _event_id(events_db, "k-mem"), _event_id(events_db, "k-con")
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_open=mine)
    assert any(b.label == "Add cost" for b in app.button)
    assert any(e.label == "Edit event" for e in app.expander)
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_open=theirs)
    assert not any(b.label == "Add cost" for b in app.button)
    assert not any(e.label == "Edit event" for e in app.expander)


# --- Adding, editing, moving, deleting ------------------------------------------


def test_adding_an_event_from_a_day(events_db):
    day = date.today()
    app = run_as(auth.Role.TREASURER, **{ADD: day})
    assert_clean(app, "Add-event form")
    app.text_input(key="events_new_title").input("Zz Resume Workshop")
    app.text_input(key="events_new_location").input("Hough 120")
    app.number_input(key="events_new_estimate").set_value(80.0)
    # Form values travel with the submit, so set them and submit in one run.
    next(b for b in app.button if b.key == "events_new_submit").click()
    app.session_state[ADD] = day
    app.run()
    rows = _rows(events_db, "SELECT event_id, event_date, location, source, edited_in_app, external_key "
                            "FROM events WHERE title = 'Zz Resume Workshop'")
    assert len(rows) == 1
    event_id, stored_day, location, source, edited, key = rows[0]
    assert (stored_day, location, source, edited) == (day.isoformat(), "Hough 120", "manual", 1)
    assert key == f"{day.isoformat()}|zz resume workshop", "same key a calendar sync would use"
    assert _rows(events_db, "SELECT kind, amount FROM event_costs WHERE event_id = ?", event_id) == [("estimate", 80.0)]


def test_a_vp_adds_events_only_to_their_own_committee(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, **{ADD: date.today()})
    assert set(app.selectbox(key="events_new_committee").options) == {"Membership"}


def test_editing_an_event_moves_its_key_and_marks_it_changed(events_db, tmp_path):
    from ais_fmd.data.sqlite_backend import SqliteBackend as SQLiteBackend

    backend = SQLiteBackend(events_db)
    event = _event_id(events_db, "k-mem")
    result = backend.update_event(event, {"event_date": "2030-01-15", "title": "Zz Moved Social"}, "t@x")
    assert result.ok and result.updated == 1
    assert _rows(events_db, "SELECT external_key, edited_in_app FROM events WHERE event_id = ?", event) == [
        ("2030-01-15|zz moved social", 1),
    ]
    # A calendar sync bringing the same event back clears the flag.
    backend.sync_events([{"external_key": "2030-01-15|zz moved social", "event_date": "2030-01-15",
                          "title": "Zz Moved Social", "source": "ics"}], "sync")
    assert _rows(events_db, "SELECT edited_in_app FROM events WHERE event_id = ?", event) == [(0,)]


def test_moving_onto_a_clashing_event_is_refused(events_db):
    from ais_fmd.data.sqlite_backend import SqliteBackend as SQLiteBackend

    backend = SQLiteBackend(events_db)
    con = _rows(events_db, "SELECT event_date, title FROM events WHERE external_key = 'k-con'")[0]
    backend.update_event(_event_id(events_db, "k-con"), {"title": con[1]}, "t")  # gives it a real day|title key
    result = backend.update_event(_event_id(events_db, "k-mem"), {"event_date": con[0], "title": con[1]}, "t")
    assert result.error and "already" in result.error


def test_the_edit_form_saves_changes(events_db):
    event = _event_id(events_db, "k-mem")
    app = run_as(auth.Role.TREASURER, events_open=event)
    app.text_input(key=f"events_edit_location_{event}").input("Reitz Union")
    _submit(app, "Save changes")
    _reopen(app, event)
    assert _rows(events_db, "SELECT location, edited_in_app FROM events WHERE event_id = ?", event) == [("Reitz Union", 1)]


def test_an_event_with_purchases_cannot_be_deleted_but_one_without_can(events_db):
    from ais_fmd.data.sqlite_backend import SqliteBackend as SQLiteBackend

    backend = SQLiteBackend(events_db)
    linked = _event_id(events_db, "k-mem")
    assert "linked" in (backend.delete_event(linked, "t").error or "")
    with sqlite3.connect(events_db) as db:
        db.execute("DELETE FROM event_links WHERE event_id = ?", (linked,))
        db.execute("INSERT INTO event_costs (event_id, kind, label, amount) VALUES (?, 'estimate', 'x', 1)", (linked,))
    assert backend.delete_event(linked, "t").updated == 1
    assert _rows(events_db, "SELECT * FROM events WHERE event_id = ?", linked) == []
    assert _rows(events_db, "SELECT * FROM event_costs WHERE event_id = ?", linked) == []


def test_deleting_from_the_panel_asks_first(events_db):
    event = _event_id(events_db, "k-mem")
    with sqlite3.connect(events_db) as db:
        db.execute("DELETE FROM event_links WHERE event_id = ?", (event,))
    app = run_as(auth.Role.TREASURER, events_open=event)
    next(b for b in app.button if b.key == f"events_delete_{event}").click()
    app = _reopen(app, event)
    assert _rows(events_db, "SELECT COUNT(*) FROM events WHERE event_id = ?", event) == [(1,)]
    next(b for b in app.button if b.key == f"events_delete_yes_{event}").click()
    app.session_state[OPEN] = event
    app.run()
    assert _rows(events_db, "SELECT COUNT(*) FROM events WHERE event_id = ?", event) == [(0,)]


# --- Calendar markup for double-click and drag ----------------------------------


def test_an_editable_month_marks_days_and_movable_chips():
    day = date(2026, 9, 23)
    chips = {day: [calendar_grid.Chip("GBM", pick="1", movable=True), calendar_grid.Chip("Social", pick="2")]}
    editable = calendar_grid.month_grid(2026, 9, chips, editable=True)
    assert 'data-day="2026-09-23"' in editable and 'data-pick="add:2026-09-23"' in editable
    assert editable.count("data-drag=") == 1 and 'data-from="2026-09-23"' in editable
    read_only = calendar_grid.month_grid(2026, 9, chips)
    assert 'data-day="' not in read_only and "data-drag=" not in read_only and "add:2026" not in read_only


# --- Export ----------------------------------------------------------------------


def _events_frame() -> pd.DataFrame:
    return pd.DataFrame([
        {"event_id": 7, "event_date": "2026-09-23", "title": "GBM #4, Fall", "start_time": "18:30",
         "end_time": "20:00", "location": "Hough Hall; Room 120", "committee_id": 5, "description": "Line one\nLine two",
         "owner": None, "audience": None, "status": None, "edited_in_app": 1},
        {"event_id": 8, "event_date": "2026-09-26", "title": "Tailgate", "start_time": "TBD", "end_time": None,
         "location": None, "committee_id": None, "description": None, "owner": None, "audience": None,
         "status": "Cancelled", "edited_in_app": 0},
    ])


def test_the_ics_export_is_what_google_calendar_imports():
    text = calendar_export.to_ics(_events_frame(), {5: "Membership"},
                                  now=datetime(2026, 11, 13, tzinfo=timezone.utc)).decode()
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")
    assert "UID:event-7@ais-fmd.events" in text
    assert "DTSTART;TZID=America/New_York:20260923T183000" in text
    assert "DTEND;TZID=America/New_York:20260923T200000" in text
    assert "SUMMARY:GBM #4\\, Fall" in text and "LOCATION:Hough Hall\\; Room 120" in text
    assert "Line one\\nLine two" in text and "CATEGORIES:Membership" in text
    # No usable time: an all-day event; cancelled status carried over.
    assert "DTSTART;VALUE=DATE:20260926" in text and "DTEND;VALUE=DATE:20260927" in text
    assert "STATUS:CANCELLED" in text
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))


def test_the_export_never_carries_money():
    frame = _events_frame().assign(Spent=1234.56, Estimated=99.0)
    assert "1234" not in calendar_export.to_ics(frame).decode()


def test_changes_only_means_edited_here():
    assert list(calendar_export.changed_here(_events_frame())["event_id"]) == [7]


def test_times_from_the_sheets():
    assert calendar_export.parse_time("7:00") == (19, 0)
    assert calendar_export.parse_time("07:00") == (7, 0)
    assert calendar_export.parse_time("10:30 AM") == (10, 30)
    assert calendar_export.parse_time("12 pm") == (12, 0)
    assert calendar_export.parse_time("TBD") is None
    # As typed on the real sheets.
    assert calendar_export.parse_time("1030") == (10, 30)
    assert calendar_export.parse_time("9:30-11:00") == (9, 30)
    assert calendar_export.parse_time("4pmish") == (16, 0)


def test_long_lines_are_folded():
    frame = _events_frame().assign(description="word " * 60)
    lines = calendar_export.to_ics(frame).decode().split("\r\n")
    assert any(line.startswith(" ") for line in lines)
    assert all(len(line.encode()) <= 75 for line in lines)


def test_the_export_menu_offers_three_scopes(events_db):
    app = run_as(auth.Role.TREASURER)
    options = app.radio(key="events_export_scope").options
    assert len(options) == 3 and options[0].startswith("Changes made here") and options[2].startswith("Whole calendar")


# --- Round trip: export here, import to Google, sync back ----------------------


def _stored() -> pd.DataFrame:
    frame = _events_frame().assign(
        external_key=lambda f: [f"{d}|{t}" for d, t in zip(f["event_date"], f["title"])],
        owner=["Alex & Robin", None], audience=["General Body", None], status=[None, "Cancelled"],
        source="Schedule", term_id=None,
    )
    extra = {"event_id": 9, "event_date": "2026-09-27", "title": "Social", "start_time": "7:30", "end_time": "09:00",
             "location": None, "committee_id": None, "description": None, "owner": None, "audience": None,
             "status": None, "edited_in_app": 0, "source": "manual", "term_id": None}
    frame = pd.concat([frame, pd.DataFrame([extra])], ignore_index=True)
    from ais_fmd.domain import events as events_domain
    frame["external_key"] = [
        events_domain.Event(event_date=pd.Timestamp(d).date(), title=t).key
        for d, t in zip(frame["event_date"], frame["title"])
    ]
    return frame


def test_an_exported_file_read_back_changes_nothing():
    from ais_fmd.domain import events as events_domain, file_compare

    stored = _stored()
    data = calendar_export.to_ics(stored, {5: "Membership"})
    parsed = events_domain.parse_upload(data, "AIS Events.ics", pd.DataFrame(columns=["TermID", "Semester", "start_date", "end_date"]))
    assert len(parsed.events) == 3
    by_title = {e.title: e for e in parsed.events}
    assert by_title["GBM #4, Fall"].owner == "Alex & Robin" and by_title["GBM #4, Fall"].committee_id == 5
    assert by_title["GBM #4, Fall"].description == "Line one Line two"
    assert by_title["Social"].committee_id is None, "no committee stays none, not guessed from the title"
    comparison = file_compare.compare_events([e.as_row() for e in parsed.events], stored.to_dict("records"))
    assert comparison.differences == 0 and comparison.matching == 3


def test_a_blank_in_the_file_is_not_a_change_and_applying_keeps_it():
    from ais_fmd.domain import file_compare

    stored = _stored().to_dict("records")[0]
    incoming = {**stored, "owner": None, "location": "Reitz Union"}
    comparison = file_compare.compare_events([incoming], [stored])
    assert [change.field for change in comparison.changed[0].changes] == ["location"]
    applied = file_compare.filled_from_app(incoming, stored)
    assert applied["owner"] == "Alex & Robin" and applied["location"] == "Reitz Union"


def test_times_compare_as_a_calendar_shows_them():
    assert calendar_export.resolve_times("7:30", "09:00") == ((19, 30), (21, 0), False)
    assert calendar_export.resolve_times("18:30", None) == ((18, 30), (19, 30), True)
    assert calendar_export.resolve_times("TBD", "9") == (None, None, False)
