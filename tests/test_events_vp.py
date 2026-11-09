"""
Events as a VP: the club calendar, but money only for their own committee.

Renders the real page headlessly. A Membership event and a Consulting event sit
in the current term, each with a linked purchase, plus an unlinked Consulting
purchase the matcher would suggest. The amounts are distinctive so a leak shows
up anywhere on the page -- a metric, a calendar chip, a table, a caption.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from ais_fmd import auth

from tests.test_views import ROOT, TIMEOUT, assert_clean, seeded_db, use_db  # noqa: F401

EVENTS_PAGE = ROOT / "ais_fmd" / "views" / "Events.py"
MEMBERSHIP, CONSULTING = 5, 7
TODAY = date.today()

# Distinctive amounts: what each committee spent.
MINE = "1234.56"        # Membership, linked to the Membership event
THEIRS = "9876.54"      # Consulting, linked to the Consulting event
SUGGESTED = "4567.89"   # Consulting, unlinked, matches the Consulting event


def _day(offset: int) -> str:
    return (pd.Timestamp(TODAY) - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")


@pytest.fixture
def events_db(seeded_db, use_db):
    path = use_db(seeded_db)
    with sqlite3.connect(path) as db:
        term = db.execute(
            "SELECT TermID FROM terms WHERE start_date <= ? AND end_date >= ?", (TODAY.isoformat(),) * 2
        ).fetchone()[0]
        db.execute("DELETE FROM event_links")
        db.execute("DELETE FROM events")
        events = [
            ("k-mem", term, _day(3), "Zzmembership Social", MEMBERSHIP),
            ("k-con", term, _day(2), "Zzconsulting Barcade Night", CONSULTING),
        ]
        db.executemany(
            "INSERT INTO events (external_key, term_id, event_date, title, committee_id, source, synced_by) "
            "VALUES (?, ?, ?, ?, ?, 'test', 'test')",
            events,
        )
        ids = dict(db.execute("SELECT external_key, event_id FROM events"))
        purchases = [
            (_day(3), f"-{MINE}", "PURCHASE AUTHORIZED ON 01/01 ZZMEMSHOP", MEMBERSHIP, "k-mem"),
            (_day(2), f"-{THEIRS}", "PURCHASE AUTHORIZED ON 01/01 ZZCONSHOP", CONSULTING, "k-con"),
            (_day(2), f"-{SUGGESTED}", f"PURCHASE AUTHORIZED ON {pd.Timestamp(_day(2)):%m/%d} BARCADE", CONSULTING, None),
        ]
        for posted, amount, details, committee, event_key in purchases:
            cursor = db.execute(
                "INSERT INTO transactions (transaction_date, details, amount, account, budget_category, natural_key) "
                "VALUES (?, ?, ?, 'Wells Fargo', ?, ?)",
                (posted, details, float(amount), committee, f"test-{details}-{amount}"),
            )
            if event_key:
                db.execute(
                    "INSERT INTO event_links (transaction_id, event_id, linked_by) VALUES (?, ?, 'test')",
                    (cursor.lastrowid, ids[event_key]),
                )
    return path


def run_as(role, committee_id=None, **session) -> AppTest:
    app = AppTest.from_file(str(EVENTS_PAGE), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(email="x@sandbox.local", role=role, committee_id=committee_id)
    for key, value in session.items():
        app.session_state[key] = value
    return app.run()


def page_text(app: AppTest) -> str:
    """Everything rendered: markdown and captions, metric values, and table cells."""
    parts = [str(element.value) for element in [*app.markdown, *app.caption]]
    parts += [str(metric.value) for metric in app.metric]
    for frame in [*app.dataframe, *app.get("arrow_data_frame")]:
        value = getattr(frame, "value", None)
        if isinstance(value, pd.DataFrame):
            parts.append(value.to_csv())
    return "\n".join(parts).replace(",", "")


def money_seen(text: str, amount: str) -> bool:
    whole = str(round(float(amount)))
    return amount in text or f"&#36;{whole}" in text or f"${whole}" in text


def test_a_vp_sees_every_club_event_but_only_their_own_committees_money(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_view="List")
    assert_clean(app, "Events as the Membership VP")
    text = page_text(app)
    assert "Zzmembership Social" in text
    assert "Zzconsulting Barcade Night" in text, "VPs work as one team: they see every event"
    assert money_seen(text, MINE)
    assert not money_seen(text, THEIRS) and not money_seen(text, SUGGESTED)


def test_the_month_view_shows_no_cost_on_other_committees_events(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP)
    assert_clean(app, "Events as the Membership VP, month view")
    text = page_text(app)
    assert not money_seen(text, THEIRS) and not money_seen(text, SUGGESTED)


def test_a_vp_can_narrow_to_their_own_committee(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_committee_vp="Membership events", events_view="List")
    assert_clean(app, "Events as the Membership VP, own committee only")
    text = page_text(app)
    assert "Zzmembership Social" in text and "Zzconsulting" not in text


def _event_id(db_path, key: str) -> int:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT event_id FROM events WHERE external_key = ?", (key,)).fetchone()[0]


def test_opening_another_committees_event_says_who_can_see_its_cost(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_committee_vp="All club events",
                 events_open=_event_id(events_db, "k-con"))
    assert_clean(app, "Events as the Membership VP, a Consulting event opened")
    text = page_text(app)
    assert "visible to the Consulting VP and the treasurer" in text
    assert not money_seen(text, THEIRS)


def test_opening_their_own_event_shows_its_purchases_without_move_or_unlink(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_open=_event_id(events_db, "k-mem"))
    assert_clean(app, "Events as the Membership VP, own event opened")
    assert money_seen(page_text(app), MINE)
    assert not [b for b in app.button if b.key and b.key.startswith("events_unlink_")]
    assert not [box for box in app.selectbox if box.key and box.key.startswith("events_move_")]


def test_another_committees_event_offers_a_vp_nothing_to_add(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_open=_event_id(events_db, "k-con"))
    assert not [box for box in app.selectbox if box.key and box.key.startswith("events_add_")]
    assert not [b for b in app.button if b.key and b.key.startswith(("events_add_go_", "events_suggest_"))]


def test_a_vp_can_add_an_unlinked_purchase_of_theirs_to_their_event(events_db):
    with sqlite3.connect(events_db) as db:
        db.execute(
            "INSERT INTO transactions (transaction_date, details, amount, account, budget_category, natural_key) "
            "VALUES (?, 'PURCHASE AUTHORIZED ON 01/01 ZZMEMSNACKS', -42.42, 'Wells Fargo', ?, 'test-memsnacks')",
            (_day(3), MEMBERSHIP),
        )
    event = _event_id(events_db, "k-mem")
    app = run_as(auth.Role.OFFICER, MEMBERSHIP, events_open=event)
    add = next(box for box in app.selectbox if box.key == f"events_add_{event}")
    assert not any("now on" in option for option in add.options), "a VP doesn't take purchases off other events"
    assert not any("Zzconshop" in option or "Barcade" in option for option in add.options), "only their committee's"
    add.select(next(option for option in add.options if "Zzmemsnacks" in option))
    app.session_state["events_open"] = event
    app = app.run()
    next(b for b in app.button if b.key == f"events_add_go_{event}").click()
    app.session_state["events_open"] = event
    app.run()
    with sqlite3.connect(events_db) as db:
        linked = db.execute(
            "SELECT e.external_key, l.linked_by FROM event_links l JOIN transactions t ON t.transactionid = l.transaction_id "
            "JOIN events e USING (event_id) WHERE t.details LIKE '%ZZMEMSNACKS'"
        ).fetchone()
    assert linked == ("k-mem", "x@sandbox.local")


def test_a_vp_cannot_sync_move_or_unlink(events_db):
    app = run_as(auth.Role.OFFICER, MEMBERSHIP)
    labels = {button.label for button in app.button}
    assert "Unlink" not in labels and "Sync these events" not in labels
    assert not app.get("popover"), "no 'Sync from a file' for a VP"


def test_the_treasurer_still_sees_every_committees_money(events_db):
    app = run_as(auth.Role.TREASURER, events_view="List")
    assert_clean(app, "Events as the treasurer")
    text = page_text(app)
    assert money_seen(text, MINE) and money_seen(text, THEIRS) and money_seen(text, SUGGESTED)
