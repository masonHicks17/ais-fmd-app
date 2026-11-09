"""Backend behaviour: atomicity, batched updates, audit trail, dedupe enforcement."""

from __future__ import annotations

import pytest

from ais_fmd.data.backend import TransactionChange
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.dedupe import assign_natural_keys

ACTOR = "tester@sandbox.local"


@pytest.fixture
def backend(tmp_path) -> SqliteBackend:
    """
    A fresh database with committees present.

    Committees must exist because `transactions.budget_category` is a real
    foreign key -- and, since the targeted ON CONFLICT clause replaced the
    blanket INSERT OR IGNORE, a violation now raises instead of silently
    dropping the row.
    """
    backend = SqliteBackend(tmp_path / "test.db")
    from ais_fmd.config.categories import COMMITTEES

    with backend._connect() as connection:  # noqa: SLF001
        connection.executemany(
            "INSERT OR REPLACE INTO committees "
            "(CommitteeID, Committee_Name, Committee_Type) VALUES (?, ?, ?)",
            [(c.id, c.name, c.kind) for c in COMMITTEES],
        )
        connection.commit()
    return backend


def test_foreign_key_violation_fails_loudly_instead_of_dropping_rows(tmp_path):
    """
    Regression guard. A blanket INSERT OR IGNORE swallowed foreign-key failures
    as well as duplicates, so a transaction pointing at a nonexistent committee
    disappeared while the upload reported success.
    """
    bare = SqliteBackend(tmp_path / "bare.db")  # no committees inserted
    receipt = bare.insert_transactions(
        assign_natural_keys(
            [
                {
                    "transaction_date": "2025-09-01",
                    "amount": -10.0,
                    "details": "X",
                    "account": "Wells Fargo",
                    "budget_category": 999,  # does not exist
                    "purpose": "Merch",
                }
            ]
        ),
        "bad.csv",
        ACTOR,
    )
    assert not receipt.ok
    assert receipt.error
    assert bare.fetch_transactions().empty, "the failed import must roll back entirely"


def records(count: int = 3) -> list[dict]:
    raw = [
        {
            "transaction_date": f"2025-09-{day:02d}",
            "amount": -10.0 * day,
            "details": f"MERCHANT {day}",
            "account": "Wells Fargo",
            "budget_category": 5,
            "purpose": "Meeting Food",
        }
        for day in range(1, count + 1)
    ]
    return assign_natural_keys(raw)


# --- Inserts -----------------------------------------------------------------

def test_insert_writes_rows_and_marks_the_file(backend):
    receipt = backend.insert_transactions(records(3), "checking_sep.csv", ACTOR)
    assert receipt.ok
    assert receipt.inserted == 3
    assert len(backend.fetch_transactions()) == 3

    files = backend.fetch_uploaded_files()
    assert files.iloc[0]["file_name"] == "checking_sep.csv"
    assert files.iloc[0]["row_count"] == 3


def test_f4_reimporting_the_same_file_inserts_nothing(backend):
    """
    The natural-key unique index enforces this in the database, so it holds even
    if application-level dedupe is bypassed.
    """
    rows = records(3)
    backend.insert_transactions(rows, "checking_sep.csv", ACTOR)
    second = backend.insert_transactions(rows, "checking_sep.csv", ACTOR)

    assert second.ok
    assert second.inserted == 0
    assert second.duplicates_skipped == 3
    assert len(backend.fetch_transactions()) == 3


def test_f4_upload_marks_the_file_and_rows_together(backend):
    """Both writes are inside one transaction, so they cannot diverge."""
    backend.insert_transactions(records(2), "atomic.csv", ACTOR)
    assert len(backend.fetch_transactions()) == 2
    assert backend.is_file_uploaded("atomic.csv")


def test_insert_of_nothing_is_reported_not_silently_ignored(backend):
    receipt = backend.insert_transactions([], "empty.csv", ACTOR)
    assert not receipt.ok
    assert receipt.error


# --- Updates (F6) ------------------------------------------------------------

def test_f6_unchanged_rows_are_not_written(backend):
    """
    The original always wrote every visible row because it compared an int
    against a display string. An unchanged row must produce zero writes.
    """
    backend.insert_transactions(records(3), "f.csv", ACTOR)
    stored = backend.fetch_transactions()

    unchanged = [
        TransactionChange(
            transaction_id=int(row["transactionid"]),
            purpose=row["purpose"],
            budget_category=int(row["budget_category"]),
        )
        for _, row in stored.iterrows()
    ]
    result = backend.update_transactions(unchanged, ACTOR)

    assert result.updated == 0
    assert result.unchanged == 3
    assert result.ok


