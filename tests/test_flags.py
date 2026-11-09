"""
"This isn't ours": a VP disputing a charge.

The properties that matter, each with a test that fails if it breaks:

  * a VP can flag only a charge booked to their own committee's lines, and the
    data layer enforces it (not just the page);
  * flagging never changes the charge -- only the treasurer moves money;
  * a VP sees only their own committee's flags;
  * the treasurer sees open flags in the Review Queue and can close them.

All data is the sandbox seed; all emails are invented `@sandbox.local` addresses.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd import auth
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import flags as flags_domain

# Fixtures shared with the other VP tests.
from tests.test_views import seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import membership_db  # noqa: F401

OFFICER = auth.Role.OFFICER
VP = "vp@sandbox.local"
NOTE = "This was bought for a different committee's event."


def charge_in(backend: SqliteBackend, committee_id: int, *, nth: int = 0) -> int:
    frame = backend.fetch_transactions()
    rows = frame[frame["budget_category"].eq(committee_id).fillna(False)]
    return int(rows.iloc[nth]["transactionid"])


def snapshot(backend: SqliteBackend, transaction_id: int) -> dict:
    frame = backend.fetch_transactions()
    row = frame[frame["transactionid"] == transaction_id].iloc[0]
    return {
        key: (None if pd.isna(row[key]) else row[key])
        for key in ("amount", "details", "budget_category", "purpose", "transaction_date")
    }


# --- Rules (pure) --------------------------------------------------------------


def test_a_usable_note_is_trimmed_and_kept():
    cleaned, problem = flags_domain.clean_note("  Not   ours,\n it was for the other event  ")
    assert problem is None
    assert cleaned == "Not ours, it was for the other event"


@pytest.mark.parametrize("note", [None, float("nan"), "", "   ", "abc", "x" * 501])
def test_an_unusable_note_is_refused_with_a_reason(note):
    cleaned, problem = flags_domain.clean_note(note)
    assert cleaned is None
    assert problem


def test_an_open_flag_wins_over_an_older_closed_one():
    flags = pd.DataFrame(
        {
            "flag_id": [1, 2, 3],
            "transaction_id": [10, 10, 11],
            "status": ["resolved", "open", "dismissed"],
        }
    )
    assert flags_domain.status_by_transaction(flags) == {10: "open", 11: "dismissed"}
    assert flags_domain.status_by_transaction(pd.DataFrame()) == {}


def test_a_vp_reads_statuses_in_plain_words():
    assert flags_domain.vp_status_label("open") == "With the treasurer"
    assert "kept" in flags_domain.vp_status_label("dismissed").lower()


# --- The data layer ------------------------------------------------------------


def test_a_vp_can_flag_a_charge_booked_to_their_committee(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge = charge_in(backend, 7)
    result = backend.create_flag(charge, (7,), NOTE, VP)
    assert result.ok and result.updated == 1

    flags = backend.fetch_flags()
    assert len(flags) == 1
    row = flags.iloc[0]
    assert (row["transaction_id"], row["booked_to_committee_id"], row["flagged_by"]) == (charge, 7, VP)
    assert row["status"] == "open" and row["note"] == NOTE


def test_flagging_a_charge_changes_nothing_about_it(seeded_db, use_db):
    """A flag is a message. Only the treasurer moves money."""
    use_db(seeded_db)
    backend = SqliteBackend()
    charge = charge_in(backend, 7)
    before = snapshot(backend, charge)
    backend.create_flag(charge, (7,), NOTE, VP)
    assert snapshot(backend, charge) == before


def test_a_vp_cannot_flag_another_committees_charge(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    marketing_charge = charge_in(backend, 9)
    result = backend.create_flag(marketing_charge, (7,), NOTE, VP)  # a Consulting VP
    assert result.error and "not booked to your committee" in result.error
    assert backend.fetch_flags().empty


def test_an_uncategorized_charge_cannot_be_flagged(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    frame = backend.fetch_transactions()
    loose = int(frame[frame["budget_category"].isna()].iloc[0]["transactionid"])
    result = backend.create_flag(loose, (7,), NOTE, VP)
    assert result.error
    assert backend.fetch_flags().empty


def test_a_charge_that_does_not_exist_is_refused(seeded_db, use_db):
    use_db(seeded_db)
    result = SqliteBackend().create_flag(10**9, (7,), NOTE, VP)
    assert result.error and "does not exist" in result.error


@pytest.mark.parametrize("note", ["", "   ", "abc", "x" * 501])
def test_a_flag_needs_a_proper_note(note, seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    result = backend.create_flag(charge_in(backend, 7), (7,), note, VP)
    assert result.error
    assert backend.fetch_flags().empty


def test_flagging_twice_does_not_stack_duplicates(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge = charge_in(backend, 7)
    assert backend.create_flag(charge, (7,), NOTE, VP).updated == 1
    second = backend.create_flag(charge, (7,), "Flagging it again, just to be sure.", VP)
    assert second.unchanged == 1 and not second.error
    assert len(backend.fetch_flags()) == 1


def test_a_committee_that_owns_two_lines_can_flag_either(membership_db):
    backend = SqliteBackend()
    second_line_charge = charge_in(backend, 4)  # the President's line, as the second line
    assert backend.create_flag(second_line_charge, (5,), NOTE, VP).error  # Membership line alone: no
    assert backend.create_flag(second_line_charge, (5, 4), NOTE, VP).ok  # both lines: yes


def test_the_treasurer_can_close_a_flag_once_and_it_can_be_raised_again(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge = charge_in(backend, 7)
    backend.create_flag(charge, (7,), NOTE, VP)
    flag_id = int(backend.fetch_flags().iloc[0]["flag_id"])

    assert backend.resolve_flag(flag_id, "bogus", "treasurer@sandbox.local").error
    closed = backend.resolve_flag(flag_id, "resolved", "treasurer@sandbox.local", "Moved it.")
    assert closed.ok and closed.updated == 1
    row = backend.fetch_flags().iloc[0]
    assert (row["status"], row["resolved_by"], row["resolution_note"]) == (
        "resolved", "treasurer@sandbox.local", "Moved it.",
    )
    assert backend.resolve_flag(flag_id, "dismissed", "treasurer@sandbox.local").unchanged == 1

    # Closed flags no longer block a fresh one on the same charge.
    assert backend.create_flag(charge, (7,), "Still wrong after the last review.", VP).ok
    assert len(backend.fetch_flags()) == 2


def test_a_vp_is_only_ever_given_their_own_committees_flags(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.create_flag(charge_in(backend, 7), (7,), "Consulting note, not ours.", VP)
    backend.create_flag(charge_in(backend, 9), (9,), "Marketing note, not ours.", VP)
    assert set(backend.fetch_flags((7,))["note"]) == {"Consulting note, not ours."}
    assert set(backend.fetch_flags((9,))["note"]) == {"Marketing note, not ours."}
    assert backend.fetch_flags(()).empty
    assert len(backend.fetch_flags(None)) == 2


def test_flagging_and_closing_are_written_to_the_audit_trail(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge = charge_in(backend, 7)
    backend.create_flag(charge, (7,), NOTE, VP)
    backend.resolve_flag(int(backend.fetch_flags().iloc[0]["flag_id"]), "dismissed", "t@sandbox.local")
    audit = backend.fetch_audit()
    actions = set(audit[audit["transaction_id"] == charge]["action"])
    assert {"flag", "flag_resolved"} <= actions


def test_resetting_the_sandbox_clears_flags_without_a_foreign_key_error(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.create_flag(charge_in(backend, 7), (7,), NOTE, VP)
    backend.reset()
    assert backend.fetch_flags().empty


# --- The treasurer's verdict moves the charge ------------------------------------------

TREASURER = "treasurer@sandbox.local"


def _flagged(backend: SqliteBackend, committee_id: int = 7) -> tuple[int, int]:
    """File a flag on a committee's charge; return (charge id, flag id)."""
    charge = charge_in(backend, committee_id)
    assert backend.create_flag(charge, (committee_id,), NOTE, VP).ok
    flag_id = int(backend.fetch_flags((committee_id,)).iloc[0]["flag_id"])
    return charge, flag_id


