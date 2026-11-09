"""
"This isn't ours": the VP and treasurer pages.

The data rules (who can flag what, that flagging never changes a charge) are tested
in test_flags.py. These tests drive the real pages through both sides of the flow:
a VP flagging a charge, and the treasurer seeing and closing it in the Review Queue.
"""

from __future__ import annotations

import pandas as pd

from ais_fmd import auth
from ais_fmd.data.backend import TransactionChange
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.details import tidy
from ais_fmd.ui.flags import VERDICT_FIXED, VERDICT_KEEP, VERDICT_MOVE

# Fixtures and helpers shared with the other VP tests.
from tests.test_flags import NOTE, OFFICER, TREASURER, VP, _flagged, charge_in, snapshot  # noqa: F401
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import _committee_rows, _shown_rows, run_as  # noqa: F401


def test_a_vp_flags_a_charge_through_the_page_and_nothing_else_changes(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    assert_clean(app, "My Transactions as the Consulting VP")

    app.selectbox(key="flag_charge_0").select_index(0)
    app.text_area(key="flag_note_0").input(NOTE)
    app.button(key="flag_submit_0").click()
    app.run()
    assert_clean(app, "after flagging a charge")

    backend = SqliteBackend()
    flags = backend.fetch_flags()
    assert len(flags) == 1
    flagged = int(flags.iloc[0]["transaction_id"])
    assert flags.iloc[0]["booked_to_committee_id"] == 7
    assert snapshot(backend, flagged)["budget_category"] == 7  # still booked where it was
    assert any("Flagged" in block.value for block in app.success)

    table = app.dataframe[0].value
    assert "Flag" in table.columns
    assert "With the treasurer" in set(table["Flag"])


def test_the_page_refuses_a_flag_with_no_note(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    app.button(key="flag_submit_0").click()
    app.run()
    assert_clean(app, "submitting a flag with no note")
    assert SqliteBackend().fetch_flags().empty


def test_a_vp_sees_only_their_own_flags_on_the_page(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.create_flag(charge_in(backend, 7), (7,), "Consulting note, not ours.", VP)
    backend.create_flag(charge_in(backend, 9), (9,), "Marketing note, not ours.", VP)

    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    assert_clean(app, "My Transactions as the Consulting VP")
    notes = set()
    for element in app.dataframe:
        if "Your note" in element.value.columns:
            notes |= set(element.value["Your note"])
    assert notes == {"Consulting note, not ours."}


def test_the_treasurer_sees_a_flag_in_the_review_queue(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.create_flag(charge_in(backend, 7), (7,), NOTE, VP)
    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "Review Queue with an open flag")
    assert any(NOTE in block.value for block in app.markdown)


def _decide(app, flag_id, verdict, *, destination=None, reply=None):
    """Drive the treasurer's decision card the way a person would, one step at a time."""
    app.radio(key=f"flag_verdict_{flag_id}").set_value(verdict).run()
    if destination is not None:
        app.selectbox(key=f"flag_dest_{flag_id}").select(destination).run()
    if reply:
        app.text_input(key=f"flag_reply_{flag_id}").input(reply).run()
    app.button(key=f"flag_apply_{flag_id}").click().run()
    return app


def test_the_treasurer_moves_a_charge_and_both_committees_see_it_move(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)  # a Consulting VP says it is not theirs
    stored = backend.fetch_transactions().set_index("transactionid").loc[charge]
    moved = (str(pd.Timestamp(stored["transaction_date"]).date()), round(float(stored["amount"]), 2), tidy(stored["details"]))
    consulting_before = _committee_rows(7)
    marketing_before = _committee_rows(9)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "Review Queue with an open flag")
    _decide(app, flag_id, VERDICT_MOVE, destination="9 - Marketing", reply="Checked the receipt.")
    assert_clean(app, "after the treasurer moved the charge")

    assert snapshot(backend, charge)["budget_category"] == 9
    flag = backend.fetch_flags().iloc[0]
    assert flag["status"] == "resolved"
    assert "Moved from Consulting to Marketing." in flag["resolution_note"]

    # The books agree on both sides: one fewer for Consulting, one more for Marketing.
    assert len(_committee_rows(7)) == len(consulting_before) - 1
    assert len(_committee_rows(9)) == len(marketing_before) + 1
    assert moved in _committee_rows(9)
    assert moved not in _committee_rows(7)

    # The Consulting VP no longer has the charge, and is told how it ended.
    consulting = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    assert moved not in _shown_rows(consulting.dataframe[0].value)
    answered = next(e.value for e in consulting.dataframe if "Treasurer's reply" in e.value.columns)
    assert answered["Status"].tolist() == ["Resolved"]
    assert "Moved from Consulting to Marketing." in answered["Treasurer's reply"].iloc[0]

    # The Marketing VP now has it.
    marketing = run_as(VIEWS / "MyTransactions.py", OFFICER, 9, mytxn_semester="All")
    assert moved in _shown_rows(marketing.dataframe[0].value)


def test_keeping_a_charge_closes_the_flag_and_changes_nothing(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    _decide(app, flag_id, VERDICT_KEEP, reply="It is Consulting's.")
    assert_clean(app, "keeping a flagged charge")

    flag = backend.fetch_flags().iloc[0]
    assert (flag["status"], flag["resolution_note"]) == ("dismissed", "It is Consulting's.")
    assert snapshot(backend, charge) == before


def test_closing_a_flag_you_already_fixed_elsewhere_moves_nothing(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    _decide(app, flag_id, VERDICT_FIXED)
    assert_clean(app, "closing a flag as already fixed")

    assert backend.fetch_flags().iloc[0]["status"] == "resolved"
    assert snapshot(backend, charge) == before


def test_nothing_can_be_applied_until_a_verdict_and_a_destination_are_chosen(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert app.button(key=f"flag_apply_{flag_id}").proto.disabled  # no verdict yet
    app.radio(key=f"flag_verdict_{flag_id}").set_value(VERDICT_MOVE).run()
    assert app.button(key=f"flag_apply_{flag_id}").proto.disabled  # a move needs a destination
    assert snapshot(backend, charge) == before


def test_the_destination_list_never_offers_the_committee_it_is_already_in(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    _, flag_id = _flagged(backend, 7)
    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    app.radio(key=f"flag_verdict_{flag_id}").set_value(VERDICT_MOVE).run()
    options = app.selectbox(key=f"flag_dest_{flag_id}").options
    assert "7 - Consulting" not in options and "9 - Marketing" in options


def test_a_charge_that_moved_since_it_was_flagged_offers_no_move(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    purpose = snapshot(backend, charge)["purpose"]
    backend.update_transactions([TransactionChange(charge, purpose, 10)], TREASURER)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "a flag whose charge has since moved")
    assert VERDICT_MOVE not in app.radio(key=f"flag_verdict_{flag_id}").options
    assert any("moved since it was flagged" in warning.value for warning in app.warning)


def test_a_closed_term_refuses_the_move_and_leaves_the_flag_open(seeded_db, use_db):
    import pandas as pd

    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)
    date = pd.Timestamp(before["transaction_date"])
    terms = backend.fetch_terms()
    term = terms[(pd.to_datetime(terms["start_date"]) <= date) & (date <= pd.to_datetime(terms["end_date"]))]
    backend.set_term_lock(str(term.iloc[0]["TermID"]), True, TREASURER)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    _decide(app, flag_id, VERDICT_MOVE, destination="9 - Marketing")
    assert_clean(app, "trying to move a charge in a closed term")

    assert any("closed" in block.value and "Reopen the term" in block.value for block in app.error)
    assert snapshot(backend, charge) == before
    assert backend.fetch_flags().iloc[0]["status"] == "open"
