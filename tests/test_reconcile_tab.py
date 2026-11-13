"""
Treasury -> Reconcile (ui/reconcile_tab.py), rendered headlessly.

AppTest cannot drive `st.file_uploader`, so each test swaps it for one that
hands back a prepared file -- the rest of the page runs for real against a
private copy of the seeded database. Tick boxes are set the way Streamlit
stores a data editor's edits (`edited_rows`, by row position).
"""

from __future__ import annotations

import hashlib
import io
import sqlite3

import pandas as pd
import pytest
import streamlit
from streamlit.testing.v1 import AppTest

from ais_fmd import auth
from ais_fmd.ui import reconcile_tab

from tests.test_views import TIMEOUT, VIEWS, assert_clean, seeded_db, use_db  # noqa: F401

TREASURY = VIEWS / "Treasury.py"
PRESIDENT = auth.Role.PRESIDENT


class _Upload(io.BytesIO):
    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name
        self.size = len(data)


@pytest.fixture
def uploads(monkeypatch):
    """{uploader key: (bytes, file name)}; every other uploader stays empty."""
    files: dict[str, tuple[bytes, str]] = {}

    def fake(label, *args, key=None, **kwargs):
        if key in files:
            data, name = files[key]
            return _Upload(data, name)  # a fresh handle each rerun, read from the start
        return None

    monkeypatch.setattr(streamlit, "file_uploader", fake)
    return files