def _spent_by_committee(backend: SqliteBackend, charge_id: int) -> dict[str, float]:
    """What each committee has spent in the term the charge falls in."""
    from ais_fmd.domain import budgets as budget_domain
    from ais_fmd.domain.terms import attach_semester

    transactions, terms = backend.fetch_transactions(), backend.fetch_terms()
    tagged = attach_semester(transactions, terms)
    semester = tagged[tagged["transactionid"] == charge_id].iloc[0]["Semester"]
    summary = budget_domain.budget_vs_actual(transactions, backend.fetch_budgets(), terms, semester)
    return dict(zip(summary["Committee_Name"], summary["Spent"]))


def test_the_verdict_moves_the_charge_and_closes_the_flag_together(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    result = backend.move_flagged_charge(flag_id, 9, TREASURER, "Checked the receipt.")
    assert result.ok and result.updated == 1

    after = snapshot(backend, charge)
    assert after["budget_category"] == 9  # now Marketing's
    for field in ("amount", "details", "purpose", "transaction_date"):
        assert after[field] == before[field], f"moving the charge changed its {field}"

    flag = backend.fetch_flags().iloc[0]
    assert flag["status"] == "resolved" and flag["resolved_by"] == TREASURER
    assert "Moved from Consulting to Marketing." in flag["resolution_note"]
    assert "Checked the receipt." in flag["resolution_note"]


def test_moving_a_charge_moves_the_money_between_the_two_committees_budgets(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    amount = abs(float(snapshot(backend, charge)["amount"]))
    before = _spent_by_committee(backend, charge)

    backend.move_flagged_charge(flag_id, 9, TREASURER)

    after = _spent_by_committee(backend, charge)
    assert after["Consulting"] == pytest.approx(before["Consulting"] - amount)
    assert after["Marketing"] == pytest.approx(before["Marketing"] + amount)
    others = set(before) - {"Consulting", "Marketing"}
    assert all(after[name] == pytest.approx(before[name]) for name in others)


def test_the_move_is_written_to_the_audit_trail_with_who_and_from_where(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    backend.move_flagged_charge(flag_id, 9, TREASURER)

    audit = backend.fetch_audit()
    rows = audit[audit["transaction_id"] == charge]
    moved = rows[(rows["action"] == "update") & (rows["field"] == "budget_category")].iloc[0]
    assert (str(moved["old_value"]), str(moved["new_value"]), moved["actor"]) == ("7", "9", TREASURER)
    assert "flag_resolved" in set(rows["action"])


def test_the_charges_purpose_is_kept_even_when_it_has_none(seeded_db, use_db):
    """`update_transactions` writes the purpose too, so a careless move would blank it."""
    import sqlite3

    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    with sqlite3.connect(backend.path) as connection:
        connection.execute("UPDATE transactions SET purpose = NULL WHERE transactionid = ?", (charge,))
    assert snapshot(backend, charge)["purpose"] is None

    assert backend.move_flagged_charge(flag_id, 9, TREASURER).ok
    assert snapshot(backend, charge)["purpose"] is None  # still none, not an empty string or a guess


@pytest.mark.parametrize("destination", [7, 99999])
def test_a_move_to_the_same_or_an_unknown_committee_is_refused_and_nothing_changes(destination, seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    result = backend.move_flagged_charge(flag_id, destination, TREASURER)

    assert result.error and not result.updated
    assert snapshot(backend, charge) == before
    assert backend.fetch_flags().iloc[0]["status"] == "open"


def test_a_charge_that_moved_since_it_was_flagged_is_not_moved_again(seeded_db, use_db):
    from ais_fmd.data.backend import TransactionChange

    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    purpose = snapshot(backend, charge)["purpose"]
    assert backend.update_transactions([TransactionChange(charge, purpose, 10)], TREASURER).updated == 1

    result = backend.move_flagged_charge(flag_id, 9, TREASURER)

    assert result.error and "changed since it was flagged" in result.error
    assert snapshot(backend, charge)["budget_category"] == 10  # untouched by the refused move
    assert backend.fetch_flags().iloc[0]["status"] == "open"


def test_a_charge_in_a_closed_term_cannot_be_moved_and_its_flag_stays_open(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    before = snapshot(backend, charge)

    date = pd.Timestamp(before["transaction_date"])
    terms = backend.fetch_terms()
    term = terms[(pd.to_datetime(terms["start_date"]) <= date) & (date <= pd.to_datetime(terms["end_date"]))]
    assert backend.set_term_lock(str(term.iloc[0]["TermID"]), True, TREASURER).ok

    result = backend.move_flagged_charge(flag_id, 9, TREASURER)

    assert result.error and "closed" in result.error
    assert snapshot(backend, charge) == before
    assert backend.fetch_flags().iloc[0]["status"] == "open"  # so it can be retried after reopening


def test_a_flag_that_is_already_closed_cannot_move_anything(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    backend.resolve_flag(flag_id, "dismissed", TREASURER)
    before = snapshot(backend, charge)

    result = backend.move_flagged_charge(flag_id, 9, TREASURER)

    assert result.unchanged == 1 and not result.error
    assert snapshot(backend, charge) == before


def test_a_charge_can_be_moved_to_a_ledger_bucket_not_only_a_committee(seeded_db, use_db):
    """The treasurer may decide a disputed charge is a refund or a transfer."""
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    assert backend.move_flagged_charge(flag_id, 17, TREASURER).ok  # 17 = Refunded
    assert snapshot(backend, charge)["budget_category"] == 17


def test_an_unknown_flag_is_refused(seeded_db, use_db):
    use_db(seeded_db)
    result = SqliteBackend().move_flagged_charge(424242, 9, TREASURER)
    assert result.error and "does not exist" in result.error
