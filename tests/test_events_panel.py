"""
The event panel and the event picker in the suggestions table (2026-11-09).

A treasurer opens an event (as a calendar click or list row would) and moves,
unlinks or adds purchases there; in the suggestions table they can pick a
different event than the guess. Uses the fixture from test_events_vp.
"""

from __future__ import annotations

import sqlite3

from ais_fmd import auth

OPEN = "events_open"

from tests.test_events_vp import (  # noqa: F401 - fixtures
    CONSULTING, MEMBERSHIP, SUGGESTED, _event_id, events_db, money_seen, page_text, run_as,
)
from tests.test_views import assert_clean, seeded_db, use_db  # noqa: F401


def _links(db_path) -> dict[str, str]:
    """Purchase details -> event key, for every link."""
    with sqlite3.connect(db_path) as db:
        return dict(db.execute(
            "SELECT t.details, e.external_key FROM event_links l "
            "JOIN transactions t ON t.transactionid = l.transaction_id JOIN events e USING (event_id)"
        ))


def _detail(links: dict[str, str], word: str) -> str:
    return next(d for d in links if word in d)


def test_the_panel_shows_the_events_purchases_and_suggestions(events_db):
    app = run_as(auth.Role.TREASURER, events_open=_event_id(events_db, "k-con"))
    assert_clean(app, "Treasurer opens the Consulting event")
    text = page_text(app)
    assert "Zzconsulting Barcade Night" in text or any("Barcade" in str(e.value) for e in app.markdown)
    assert "Suggested for this event" in text
    assert money_seen(text, SUGGESTED)


def test_moving_a_purchase_to_another_event(events_db):
    app = run_as(auth.Role.TREASURER, events_open=_event_id(events_db, "k-mem"))
    move = next(box for box in app.selectbox if box.key and box.key.startswith("events_move_"))
    move.set_value(_event_id(events_db, "k-con"))
    app.session_state[OPEN] = _event_id(events_db, "k-mem")
    app.run()
    assert _links(events_db)[_detail(_links(events_db), "ZZMEMSHOP")] == "k-con"


def test_unlinking_a_purchase(events_db):
    app = run_as(auth.Role.TREASURER, events_open=_event_id(events_db, "k-mem"))
    next(b for b in app.button if b.key and b.key.startswith("events_unlink_")).click()
    app.session_state[OPEN] = _event_id(events_db, "k-mem")
    app.run()
    assert not any("ZZMEMSHOP" in d for d in _links(events_db))


def test_linking_a_suggested_purchase_from_the_panel(events_db):
    app = run_as(auth.Role.TREASURER, events_open=_event_id(events_db, "k-con"))
    next(b for b in app.button if b.key and b.key.startswith("events_suggest_")).click()
    app.session_state[OPEN] = _event_id(events_db, "k-con")
    app.run()
    assert _links(events_db)[_detail(_links(events_db), "BARCADE")] == "k-con"


def test_linking_any_nearby_purchase_from_the_panel(events_db):
    app = run_as(auth.Role.TREASURER, events_open=_event_id(events_db, "k-mem"))
    add = next(box for box in app.selectbox if box.key and box.key.startswith("events_add_"))
    barcade = next(option for option in add.options if "· Barcade ·" in option)
    add.select(barcade)
    app.session_state[OPEN] = _event_id(events_db, "k-mem")
    app = app.run()
    next(b for b in app.button if b.key and b.key.startswith("events_add_go_")).click()
    app.session_state[OPEN] = _event_id(events_db, "k-mem")
    app.run()
    assert _links(events_db)[_detail(_links(events_db), "BARCADE")] == "k-mem"


def test_picking_another_event_in_the_suggestions_table_links_it_there(events_db):
    app = run_as(auth.Role.TREASURER, events_confidence=["High", "Medium", "Low"])
    key = next(d.key for d in app.dataframe if d.key and d.key.startswith("events_suggestions_"))
    # AppTest can't edit a data_editor cell; set the edit state Streamlit keeps
    # for one. The guess (Consulting) is High, so Link starts ticked: untick it
    # and pick the Membership event instead -- changing the event counts as accepting.
    app.session_state[key] = {"edited_rows": {0: {"Event": _event_id(events_db, "k-mem"), "Link": False}},
                              "added_rows": [], "deleted_rows": []}
    app = app.run()
    button = next(b for b in app.button if b.key == "events_link")
    assert button.label == "Link 1 purchase(s)"
    button.click().run()
    assert _links(events_db)[_detail(_links(events_db), "BARCADE")] == "k-mem"
