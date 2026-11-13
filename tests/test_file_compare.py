"""Comparing an uploaded file with what the app holds (domain/file_compare.py)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from ais_fmd.domain import file_compare as fc
from ais_fmd.domain.dedupe import assign_natural_keys
from ais_fmd.domain.events import Event
from ais_fmd.domain.roster import parse_roster

WF = "Wells Fargo"


def _txn(day: str, amount: float, details: str, account: str = WF, tid: int | None = None) -> dict:
    row = {"transaction_date": day, "amount": amount, "details": details, "account": account}
    if tid is not None:
        row["transactionid"] = tid
    return row


# --- Bank statement --------------------------------------------------------------

def test_statement_new_rows_and_matching_rows():
    stored = [_txn("2026-09-01", -10.0, "PUBLIX", tid=1), _txn("2026-09-02", -20.0, "CHIPOTLE", tid=2)]
    file_rows = [_txn("2026-09-01", -10.0, "publix  "), _txn("2026-09-03", -30.0, "PANERA")]
    comparison = fc.compare_statement(file_rows, stored, WF)
    assert [d.file_row["details"] for d in comparison.new] == ["PANERA"]
    assert comparison.matching == 1
    assert comparison.new[0].key == comparison.new[0].file_row["natural_key"]
    assert comparison.changed == []


def test_statement_missing_is_limited_to_its_dates_and_account():
    stored = [
        _txn("2026-09-02", -20.0, "CHIPOTLE", tid=1),          # inside, not in the file: missing
        _txn("2026-08-01", -99.0, "BEFORE THE FILE", tid=2),   # outside the dates
        _txn("2026-09-02", -5.0, "VENMO ROW", account="Venmo", tid=3),  # other account
        _txn("2026-09-01", -10.0, "PUBLIX", account="Wells", tid=4),    # legacy spelling, in file
    ]
    file_rows = [_txn("2026-09-01", -10.0, "PUBLIX"), _txn("2026-09-05", -1.0, "FEE")]
    comparison = fc.compare_statement(file_rows, stored, WF)
    assert [d.app_row["transactionid"] for d in comparison.missing] == [1]
    assert "Sep 01, 2026" in comparison.scope and "Sep 05, 2026" in comparison.scope


def test_statement_missing_counts_repeats_as_a_multiset():
    """Two identical coffees stored, one in the file: one is missing."""
    stored = [_txn("2026-09-01", -4.5, "COFFEE", tid=1), _txn("2026-09-01", -4.5, "COFFEE", tid=2)]
    comparison = fc.compare_statement([_txn("2026-09-01", -4.5, "COFFEE")], stored, WF)
    assert comparison.new == [] and comparison.matching == 1
    assert [d.app_row["transactionid"] for d in comparison.missing] == [2]


def test_statement_reads_stored_timestamps():
    stored = [_txn(pd.Timestamp("2026-09-01"), -10.0, "PUBLIX", tid=1)]
    comparison = fc.compare_statement([_txn("2026-09-01", -10.0, "PUBLIX")], stored, WF)
    assert comparison.differences == 0 and comparison.matching == 1


def test_statement_new_keys_are_the_import_keys():
    file_rows = [_txn("2026-09-01", -4.5, "COFFEE"), _txn("2026-09-01", -4.5, "COFFEE")]
    comparison = fc.compare_statement(file_rows, [], WF)
    assert [d.key for d in comparison.new] == [r["natural_key"] for r in assign_natural_keys(file_rows)]


# --- Membership roster ------------------------------------------------------------

def _stored(name, key, **extra) -> dict:
    return {"full_name": name, "match_key": key, "alt_keys": extra.pop("alt_keys", None),
            "preferred_name": extra.pop("preferred_name", None), "claims_paid": extra.pop("claims_paid", 0),
            "email": extra.pop("email", None), "ufid": extra.pop("ufid", None),
            "committee_id": extra.pop("committee_id", None), "notes": extra.pop("notes", None)}


STORED_ROSTER = [
    _stored("Jane Doe", "doe jane", email="jane@ufl.edu", ufid="1111", committee_id=5, alt_keys="doe jd"),
    _stored("Sam Lee", "lee sam", email="sam@ufl.edu"),
    _stored("Old Member", "member old", email="old@ufl.edu"),
]


def _file(rows: list[dict]) -> tuple[list, pd.DataFrame]:
    frame = pd.DataFrame(rows)
    return parse_roster(frame).members, frame


def test_roster_groups_new_changed_and_missing():
    members, frame = _file([
        {"Name": "Jane Doe", "UF Email": "jane@ufl.edu", "UFID": "1111", "Preferred Name": "JD",
         "I certify that I have completed the dues transaction": "Yes"},
        {"Name": "Sam Lee", "UF Email": "SAM@ufl.edu", "UFID": "", "Preferred Name": "",
         "I certify that I have completed the dues transaction": "No"},
        {"Name": "New Person", "UF Email": "new@ufl.edu", "UFID": "3333", "Preferred Name": "",
         "I certify that I have completed the dues transaction": "No"},
    ])
    comparison = fc.compare_roster(members, STORED_ROSTER, "Fall 2026",
                                   compare_claims=fc.roster_has_claim_column(frame.columns))
    assert [d.file_row["full_name"] for d in comparison.new] == ["New Person"]
    assert [d.app_row["full_name"] for d in comparison.missing] == ["Old Member"]
    assert comparison.matching == 1, "Sam differs only in email case"
    (jane,) = comparison.changed
    assert {c.field for c in jane.changes} == {"preferred_name", "claims_paid"}
    assert "Preferred name: — → JD" in jane.change_text


def test_roster_matches_on_ufid_and_reports_a_name_change():
    members, _ = _file([{"Name": "Jane Smith", "UFID": "1111"}])
    comparison = fc.compare_roster(members, STORED_ROSTER, compare_claims=False)
    (change,) = comparison.changed
    assert [c.field for c in change.changes] == ["full_name"]
    assert change.changes[0].old == "Jane Doe" and change.changes[0].new == "Jane Smith"


def test_a_file_without_the_paid_question_does_not_take_claims_back():
    stored = [_stored("Jane Doe", "doe jane", claims_paid=1)]
    members, frame = _file([{"Name": "Jane Doe"}])
    assert not fc.roster_has_claim_column(frame.columns)
    comparison = fc.compare_roster(members, stored, compare_claims=False)
    assert comparison.differences == 0


def test_merged_roster_applies_only_what_is_ticked_and_keeps_other_fields():
    members, frame = _file([
        {"Name": "Jane Doe", "UF Email": "jane.new@ufl.edu", "UFID": "1111"},
        {"Name": "New Person", "UF Email": "new@ufl.edu"},
        {"Name": "Another New", "UF Email": "another@ufl.edu"},
    ])
    comparison = fc.compare_roster(members, STORED_ROSTER, compare_claims=False)
    jane = comparison.changed[0].key
    new_person = next(d.key for d in comparison.new if d.file_row["full_name"] == "New Person")
    old = next(d.key for d in comparison.missing if d.app_row["full_name"] == "Old Member")
    sam = next(d.key for d in comparison.missing if d.app_row["full_name"] == "Sam Lee")

    merged = fc.merged_roster(STORED_ROSTER, comparison, {jane, new_person, old})
    names = [row["full_name"] for row in merged]
    assert names == ["Jane Doe", "Sam Lee", "New Person"], "Sam not ticked: kept; Another New not ticked"
    assert sam not in {jane, new_person, old}
    jane_row = merged[0]
    assert jane_row["email"] == "jane.new@ufl.edu"
    assert jane_row["committee_id"] == 5 and jane_row["alt_keys"] == ["doe jd"], "kept from the app"


def test_merged_roster_with_nothing_ticked_is_the_stored_roster():
    members, _ = _file([{"Name": "Brand New"}])
    comparison = fc.compare_roster(members, STORED_ROSTER, compare_claims=False)
    merged = fc.merged_roster(STORED_ROSTER, comparison, set())
    assert [row["match_key"] for row in merged] == [row["match_key"] for row in STORED_ROSTER]


def test_roster_rows_carry_only_the_allowed_fields():
    """Privacy rule: name, preferred name, UFID, UF email and the paid answer. Nothing else."""
    members, _ = _file([{"Name": "New Person", "Phone": "352-555-0100", "Gender": "x",
                         "What do you want from AIS?": "jobs", "UF Email": "n@ufl.edu"}])
    comparison = fc.compare_roster(members, [], compare_claims=False)
    row = comparison.new[0].file_row
    assert set(row) == {"full_name", "match_key", "alt_keys", "preferred_name", "claims_paid", "email", "ufid"}
    assert "352-555-0100" not in str(fc.merged_roster([], comparison, {comparison.new[0].key}))


# --- Event schedule ----------------------------------------------------------------

def _event(day: date, title: str, **extra) -> dict:
    return Event(event_date=day, title=title, term_id="FA26", source="Spreadsheet: s.xlsx", **extra).as_row()


def _stored_event(event_id: int, day: str, title: str, source: str = "Spreadsheet: s.xlsx", **extra) -> dict:
    row = Event(event_date=date.fromisoformat(day), title=title).as_row()
    row.update({"event_id": event_id, "source": source, "edited_in_app": 0, **extra})
    return row


def test_events_new_changed_missing_and_manual_kept():
    stored = [
        _stored_event(1, "2026-09-10", "GBM 1", location="Hough 120"),
        _stored_event(2, "2026-09-12", "Social", start_time="19:00"),
        _stored_event(3, "2026-09-14", "Dropped Event"),
        _stored_event(4, "2026-09-15", "Typed In App", source="manual"),
        _stored_event(5, "2026-12-01", "Later Event"),    # outside the file's span
    ]
    file_rows = [
        _event(date(2026, 9, 10), "GBM 1", location="Hough 120"),
        _event(date(2026, 9, 12), "Social", start_time="20:00", committee_id=5),
        _event(date(2026, 9, 20), "Kickoff"),
    ]
    comparison = fc.compare_events(file_rows, stored)
    assert [d.file_row["title"] for d in comparison.new] == ["Kickoff"]
    assert comparison.matching == 1
    (social,) = comparison.changed
    assert {c.field for c in social.changes} == {"start_time", "committee_id"}
    assert "Start: 19:00 → 20:00" in social.change_text
    assert [d.app_row["event_id"] for d in comparison.missing] == [3]
    assert [d.app_row["event_id"] for d in comparison.kept] == [4]


def test_events_blank_and_nan_are_the_same():
    stored = [_stored_event(1, "2026-09-10", "GBM 1", location=float("nan"), committee_id=float("nan"))]
    comparison = fc.compare_events([_event(date(2026, 9, 10), "GBM 1")], stored)
    assert comparison.differences == 0 and comparison.matching == 1
