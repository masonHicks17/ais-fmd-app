"""
Spending by kind (My Committee) and event planning (My Plan), 2026-11-09.

Pure tests for the grouping rules, storage tests for planned events, and
headless page tests for adding ideas and for removal asking before it deletes.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ais_fmd import auth
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import event_ideas as ei
from ais_fmd.domain import spend_kinds

from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as

OFFICER = auth.Role.OFFICER
MEMBERSHIP, CONSULTING = 5, 7


# --- What kind of spending ------------------------------------------------------

@pytest.mark.parametrize("details, kind", [
    ("PURCHASE AUTHORIZED ON 09/02 PIESANOS STONE FIRED GAINESVILLE FL S540669435204 CARD 4831", "Food & catering"),
    ("PURCHASE AUTHORIZED ON 10/28 SALTY DOG SALOON GAINESVILLE FL S1 CARD 1234", "Bar tabs"),
    ("PURCHASE AUTHORIZED ON 10/28 MACDINTONS PUB GAINESVILLE FL S1 CARD 1234", "Bar tabs"),
    ("PURCHASE AUTHORIZED ON 10/28 TOTAL WINE AND MORE GAINESVILLE FL S1 CARD 1234", "Drinks (liquor stores)"),
    ("PURCHASE AUTHORIZED ON 10/28 STAPLES 00123 GAINESVILLE FL S1 CARD 1234", "Supplies & printing"),
    ("PURCHASE AUTHORIZED ON 10/28 UF REITZ UNION GAINESVILLE FL S1 CARD 1234", "Venues & activities"),
    ("PURCHASE AUTHORIZED ON 10/28 ZOOM.US 888-799-9666 CA S1 CARD 1234", "Software & ads"),
    ("3000132 | Pizza for GBM | Hana Kim | UF AIS", spend_kinds.PAID_BACK),
    ("PURCHASE AUTHORIZED ON 10/28 XYZZY THINGS GAINESVILLE FL S1 CARD 1234", spend_kinds.OTHER),
])
def test_charges_are_grouped_by_what_was_bought(details, kind):
    assert spend_kinds.spend_kind(details) == kind


@pytest.mark.parametrize("details, store", [
    ("PURCHASE AUTHORIZED ON 08/18 PUBLIX SUPER MAR 1560 GAINESVILLE FL S386230123 CARD 4417", "Publix"),
    ("PURCHASE AUTHORIZED ON 08/18 COSTCO WHSE #0123 GAINESVILLE FL S1 CARD 4417", "Costco"),
    ("PURCHASE AUTHORIZED ON 08/18 WALMART SUPERCENTER GAINESVILLE FL S1 CARD 4417", "Walmart"),
])
def test_big_stores_keep_their_name_because_the_receipt_could_be_anything(details, store):
    assert spend_kinds.spend_kind(details) == store


def test_pub_matches_a_pub_but_not_publix():
    assert spend_kinds.spend_kind("PURCHASE AUTHORIZED ON 01/01 MACDINTONS PUB") == "Bar tabs"
    assert spend_kinds.spend_kind("PURCHASE AUTHORIZED ON 01/01 PUBLIX SUPER MAR") == "Publix"


def test_kind_split_counts_money_out_only_largest_first():
    frame = pd.DataFrame({
        "details": ["PURCHASE AUTHORIZED ON 01/01 CHIPOTLE", "PURCHASE AUTHORIZED ON 01/02 CHIPOTLE",
                    "PURCHASE AUTHORIZED ON 01/03 THE GROVE", "ZELLE FROM JANE DOE"],
        "amount": [-30.0, -20.0, -100.0, 50.0],
    })
    split = spend_kinds.kind_split(frame)
    assert split["Kind"].tolist() == ["Bar tabs", "Food & catering"]
    assert split["Amount"].tolist() == [100.0, 50.0] and split["Charges"].tolist() == [1, 2]
    assert split["Share"].sum() == pytest.approx(100.0)


# --- Event kinds and ideas -------------------------------------------------------

@pytest.mark.parametrize("title, kind, detail", [
    ("UF/kentucky Watch Party", "Watch party", "kentucky"),
    ("Florida/Oklahoma Tailgate", "Tailgate", "Florida/Oklahoma"),
    ("Tailgate 2 - USF", "Tailgate", "USF"),
    ("GBM # 3 - KPMG", "GBM", "KPMG"),
    ("GBM #7", "GBM", ""),
    ("Passport: Pumpkins", "Passport outing", "Pumpkins"),
    ("Workshop #2: Diana Saiz", "Workshop", "Diana Saiz"),
    ("Coffee & Pastries", "Coffee & pastries", ""),
    ("Opus Tab", "Bar tab", "Opus"),
    ("Semi-formal", "Semi-formal", ""),
    ("VP Apps Close", None, ""),
    ("Easter", None, ""),
])
def test_an_event_title_reduces_to_its_kind_and_what_made_it_different(title, kind, detail):
    assert ei.event_kind(title) == (kind, detail)


def _events(*rows):
    return pd.DataFrame([
        {"event_id": i, "event_date": d, "title": t, "attendance": a, "committee_id": MEMBERSHIP}
        for i, (d, t, a) in enumerate(rows, start=1)
    ])


def test_ideas_summarise_each_kind_with_cost_attendance_and_last_held():
    events = _events(("2026-09-05", "UF/Texas Watch Party", 30), ("2026-10-10", "UF/Auburn Watch Party", 50),
                     ("2026-10-01", "GBM # 4", None), ("2026-12-01", "UF/FSU Watch Party", None))  # last: future
    spending = pd.DataFrame({"transactionid": [10, 11], "amount": [-120.0, -80.0]})
    links = pd.DataFrame({"transaction_id": [10, 11], "event_id": [1, 2]})
    table = ei.ideas(events, spending, links, today=date(2026, 11, 9)).set_index("Kind")
    party = table.loc["Watch party"]
    assert party["Times"] == 2 and party["Last held"] == date(2026, 10, 10)
    assert party["Average attendance"] == 40 and party["Last attendance"] == 50
    assert party["Average cost"] == pytest.approx(100.0)
    assert party["Cost per person"] == pytest.approx((120 / 30 + 80 / 50) / 2, abs=0.01)
    assert party["Examples"] == "Auburn, Texas", "most recent game first; the future FSU game isn't past yet"
    assert pd.isna(table.loc["GBM"]["Average cost"])


def test_ideas_never_count_money_the_caller_did_not_pass_in():
    events = _events(("2026-09-05", "UF/Texas Watch Party", 30))
    links = pd.DataFrame({"transaction_id": [99], "event_id": [1]})  # someone else's purchase
    table = ei.ideas(events, pd.DataFrame({"transactionid": [10], "amount": [-5.0]}), links, today=date(2026, 11, 9))
    assert pd.isna(table.iloc[0]["Average cost"])


def test_a_game_day_idea_is_planned_as_the_kind_with_past_games_as_a_hint():
    idea = pd.DataFrame([{"Kind": "Tailgate", "Examples": "FSU, Tennessee", "Last title": "FSU Tailgate",
                          "Average cost": 212.3, "Average attendance": 31}])
    [row] = ei.plan_rows_from_ideas(idea)
    assert row["title"] == "Tailgate" and row["event_date"] is None
    assert row["estimated_cost"] == 215.0 and row["expected_attendance"] == 31
    assert "Which game?" in row["note"] and "FSU" in row["note"]


def test_planned_events_are_checked_before_saving():
    window = (date(2027, 1, 5), date(2027, 5, 5))
    frame = pd.DataFrame([
        {"title": " GBM ", "event_date": date(2027, 1, 20), "estimated_cost": 150, "expected_attendance": 60.0},
        {"title": "Tailgate", "event_date": date(2026, 9, 1), "estimated_cost": 100},
        {"title": "", "estimated_cost": 40},
        {"title": "Bowling", "estimated_cost": -1},
    ])
    events, problems = ei.clean_planned_events(frame, window, "Spring 2027")
    assert [e["title"] for e in events] == ["GBM"] and events[0]["event_date"] == "2027-01-20"
    assert any("isn't in Spring 2027" in p for p in problems)
    assert "Every event needs a name." in problems
    assert any("between $0" in p for p in problems)


def test_an_unset_semester_gets_rough_dates():
    assert ei.guess_window("Spring 2027") == (date(2027, 1, 5), date(2027, 5, 5))
    assert ei.guess_window("nonsense") is None


# --- Storage ---------------------------------------------------------------------

def test_a_vp_can_only_save_their_own_committees_events(tmp_path):
    backend = SqliteBackend(tmp_path / "plan.db")
    event = {"title": "GBM", "estimated_cost": 150.0, "event_date": "2027-01-20"}
    assert backend.replace_planned_events(MEMBERSHIP, "Spring 2027", [event], (MEMBERSHIP,), "vp").ok
    refused = backend.replace_planned_events(CONSULTING, "Spring 2027", [event], (MEMBERSHIP,), "vp")
    assert refused.error and backend.fetch_planned_events((CONSULTING,)).empty
    assert backend.fetch_planned_events((MEMBERSHIP,), "Spring 2027")["title"].tolist() == ["GBM"]


# --- The pages ---------------------------------------------------------------------

def _plan(app):
    return SqliteBackend().fetch_planned_events((MEMBERSHIP,))


def test_my_committee_shows_spending_by_kind_for_the_vp(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, MEMBERSHIP)
    assert_clean(app, "My Committee as the Membership VP")
    spec = " ".join(chart.proto.spec for chart in app.get("plotly_chart"))
    assert "Food & catering" in spec and "Bar tabs" in spec


def test_adding_an_idea_saves_it_to_the_plan(seeded_db, use_db):
    use_db(seeded_db)
    from ais_fmd.domain.events import Event

    SqliteBackend().sync_events([
        Event(event_date=date(2026, 9, 5), title="UF/Texas Watch Party", term_id="FA26", source="test",
              committee_id=MEMBERSHIP).as_row(),
        Event(event_date=date(2026, 10, 10), title="UF/Auburn Watch Party", term_id="FA26", source="test",
              committee_id=MEMBERSHIP).as_row(),
    ], "test")
    app = run_as(VIEWS / "MyPlan.py", OFFICER, MEMBERSHIP)
    assert_clean(app, "My Plan as the Membership VP")
    assert "Ideas from past events" in " ".join(str(m.value) for m in app.markdown)
    # Tick the first idea (most often held) and add it.
    key = "events_now_ideas_0"
    app.session_state[key] = {"edited_rows": {0: {"add": True}}, "added_rows": [], "deleted_rows": []}
    app = app.run()
    next(b for b in app.button if b.label.startswith("Add 1")).click().run()
    assert_clean(app, "after adding an idea")
    assert len(_plan(app)) == 1


def test_removing_a_planned_event_asks_first_and_keep_changes_nothing(seeded_db, use_db):
    path = use_db(seeded_db)
    SqliteBackend().replace_planned_events(
        MEMBERSHIP, "Fall 2026", [{"title": "Tailgate", "estimated_cost": 200.0}], (MEMBERSHIP,), "vp")
    app = run_as(VIEWS / "MyPlan.py", OFFICER, MEMBERSHIP)
    app.session_state["events_now_editor_0"] = {"edited_rows": {0: {"remove": True}}, "added_rows": [], "deleted_rows": []}
    app = app.run()
    next(b for b in app.button if b.label == "Save events").click().run()
    assert_clean(app, "after asking to remove")
    assert any("Remove 1 planned event: Tailgate" in str(w.value) for w in app.warning)
    assert len(_plan(app)) == 1, "nothing removed until confirmed"

    next(b for b in app.button if b.label == "Keep them").click().run()
    assert len(_plan(app)) == 1

    app.session_state["events_now_editor_1"] = {"edited_rows": {0: {"remove": True}}, "added_rows": [], "deleted_rows": []}
    app = app.run()
    next(b for b in app.button if b.label == "Save events").click().run()
    next(b for b in app.button if b.label == "Remove them").click().run()
    assert_clean(app, "after confirming")
    assert _plan(app).empty


# --- Planned vs reported: projected spending and room for extras --------------------

def _planned(*rows):
    return pd.DataFrame(rows, columns=["event_date", "title", "estimated_cost"])


def test_projection_adds_upcoming_planned_events_to_what_the_bank_reported():
    planned = _planned(
        ("2026-11-20", "GBM", 160.0),        # ahead
        (None, "Bar tab", 250.0),            # undated counts as ahead
        ("2026-10-01", "Tailgate", 300.0),   # passed: its real cost is already in spent
    )
    out = ei.projection(2000.0, 900.0, planned, today=date(2026, 11, 9))
    assert out["ahead"] == 410.0 and out["ahead_count"] == 2 and out["passed_count"] == 1
    assert out["projected"] == 1310.0 and out["room"] == 690.0 and out["status"] == "room"


@pytest.mark.parametrize("spent, status", [(1850.0, "tight"), (2100.0, "over")])
def test_projection_says_when_the_room_is_thin_or_gone(spent, status):
    out = ei.projection(2000.0, spent, _planned(("2026-12-01", "Social", 100.0)), today=date(2026, 11, 9))
    assert out["status"] == status


def test_projection_without_a_budget_or_plan():
    assert ei.projection(None, 50.0, pd.DataFrame(), today=date(2026, 11, 9))["status"] == "no budget"
    empty = ei.projection(1000.0, 200.0, pd.DataFrame(), today=date(2026, 11, 9))
    assert empty["ahead"] == 0 and empty["room"] == 800.0


def test_my_plan_shows_spent_planned_and_room_for_extras(seeded_db, use_db):
    use_db(seeded_db)
    SqliteBackend().replace_planned_events(
        MEMBERSHIP, "Fall 2026", [{"title": "Big Social", "estimated_cost": 400.0, "event_date": "2099-12-01"}],
        (MEMBERSHIP,), "vp")
    app = run_as(VIEWS / "MyPlan.py", OFFICER, MEMBERSHIP)
    assert_clean(app, "My Plan projection")
    labels = [m.label for m in app.metric]
    assert {"Spent so far", "Planned events ahead"} <= set(labels)
    assert "Room for extras" in labels or "Over the budget" in labels
    assert any(m.value == "$400" for m in app.metric if m.label == "Planned events ahead")
    text = " ".join(str(m.value) for m in app.markdown)
    assert "for extras" in text or "over" in text