def test_f6_only_genuinely_changed_rows_are_written(backend):
    backend.insert_transactions(records(3), "f.csv", ACTOR)
    stored = backend.fetch_transactions()
    target = int(stored.iloc[0]["transactionid"])

    result = backend.update_transactions(
        [TransactionChange(transaction_id=target, purpose="Merch", budget_category=9)],
        ACTOR,
    )
    assert result.updated == 1

    refreshed = backend.fetch_transactions()
    row = refreshed[refreshed["transactionid"] == target].iloc[0]
    assert row["purpose"] == "Merch"
    assert row["budget_category"] == 9


def test_update_reports_missing_ids_rather_than_failing_silently(backend):
    result = backend.update_transactions(
        [TransactionChange(transaction_id=999_999, purpose="Merch", budget_category=9)],
        ACTOR,
    )
    assert result.failed == [999_999]


def test_clearing_a_category_is_a_real_change(backend):
    backend.insert_transactions(records(1), "f.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])

    result = backend.update_transactions(
        [TransactionChange(transaction_id=target, purpose=None, budget_category=None)], ACTOR
    )
    assert result.updated == 1
    row = backend.fetch_transactions().iloc[0]
    assert row["purpose"] is None
    assert row["budget_category"] is None or str(row["budget_category"]) == "<NA>"


# --- Audit trail (M2) --------------------------------------------------------

def test_every_insert_is_audited(backend):
    backend.insert_transactions(records(3), "f.csv", ACTOR)
    audit = backend.fetch_audit(100)
    inserts = audit[audit["action"] == "insert"]
    assert len(inserts) == 3
    assert set(inserts["actor"]) == {ACTOR}


def test_updates_record_the_previous_value(backend):
    backend.insert_transactions(records(1), "f.csv", ACTOR)
    target = int(backend.fetch_transactions().iloc[0]["transactionid"])

    backend.update_transactions(
        [TransactionChange(transaction_id=target, purpose="Merch", budget_category=9)], ACTOR
    )
    audit = backend.fetch_audit(100)
    purpose_change = audit[(audit["action"] == "update") & (audit["field"] == "purpose")]
    assert len(purpose_change) == 1
    assert purpose_change.iloc[0]["old_value"] == "Meeting Food"
    assert purpose_change.iloc[0]["new_value"] == "Merch"


# --- Budgets -----------------------------------------------------------------

def test_budget_upsert_does_not_delete_first(backend):
    """
    The original deleted every budget for the term before re-inserting, which
    destroyed history and left a window with no budget at all.
    """
    backend.insert_term("FA25", "Fall 2025", "2025-08-20", "2025-12-12", ACTOR)
    with backend._connect() as connection:  # noqa: SLF001
        connection.executemany(
            "INSERT OR REPLACE INTO committees VALUES (?, ?, ?)",
            [(5, "Membership", "committee"), (9, "Marketing", "committee")],
        )
        connection.commit()

    backend.upsert_budgets("FA25", {5: 1000.0, 9: 500.0}, ACTOR)
    first = backend.fetch_budgets()
    assert len(first) == 2

    result = backend.upsert_budgets("FA25", {5: 1200.0, 9: 500.0}, ACTOR)
    assert result.updated == 1
    assert result.unchanged == 1

    second = backend.fetch_budgets()
    assert len(second) == 2
    assert float(second[second["committeeid"] == 5].iloc[0]["budget_amount"]) == 1200.0


def test_duplicate_term_is_rejected_with_a_message(backend):
    backend.insert_term("FA25", "Fall 2025", "2025-08-20", "2025-12-12", ACTOR)
    result = backend.insert_term("FA25", "Fall 2025", "2025-08-20", "2025-12-12", ACTOR)
    assert result.error and "already exists" in result.error


# --- Merchants ---------------------------------------------------------------

def test_merchant_upsert_increments_hit_count(backend):
    rule = {
        "merchant_key": "publix",
        "canonical_name": "Publix",
        "committee_id": 5,
        "purpose": "Meeting Food",
        "hit_count": 1,
    }
    backend.upsert_merchants([rule], ACTOR)
    backend.upsert_merchants([rule], ACTOR)

    merchants = backend.fetch_merchants()
    assert len(merchants) == 1
    assert int(merchants.iloc[0]["hit_count"]) == 2


# --- Empty database ----------------------------------------------------------

def test_reads_on_an_empty_database_return_shaped_frames(backend):
    for frame in (
        backend.fetch_transactions(),
        backend.fetch_terms(),
        backend.fetch_budgets(),
        backend.fetch_merchants(),
        backend.fetch_audit(),
        backend.fetch_statement_balances(),
    ):
        assert frame.empty
        assert list(frame.columns), "empty frames must still carry their columns"