def _run(source: str, role=auth.Role.TREASURER, **session) -> AppTest:
    app = AppTest.from_file(str(TREASURY), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(email="tester@sandbox.local", role=role)
    app.session_state[reconcile_tab.TAB_KEY] = reconcile_tab.TAB_LABEL
    app.session_state[reconcile_tab.SOURCE_KEY] = source
    for key, value in session.items():
        app.session_state[key] = value
    return app.run()


def _editor_key(name: str, data: bytes, round_: int = 0) -> str:
    return f"reconcile_{name}_{hashlib.sha1(data).hexdigest()[:10]}_{round_}"


def _ticks(ticks: dict[int, bool]) -> dict:
    return {"edited_rows": {str(row): {reconcile_tab.APPLY: value} for row, value in ticks.items()},
            "added_rows": [], "deleted_rows": []}


def _apply_button(app: AppTest, name: str):
    return next(button for button in app.button if button.key == f"reconcile_apply_{name}")


def _messages(app: AppTest) -> str:
    return "\n".join(str(block.value) for block in [*app.success, *app.warning, *app.info])


def _tables(app: AppTest) -> str:
    frames = [*app.dataframe, *app.get("arrow_data_frame")]
    return "\n".join(f.value.to_csv() for f in frames if isinstance(getattr(f, "value", None), pd.DataFrame))


def test_the_tab_opens_from_another_page_on_the_chosen_source(seeded_db, use_db, uploads):
    use_db(seeded_db)
    app = _run(reconcile_tab.ROSTER)
    assert_clean(app, "Treasury opened on Reconcile")
    assert [tab.label for tab in app.tabs if tab.label == "Reconcile"]
    assert app.session_state[reconcile_tab.TAB_KEY] == "Reconcile"
    assert any(box.key == reconcile_tab.ROSTER_SEMESTER_KEY for box in app.selectbox)


# --- Bank statement ------------------------------------------------------------------

def _statement(db_path) -> tuple[bytes, list[str], list[str], int]:
    """
    Every Wells Fargo row from the 15th-latest one's day on, two left out and
    three new ones added. Also returns how many rows are in both.
    """
    with sqlite3.connect(db_path) as db:
        (since,) = db.execute(
            "SELECT transaction_date FROM transactions WHERE account = 'Wells Fargo' "
            "ORDER BY transaction_date DESC LIMIT 1 OFFSET 14"
        ).fetchone()
        rows = db.execute(
            "SELECT transactionid, transaction_date, details, amount FROM transactions "
            "WHERE account = 'Wells Fargo' AND transaction_date >= ? ORDER BY transaction_date, transactionid",
            (since,),
        ).fetchall()
    left_out = [rows[4][2], rows[8][2]]
    kept = [row for position, row in enumerate(rows) if position not in (4, 8)]
    day = pd.Timestamp(rows[6][1]).strftime("%m/%d/%Y")
    new = [f"PURCHASE AUTHORIZED ON 10/20 ZZRECON STORE {n} GAINESVILLE FL S1234 CARD 1234" for n in range(3)]
    lines = [["DATE", "DESCRIPTION", "AMOUNT", "CHECK #", "STATUS"]]
    lines += [[pd.Timestamp(r[1]).strftime("%m/%d/%Y"), r[2], f"{r[3]:.2f}", "", "Posted"] for r in kept]
    lines += [[day, details, f"-{11 + n}.25", "", "Posted"] for n, details in enumerate(new)]
    buffer = io.StringIO()
    pd.DataFrame(lines).to_csv(buffer, header=False, index=False)
    return buffer.getvalue().encode(), new, left_out, len(kept)


def _stored_details(db_path) -> set[str]:
    with sqlite3.connect(db_path) as db:
        return {row[0] for row in db.execute("SELECT details FROM transactions")}


def test_statement_differences_are_found(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    data, new, left_out, in_both = _statement(path)
    uploads["reconcile_upload_statement"] = (data, "checking.csv")
    app = _run(reconcile_tab.BANK)
    assert_clean(app, "Reconcile a bank statement")
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["In the file only"] == "3"
    assert metrics["In the app only"] == "2"
    assert metrics["In both"] == str(in_both)
    tables = _tables(app)
    assert all(details in tables for details in left_out), "the rows the file lacks are listed"
    assert not _apply_button(app, "statement").disabled


def test_statement_apply_imports_only_the_ticked_rows(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    data, new, _, _ = _statement(path)
    uploads["reconcile_upload_statement"] = (data, "checking.csv")
    app = _run(reconcile_tab.BANK, **{_editor_key("statement_new", data): _ticks({1: False})})
    assert _apply_button(app, "statement").label == "Apply 2 ticked changes"
    app = _apply_button(app, "statement").click().run()
    assert_clean(app, "After importing the ticked rows")
    stored = _stored_details(path)
    assert new[0] in stored and new[2] in stored
    assert new[1] not in stored, "an unticked row was imported"
    assert "Imported 2 transaction(s)" in _messages(app)
    assert {m.label: m.value for m in app.metric}["In the file only"] == "1"


def test_a_read_only_identity_sees_the_statement_but_cannot_apply(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    data, new, _, _ = _statement(path)
    uploads["reconcile_upload_statement"] = (data, "checking.csv")
    app = _run(reconcile_tab.BANK, role=PRESIDENT)
    assert_clean(app, "Reconcile as the President")
    assert {m.label: m.value for m in app.metric}["In the file only"] == "3"
    assert _apply_button(app, "statement").disabled
    assert not (_stored_details(path) & set(new))


# --- Membership roster -----------------------------------------------------------------

ROSTER_CSV = (
    "Name,Preferred Name,UF Email,UFID,Phone,Gender,I certify that I have completed the dues transaction\n"
    "Jane Doe,JD,jane@ufl.edu,1111,352-555-0100,F,Yes\n"
    "Sam Lee,,sam@ufl.edu,,352-555-0101,M,No\n"
    "New Person,,new@ufl.edu,3333,352-555-0102,F,No\n"
    "Second New,,second@ufl.edu,4444,352-555-0103,M,No\n"
).encode()


def _seed_roster(path) -> None:
    from ais_fmd.data.sqlite_backend import SqliteBackend

    SqliteBackend().replace_members("FA26", [
        {"full_name": "Jane Doe", "match_key": "doe jane", "email": "jane@ufl.edu", "ufid": "1111",
         "committee_id": 5},
        {"full_name": "Sam Lee", "match_key": "lee sam", "email": "sam@ufl.edu"},
        {"full_name": "Old Member", "match_key": "member old", "email": "old@ufl.edu"},
    ], "seed.csv", "test")


def _roster(path) -> dict[str, dict]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {row["full_name"]: dict(row) for row in db.execute("SELECT * FROM members WHERE term_id = 'FA26'")}


def test_roster_differences_are_found(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_roster(path)
    uploads["reconcile_upload_roster"] = (ROSTER_CSV, "members.csv")
    app = _run(reconcile_tab.ROSTER, **{reconcile_tab.ROSTER_SEMESTER_KEY: "Fall 2026"})
    assert_clean(app, "Reconcile a roster")
    metrics = {metric.label: metric.value for metric in app.metric}
    assert (metrics["In the file only"], metrics["Different"], metrics["In the app only"], metrics["Same"]) == (
        "2", "1", "1", "1")
    tables = _tables(app)
    assert "Preferred name: — → JD" in tables and "Old Member" in tables
    assert "352-555" not in tables, "phone numbers are never read"


def test_roster_apply_saves_only_the_ticked_changes(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_roster(path)
    uploads["reconcile_upload_roster"] = (ROSTER_CSV, "members.csv")
    session = {
        reconcile_tab.ROSTER_SEMESTER_KEY: "Fall 2026",
        _editor_key("roster_new", ROSTER_CSV): _ticks({1: False}),     # Second New: not added
        _editor_key("roster_changed", ROSTER_CSV): _ticks({0: True}),  # Jane: take the file's values
        # roster_missing left alone: Old Member stays
    }
    app = _run(reconcile_tab.ROSTER, **session)
    app = _apply_button(app, "roster").click().run()
    assert_clean(app, "After saving the roster")
    roster = _roster(path)
    assert set(roster) == {"Jane Doe", "Sam Lee", "Old Member", "New Person"}
    assert roster["Jane Doe"]["preferred_name"] == "JD" and roster["Jane Doe"]["claims_paid"] == 1
    assert roster["Jane Doe"]["committee_id"] == 5, "a field the file does not carry is kept"
    assert "Applied 2 change(s)" in _messages(app)
    with sqlite3.connect(path) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(members)")]
    assert not {"phone", "gender"} & set(columns)


def test_roster_removal_only_when_ticked(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_roster(path)
    uploads["reconcile_upload_roster"] = (ROSTER_CSV, "members.csv")
    session = {
        reconcile_tab.ROSTER_SEMESTER_KEY: "Fall 2026",
        _editor_key("roster_new", ROSTER_CSV): _ticks({0: False, 1: False}),
        _editor_key("roster_missing", ROSTER_CSV): _ticks({0: True}),
    }
    app = _run(reconcile_tab.ROSTER, **session)
    _apply_button(app, "roster").click().run()
    assert set(_roster(path)) == {"Jane Doe", "Sam Lee"}


def test_a_read_only_identity_cannot_save_the_roster(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_roster(path)
    uploads["reconcile_upload_roster"] = (ROSTER_CSV, "members.csv")
    app = _run(reconcile_tab.ROSTER, role=PRESIDENT, **{reconcile_tab.ROSTER_SEMESTER_KEY: "Fall 2026"})
    assert_clean(app, "Roster reconcile as the President")
    assert _apply_button(app, "roster").disabled
    assert set(_roster(path)) == {"Jane Doe", "Sam Lee", "Old Member"}


# --- Event schedule ---------------------------------------------------------------------

ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20260910T180000Z\r\nDTEND:20260910T190000Z\r\nSUMMARY:Zz GBM One\r\n"
    "LOCATION:Hough 120\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20260912T200000Z\r\nDTEND:20260912T220000Z\r\nSUMMARY:Zz Social\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20260920T180000Z\r\nSUMMARY:Zz Kickoff\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nDTSTART:20260925T180000Z\r\nSUMMARY:Zz Second New\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()


def _seed_events(path) -> None:
    from ais_fmd.domain.events import infer_committee

    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM event_links")
        db.execute("DELETE FROM events")
        rows = [
            ("2026-09-10|zz gbm one", "2026-09-10", "Zz GBM One", "18:00", "19:00", "Hough 120", "calendar"),
            ("2026-09-12|zz social", "2026-09-12", "Zz Social", "19:00", "22:00", None, "calendar"),
            ("2026-09-14|zz dropped", "2026-09-14", "Zz Dropped", None, None, None, "calendar"),
            ("2026-09-15|zz linked", "2026-09-15", "Zz Linked", None, None, None, "calendar"),
            ("2026-09-16|zz typed in app", "2026-09-16", "Zz Typed In App", None, None, None, "manual"),
        ]
        for key, day, title, start, end, location, source in rows:
            db.execute(
                "INSERT INTO events (external_key, term_id, event_date, title, start_time, end_time, location, "
                "committee_id, source, synced_by) VALUES (?, 'FA26', ?, ?, ?, ?, ?, ?, ?, 'test')",
                (key, day, title, start, end, location, infer_committee(title, None, None), source),
            )
        linked = db.execute("SELECT event_id FROM events WHERE title = 'Zz Linked'").fetchone()[0]
        transaction = db.execute("SELECT MIN(transactionid) FROM transactions").fetchone()[0]
        db.execute("INSERT INTO event_links (transaction_id, event_id, linked_by) VALUES (?, ?, 'test')",
                   (transaction, linked))


def _events(path) -> dict[str, dict]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {row["title"]: dict(row) for row in db.execute("SELECT * FROM events")}


def test_event_differences_are_found(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_events(path)
    uploads["reconcile_upload_events"] = (ICS, "ais.ics")
    app = _run(reconcile_tab.SCHEDULE)
    assert_clean(app, "Reconcile an event schedule")
    metrics = {metric.label: metric.value for metric in app.metric}
    assert (metrics["In the file only"], metrics["Different"], metrics["In the app only"], metrics["Same"]) == (
        "2", "1", "2", "1")
    tables = _tables(app)
    assert "Start: 19:00 → 20:00" in tables
    assert "Zz Typed In App" in tables, "listed as kept, not as missing"
    assert any("never removed here" in expander.label for expander in app.expander)


def test_event_apply_does_only_the_ticked_rows_and_refuses_linked_removals(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_events(path)
    uploads["reconcile_upload_events"] = (ICS, "ais.ics")
    session = {
        _editor_key("events_new", ICS): _ticks({1: False}),          # Second New: not added
        _editor_key("events_changed", ICS): _ticks({0: True}),       # Social: take the file's time
        _editor_key("events_missing", ICS): _ticks({0: True, 1: True}),  # Dropped, and Linked (refused)
    }
    app = _run(reconcile_tab.SCHEDULE, **session)
    app = _apply_button(app, "events").click().run()
    assert_clean(app, "After applying the events")
    events = _events(path)
    assert "Zz Kickoff" in events and "Zz Second New" not in events
    assert events["Zz Social"]["start_time"] == "20:00"
    assert "Zz Dropped" not in events
    assert "Zz Linked" in events, "an event with linked purchases is never removed"
    assert "Zz Typed In App" in events
    messages = _messages(app)
    assert "Zz Linked was not removed" in messages and "linked" in messages
    assert "Removed 1 event(s)" in messages


def test_a_read_only_identity_cannot_change_events(seeded_db, use_db, uploads):
    path = use_db(seeded_db)
    _seed_events(path)
    uploads["reconcile_upload_events"] = (ICS, "ais.ics")
    app = _run(reconcile_tab.SCHEDULE, role=PRESIDENT)
    assert_clean(app, "Events reconcile as the President")
    assert _apply_button(app, "events").disabled
    assert "Zz Kickoff" not in _events(path)


# --- The links that used to open an upload --------------------------------------------

def test_dues_upload_roster_opens_treasury_reconcile_on_the_roster(seeded_db, use_db, uploads):
    """Dues' "Upload roster" (no roster stored) lands on Treasury -> Reconcile -> Membership roster."""
    use_db(seeded_db)
    app = AppTest.from_file(str(VIEWS / "Dues.py"), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(email="tester@sandbox.local", role=auth.Role.TREASURER)
    app.run()
    assert_clean(app, "Dues")
    button = next((b for b in app.button if b.key == "dues_open_roster"), None)
    if button is None or button.label != "Upload roster":
        pytest.skip("the seeded current term already has a roster")
    semester = app.selectbox(key="dues_semester").value
    button.click().run()
    assert app.session_state[reconcile_tab.TAB_KEY] == "Reconcile"
    assert app.session_state[reconcile_tab.SOURCE_KEY] == reconcile_tab.ROSTER
    assert app.session_state[reconcile_tab.ROSTER_SEMESTER_KEY] == semester


def test_no_hub_offers_the_old_import_view():
    from tests.test_assistant import _hub_tabs

    assert "Import" not in _hub_tabs("TransactionsHub.py")
    assert not (VIEWS / "ImportStatement.py").exists()
