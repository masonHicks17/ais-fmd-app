"""
The sandbox backend: a local SQLite file.

Properties that make this safe to hand to someone for testing:

  * It is created on demand under `sandbox_data/` and can be deleted freely.
  * It never opens a socket. There is no configuration that points it anywhere.
  * Uploads are genuinely atomic -- SQLite gives real transactions, so the
    partial-write failure mode described in FINDING F4 cannot occur.
  * The unique index on `natural_key` enforces deduplication in the database
    rather than in a pandas filter (FINDING F3).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

from .. import settings
from ..domain import flags as flags_domain
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt, normalize_email

SCHEMA = """
CREATE TABLE IF NOT EXISTS committees (
    CommitteeID     INTEGER PRIMARY KEY,
    Committee_Name  TEXT NOT NULL UNIQUE,
    Committee_Type  TEXT NOT NULL DEFAULT 'committee'
);

CREATE TABLE IF NOT EXISTS terms (
    TermID      TEXT PRIMARY KEY,
    Semester    TEXT,
    start_date  TEXT,
    end_date    TEXT,
    -- Module M10: once a term is closed its transactions become read-only, so a
    -- retroactive edit cannot silently change a figure already reported.
    locked      INTEGER NOT NULL DEFAULT 0,
    locked_at   TEXT,
    locked_by   TEXT,
    -- Dues rates in force this term, as a comma-separated decimal list
    -- ("35.00,52.50"). Previously a module constant pinned to Fall 2024, which
    -- meant the first term after a rate change silently stopped categorizing
    -- dues. NULL falls back to that constant.
    dues_rates          TEXT,
    -- 0 until a human confirms the rates are what the term actually charged.
    -- Seeded rates are copies of the old constant, not evidence.
    dues_rates_verified INTEGER NOT NULL DEFAULT 0,
    -- When the dues price goes up during the term (2026-11-08). The early price
    -- applies through dues_increase_date (inclusive), the late price after it.
    -- dues_late_cutoff: payments after this date are flagged for a look --
    -- late dues, or someone paying for another member. All NULL = no checks.
    dues_early_price    REAL,
    dues_late_price     REAL,
    dues_increase_date  TEXT,
    dues_late_cutoff    TEXT
);

CREATE TABLE IF NOT EXISTS committeebudgets (
    committeebudgetid INTEGER PRIMARY KEY AUTOINCREMENT,
    termid            TEXT NOT NULL,
    committeeid       INTEGER NOT NULL,
    budget_amount     REAL,
    UNIQUE (termid, committeeid),
    FOREIGN KEY (termid) REFERENCES terms (TermID),
    FOREIGN KEY (committeeid) REFERENCES committees (CommitteeID)
);

CREATE TABLE IF NOT EXISTS transactions (
    transactionid    INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_date TEXT NOT NULL,
    amount           REAL NOT NULL,
    details          TEXT,
    budget_category  INTEGER,
    purpose          TEXT,
    account          TEXT,
    source_file      TEXT,
    natural_key      TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (budget_category) REFERENCES committees (CommitteeID)
);

-- FINDING F3: deduplication enforced by the database, not by a pandas filter.
CREATE UNIQUE INDEX IF NOT EXISTS ux_transactions_natural_key
    ON transactions (natural_key) WHERE natural_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions (transaction_date);
CREATE INDEX IF NOT EXISTS ix_transactions_committee ON transactions (budget_category);

CREATE TABLE IF NOT EXISTS uploaded_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name   TEXT NOT NULL UNIQUE,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    row_count   INTEGER DEFAULT 0,
    uploaded_by TEXT
);

-- Module M2: audit trail.
CREATE TABLE IF NOT EXISTS transaction_audit (
    audit_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER,
    action         TEXT NOT NULL,
    field          TEXT,
    old_value      TEXT,
    new_value      TEXT,
    actor          TEXT,
    changed_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_audit_txn ON transaction_audit (transaction_id);

-- Module M4: merchant memory.
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_key   TEXT NOT NULL UNIQUE,
    canonical_name TEXT,
    committee_id   INTEGER,
    purpose        TEXT,
    hit_count      INTEGER DEFAULT 0,
    source         TEXT DEFAULT 'learned',
    -- Which committees a human has actually chosen for this merchant, and how
    -- often: "5:3,7:7". `committee_id` above holds the dominant reading; this
    -- holds the whole distribution, so a merchant that genuinely spans
    -- committees can say so instead of being overwritten by whichever decision
    -- came last. See domain/categorize/merchants.py.
    committee_counts TEXT,
    updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Module M8: reimbursement requests.
CREATE TABLE IF NOT EXISTS reimbursements (
    request_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    requester       TEXT NOT NULL,
    committee_id    INTEGER,
    amount          REAL NOT NULL,
    description     TEXT,
    incurred_on     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    submitted_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    decided_at      TEXT,
    decided_by      TEXT,
    decision_note   TEXT,
    matched_transaction_id INTEGER,
    receipt_id      INTEGER,
    FOREIGN KEY (committee_id) REFERENCES committees (CommitteeID)
);

CREATE INDEX IF NOT EXISTS ix_reimbursements_status ON reimbursements (status);

-- "This isn't ours": a VP disputing a charge booked to their committee. A flag is
-- a note to the treasurer and never changes the booking itself. `booked_to` is
-- the committee line the charge sat in when it was flagged, which is what scopes
-- a VP's view of their own flags even if the treasurer later moves the charge.
CREATE TABLE IF NOT EXISTS transaction_flags (
    flag_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id         INTEGER NOT NULL,
    booked_to_committee_id INTEGER,
    flagged_by             TEXT NOT NULL,
    note                   TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'open',
    flagged_at             TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at            TEXT,
    resolved_by            TEXT,
    resolution_note        TEXT,
    FOREIGN KEY (transaction_id) REFERENCES transactions (transactionid)
);

CREATE INDEX IF NOT EXISTS ix_flags_status ON transaction_flags (status);

-- At most one open flag per charge, so a double-click or a second VP click cannot
-- stack duplicates in the treasurer's queue.
CREATE UNIQUE INDEX IF NOT EXISTS ux_flags_one_open
    ON transaction_flags (transaction_id) WHERE status = 'open';

-- The model spending ledger (migrations/013_model_runs.sql). Every model call
-- belongs to a run, and a run is recorded before its first call.
CREATE TABLE IF NOT EXISTS llm_runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose            TEXT NOT NULL,
    model              TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'running',
    started_by         TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    rows_planned       INTEGER NOT NULL DEFAULT 0,
    calls_planned      INTEGER NOT NULL DEFAULT 0,
    calls_made         INTEGER NOT NULL DEFAULT 0,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_usd       REAL NOT NULL,
    cost_usd           REAL NOT NULL DEFAULT 0,
    error              TEXT
);

-- One run at a time: a second "running" row cannot exist, so a double-click
-- or two treasurers at once get a refusal, not two runs.
CREATE UNIQUE INDEX IF NOT EXISTS ux_llm_runs_one_running
    ON llm_runs (status) WHERE status = 'running';

-- One verdict per charge per committee it was booked to. Re-booking a charge
-- makes it eligible once more; nothing else does.
CREATE TABLE IF NOT EXISTS spend_reviews (
    review_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id         INTEGER NOT NULL,
    booked_committee_id    INTEGER NOT NULL,
    verdict                TEXT NOT NULL,
    suggested_committee_id INTEGER,
    confidence             REAL,
    reason                 TEXT,
    message_to_vp          TEXT,
    status                 TEXT NOT NULL,
    run_id                 INTEGER,
    model                  TEXT,
    prompt_version         TEXT,
    reviewed_at            TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at            TEXT,
    resolved_by            TEXT,
    resolution_note        TEXT,
    UNIQUE (transaction_id, booked_committee_id),
    FOREIGN KEY (transaction_id) REFERENCES transactions (transactionid),
    FOREIGN KEY (run_id) REFERENCES llm_runs (run_id)
);

CREATE INDEX IF NOT EXISTS ix_spend_reviews_status ON spend_reviews (status);

-- Module M9: receipt capture.
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name      TEXT NOT NULL,
    stored_path    TEXT NOT NULL,
    content_type   TEXT,
    byte_size      INTEGER,
    uploaded_by    TEXT,
    uploaded_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    transaction_id INTEGER,
    request_id     INTEGER,
    vendor         TEXT,
    amount         REAL,
    receipt_date   TEXT
);

CREATE INDEX IF NOT EXISTS ix_receipts_txn ON receipts (transaction_id);

-- Module M1: reconciliation inputs.
CREATE TABLE IF NOT EXISTS statement_balances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT NOT NULL,
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    opening_balance REAL NOT NULL,
    closing_balance REAL NOT NULL,
    source_file     TEXT,
    recorded_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account, period_start, period_end)
);

-- M19: human-assigned labels, and what the model had proposed at the time.
--
-- One table serves two jobs that are really the same job. A row imported from a
-- past treasurer's ledger is a label with no model prediction; a decision made
-- in the review queue is a label that also records what the model said. Keeping
-- them together means "everything a human has ever decided" is one query, and
-- agreement rate falls out of the rows where model_committee is not null.
--
-- `era` scopes features that do not survive an officer handover. Card numbers
-- are the clear case: the 2022 ledger's cards (0319, 6570, 2949) belong to
-- people who have since graduated, so a card weight fitted across all eras
-- would be nonsense. Merchant and description patterns do transfer.
CREATE TABLE IF NOT EXISTS labeled_examples (
    label_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,          -- 'ledger' | 'review' | 'spot-check'
    source_ref        TEXT,                   -- workbook name, or similar
    era               TEXT,                   -- e.g. '2022-2023'; scopes card features
    transaction_id    INTEGER,                -- set when the label came from the queue
    transaction_date  TEXT,
    amount            REAL,
    details           TEXT NOT NULL,
    account           TEXT,
    committee_id      INTEGER NOT NULL,
    purpose           TEXT,
    model_committee   INTEGER,                -- what scoring proposed, when known
    model_confidence  REAL,
    model_source      TEXT,
    sample_weight     REAL,                   -- spot-checks: rows of the population this one stands for
    labeled_by        TEXT NOT NULL,
    labeled_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    natural_key       TEXT UNIQUE             -- dedupes re-imports of the same ledger row
);

CREATE INDEX IF NOT EXISTS idx_labeled_examples_source ON labeled_examples (source);
CREATE INDEX IF NOT EXISTS idx_labeled_examples_committee ON labeled_examples (committee_id);

-- Module M21: the expected membership roster, for dues reconciliation.
--
-- Until this existed the app could report who DID pay, recovered from transfer
-- descriptions, but had nothing to compare that against -- so "who still owes?"
-- could only be answered by typing a headcount and subtracting.
--
-- match_key is the normalised name (`domain.roster.match_key`), stored rather
-- than recomputed so the uniqueness constraint and the matching rule cannot
-- drift apart. Scoped per term: a roster is a snapshot of one semester's
-- membership, and re-uploading replaces that term's roster rather than merging
-- into it.
CREATE TABLE IF NOT EXISTS members (
    member_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id        TEXT NOT NULL,
    full_name      TEXT NOT NULL,
    match_key      TEXT NOT NULL,
    -- Additional normalised names this person answers to, comma-separated.
    -- Stored rather than rebuilt on load because rebuilding needs the surname
    -- as a separate field, which a roster with one full-name column does not
    -- have. Without it a stored roster silently loses preferred-name matching
    -- that worked at import time -- and on the Fall 2026 form that is 64 of 149
    -- members.
    alt_keys       TEXT,
    preferred_name TEXT,
    -- Whether they certified on the membership form that they had paid.
    claims_paid    INTEGER NOT NULL DEFAULT 0,
    email          TEXT,
    ufid           TEXT,
    committee_id   INTEGER,
    notes          TEXT,
    source_file    TEXT,
    uploaded_by    TEXT,
    uploaded_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (term_id, match_key)
);

CREATE INDEX IF NOT EXISTS idx_members_term ON members (term_id);

-- Events, from the club calendar (domain/events.py): the AIS Event Schedule
-- workbooks or an .ics export of the Google Calendar. external_key is the day
-- plus the normalised title, so syncing the same schedule again updates rows
-- instead of duplicating them, and an event keeps its event_id -- which the
-- links below point at -- across syncs.
CREATE TABLE IF NOT EXISTS events (
    event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_key      TEXT NOT NULL UNIQUE,
    term_id           TEXT,
    event_date        TEXT NOT NULL,
    title             TEXT NOT NULL,
    start_time        TEXT,
    end_time          TEXT,
    location          TEXT,
    owner             TEXT,
    audience          TEXT,
    status            TEXT,
    committee_id      INTEGER,
    attendance        INTEGER,
    -- 'schedule' (typed on the sheet), 'member database' (GBM check-ins).
    attendance_source TEXT,
    description       TEXT,
    source            TEXT,
    calendar_id       TEXT,
    synced_by         TEXT,
    synced_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    -- 1 once someone adds, moves or edits the event in the app; a sync of the
    -- same event from the calendar sets it back to 0. "Export changes" sends
    -- only these to Google Calendar.
    edited_in_app     INTEGER NOT NULL DEFAULT 0,
    edited_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_term ON events (term_id, event_date);

-- Costs typed against an event (2026-11-13): an estimate before it happens,
-- or a cost the bank statement will never show (cash, a member's card not yet
-- reimbursed). Purchases from the statement are linked in event_links instead.
CREATE TABLE IF NOT EXISTS event_costs (
    cost_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('estimate', 'manual')),
    label      TEXT NOT NULL,
    amount     REAL NOT NULL,
    added_by   TEXT,
    added_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_event_costs_event ON event_costs (event_id);

-- Which event a transaction paid for. One event per transaction; confirmed by
-- a person (a suggestion from domain/events.suggest_links is not stored).
CREATE TABLE IF NOT EXISTS event_links (
    transaction_id INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL,
    linked_by      TEXT,
    linked_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Module M22: VP/officer -> committee assignment, for the committee-scoped
-- portal (`views/Officer.py`). Keyed by email so a signed-in Google identity
-- (Streamlit's native OIDC, not Supabase Auth -- see migrations/002 for why)
-- can be looked up directly, with no separate account-creation step. Not
-- term-scoped: one active assignment per person, changed when it changes,
-- same shape as `migrations/002_deferred_features.sql`.
CREATE TABLE IF NOT EXISTS profiles (
    email        TEXT PRIMARY KEY,
    display_name TEXT,
    role         TEXT NOT NULL DEFAULT 'member',
    committee_id INTEGER,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    created_by   TEXT
);

-- A VP's budget plan (`domain/committee_plan.py`): one line per category (a
-- purpose) and an amount. Keyed by the semester's *name*, not its TermID,
-- because a VP plans for next term before the treasurer has set it up. A plan
-- is an estimate: it never books, moves or reserves money.
CREATE TABLE IF NOT EXISTS committee_plan_lines (
    line_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id INTEGER NOT NULL,
    semester     TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    amount       REAL NOT NULL,
    note         TEXT,
    updated_by   TEXT,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (committee_id, semester, purpose)
);

-- One budget request per committee and semester; sending again replaces it.
-- A message to the treasurer, who still sets every budget.
CREATE TABLE IF NOT EXISTS budget_requests (
    request_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id INTEGER NOT NULL,
    semester     TEXT NOT NULL,
    amount       REAL NOT NULL,
    reason       TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (committee_id, semester)
);

-- Events a VP plans for a semester (My Plan, 2026-11-09): what, roughly when,
-- what it should cost and who should come. Like plan lines, keyed by the
-- semester's name and only an estimate -- it never books or reserves money.
-- `kind` is the past-event idea it came from ("Tailgate", "GBM"), if any.
CREATE TABLE IF NOT EXISTS planned_events (
    planned_event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    committee_id        INTEGER NOT NULL,
    semester            TEXT NOT NULL,
    event_date          TEXT,
    title               TEXT NOT NULL,
    kind                TEXT,
    estimated_cost      REAL NOT NULL DEFAULT 0,
    expected_attendance INTEGER,
    note                TEXT,
    updated_by          TEXT,
    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
"""



def _describe_dues_schedule(early, late, increase, cutoff) -> str:
    """One readable line for the audit log."""
    if early is None and late is None and not increase and not cutoff:
        return "(none)"
    price = lambda v: "—" if v is None else f"${float(v):,.2f}"  # noqa: E731
    return f"{price(early)} through {increase or '—'}, then {price(late)}; late after {cutoff or '—'}"

class SqliteBackend(Backend):
    name = "sandbox-sqlite"

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else settings.sandbox_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # --- plumbing ------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.commit()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """
        Add columns introduced after a database was first created.

        CREATE TABLE IF NOT EXISTS silently does nothing when the table already
        exists, so new columns would be missing on any database created by an
        earlier version.
        """
        additions = {
            "members": [
                ("alt_keys", "TEXT"),
                ("preferred_name", "TEXT"),
                ("claims_paid", "INTEGER NOT NULL DEFAULT 0"),
            ],
            "merchants": [
                ("committee_counts", "TEXT"),
            ],
            "labeled_examples": [
                ("sample_weight", "REAL"),
            ],
            "terms": [
                ("locked", "INTEGER NOT NULL DEFAULT 0"),
                ("locked_at", "TEXT"),
                ("locked_by", "TEXT"),
                ("dues_rates", "TEXT"),
                ("dues_rates_verified", "INTEGER NOT NULL DEFAULT 0"),
                # Sponsorship dollar goal for the term. NULL = no goal set yet,
                # distinct from 0 (a goal of literally $0), same reasoning as
                # dues_rates being nullable above.
                ("sponsorship_goal", "REAL"),
                # Dues price change and late cutoff (domain/dues_checks.py).
                ("dues_early_price", "REAL"),
                ("dues_late_price", "REAL"),
                ("dues_increase_date", "TEXT"),
                ("dues_late_cutoff", "TEXT"),
            ],
            "events": [
                ("edited_in_app", "INTEGER NOT NULL DEFAULT 0"),
                ("edited_at", "TEXT"),
            ],
            "transactions": [
                # Who a sponsorship deposit was actually from. Free text: bank
                # descriptions never carry a company name (just "DEPOSIT
                # CORPORATE SPONSORSHIP REF12345"), so this has to be typed in
                # by whoever reconciles the deposit, not parsed from anything.
                ("sponsor_name", "TEXT"),
            ],
        }
        for table, columns in additions.items():
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    # --- Period locking (M10) ------------------------------------------------

    @staticmethod
    def _locked_ranges(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
        rows = connection.execute(
            "SELECT Semester, start_date, end_date FROM terms WHERE locked = 1"
        ).fetchall()
        return [(row["Semester"], row["start_date"], row["end_date"]) for row in rows]

    @staticmethod
    def _locked_term_for(date_text: str | None, ranges: list[tuple[str, str, str]]) -> str | None:
        """The locked term a date falls inside, if any."""
        if not date_text:
            return None
        stamp = str(date_text)[:10]
        for semester, start, end in ranges:
            if start and end and str(start)[:10] <= stamp <= str(end)[:10]:
                return semester
        return None

    def set_term_lock(self, term_id: str, locked: bool, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT Semester, locked FROM terms WHERE TermID = ?", (term_id,)
                ).fetchone()
                if row is None:
                    result.error = f"Term {term_id} does not exist."
                    return result
                if bool(row["locked"]) == locked:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE terms SET locked = ?, locked_at = CURRENT_TIMESTAMP, locked_by = ? "
                    "WHERE TermID = ?",
                    (1 if locked else 0, actor, term_id),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="lock" if locked else "unlock",
                    actor=actor,
                    field=term_id,
                    old_value="locked" if row["locked"] else "open",
                    new_value="locked" if locked else "open",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_term_dues_rates(
        self, term_id: str, rates: str, verified: bool, actor: str
    ) -> UpdateResult:
        """
        Record the dues rates a term charged.

        Audited like a lock change rather than written quietly: this decides
        which incoming payments count as dues income, so who set it and what it
        was before both need to survive.
        """
        result = UpdateResult()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT dues_rates, dues_rates_verified FROM terms WHERE TermID = ?",
                    (term_id,),
                ).fetchone()
                if row is None:
                    result.error = f"Term {term_id} does not exist."
                    return result

                previous = row["dues_rates"]
                was_verified = bool(row["dues_rates_verified"] or 0)
                if _normalized(previous) == _normalized(rates) and was_verified == verified:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE terms SET dues_rates = ?, dues_rates_verified = ? "
                    "WHERE TermID = ?",
                    (rates or None, 1 if verified else 0, term_id),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="dues_rates",
                    actor=actor,
                    field=term_id,
                    old_value=f"{previous or '(none)'}{'' if was_verified else ' (unconfirmed)'}",
                    new_value=f"{rates or '(none)'}{'' if verified else ' (unconfirmed)'}",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_term_sponsorship_goal(
        self, term_id: str, goal: float | None, actor: str
    ) -> UpdateResult:
        """
        Record the sponsorship dollar goal for a term.

        `goal=None` clears it (shows as "no goal set" rather than "$0 goal" --
        those mean different things to a treasurer). Audited the same way as
        `set_term_dues_rates`: this is a number a committee will be measured
        against, so who set it and what it replaced both need to survive.
        """
        result = UpdateResult()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT sponsorship_goal FROM terms WHERE TermID = ?", (term_id,)
                ).fetchone()
                if row is None:
                    result.error = f"Term {term_id} does not exist."
                    return result

                previous = row["sponsorship_goal"]
                if (previous is None and goal is None) or (
                    previous is not None and goal is not None and float(previous) == float(goal)
                ):
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE terms SET sponsorship_goal = ? WHERE TermID = ?",
                    (goal, term_id),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="sponsorship_goal",
                    actor=actor,
                    field=term_id,
                    old_value="(none)" if previous is None else f"${previous:,.2f}",
                    new_value="(none)" if goal is None else f"${goal:,.2f}",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_term_dues_schedule(
        self,
        term_id: str,
        early_price: float | None,
        late_price: float | None,
        increase_date: str | None,
        late_cutoff: str | None,
        actor: str,
    ) -> UpdateResult:
        """
        Record when a term's dues price goes up, and when a payment counts as late.

        Audited like `set_term_dues_rates`: these decide which payments get
        flagged as underpaid, so who set them and what they replaced must survive.
        """
        fields = ("dues_early_price", "dues_late_price", "dues_increase_date", "dues_late_cutoff")
        new = (early_price, late_price, increase_date, late_cutoff)
        result = UpdateResult()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    f"SELECT {', '.join(fields)} FROM terms WHERE TermID = ?", (term_id,)
                ).fetchone()
                if row is None:
                    result.error = f"Term {term_id} does not exist."
                    return result
                old = tuple(row[field] for field in fields)
                if old == new:
                    result.unchanged = 1
                    return result
                connection.execute(
                    f"UPDATE terms SET {', '.join(f'{f} = ?' for f in fields)} WHERE TermID = ?",
                    (*new, term_id),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="dues_schedule",
                    actor=actor,
                    field=term_id,
                    old_value=_describe_dues_schedule(*old),
                    new_value=_describe_dues_schedule(*new),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def set_transaction_sponsor_name(
        self, transaction_id: int, sponsor_name: str | None, actor: str
    ) -> UpdateResult:
        """
        Record who a sponsorship transaction was actually from.

        Same M10 period-locking rule as `update_transactions`: a closed
        term's transactions are read-only, and this is an edit to a
        transaction like any other, so it is refused the same way rather
        than quietly bypassing the lock through a side door.
        """
        result = UpdateResult()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT sponsor_name, transaction_date FROM transactions "
                    "WHERE transactionid = ?",
                    (transaction_id,),
                ).fetchone()
                if row is None:
                    result.failed.append(transaction_id)
                    result.error = f"Transaction {transaction_id} does not exist."
                    return result

                locked = self._locked_ranges(connection)
                closed = self._locked_term_for(row["transaction_date"], locked)
                if closed:
                    result.failed.append(transaction_id)
                    result.error = (
                        f"This transaction falls in {closed}, which is closed. "
                        f"Reopen the term before editing it."
                    )
                    return result

                previous = row["sponsor_name"]
                cleaned = (sponsor_name or "").strip() or None
                if (previous or None) == cleaned:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE transactions SET sponsor_name = ? WHERE transactionid = ?",
                    (cleaned, transaction_id),
                )
                self._audit(
                    connection,
                    transaction_id=transaction_id,
                    action="update",
                    actor=actor,
                    field="sponsor_name",
                    old_value=previous,
                    new_value=cleaned,
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- "This isn't ours" flags ---------------------------------------------

    def fetch_flags(self, committee_ids: tuple[int, ...] | None = None) -> pd.DataFrame:
        """
        Flags, open ones first. With `committee_ids`, only flags on charges that
        were booked to those committee lines -- what a VP is allowed to see.
        """
        order = "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, flag_id DESC"
        if committee_ids is None:
            return self._read(f"SELECT * FROM transaction_flags {order}")
        ids = [int(committee_id) for committee_id in committee_ids]
        if not ids:
            return pd.DataFrame()
        marks = ",".join("?" for _ in ids)
        return self._read(
            f"SELECT * FROM transaction_flags WHERE booked_to_committee_id IN ({marks}) {order}",
            tuple(ids),
        )

    def create_flag(
        self,
        transaction_id: int,
        allowed_committee_ids: tuple[int, ...],
        note: str,
        actor: str,
    ) -> UpdateResult:
        """
        Flag one charge as not belonging to the caller's committee.

        `allowed_committee_ids` is every budget line the caller owns. The check
        that the charge really is booked to one of them lives here, not only in
        the page, so a hand-made request for another committee's charge is
        refused at the data layer. Nothing about the charge itself is changed.
        """
        result = UpdateResult()
        cleaned, problem = flags_domain.clean_note(note)
        if problem:
            result.error = problem
            return result

        allowed = {int(committee_id) for committee_id in allowed_committee_ids}
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT budget_category FROM transactions WHERE transactionid = ?",
                    (int(transaction_id),),
                ).fetchone()
                if row is None:
                    result.error = f"Charge {transaction_id} does not exist."
                    return result
                booked_to = row["budget_category"]
                if booked_to is None or int(booked_to) not in allowed:
                    result.error = "That charge is not booked to your committee, so you cannot flag it."
                    return result

                already = connection.execute(
                    "SELECT 1 FROM transaction_flags WHERE transaction_id = ? AND status = 'open'",
                    (int(transaction_id),),
                ).fetchone()
                if already:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "INSERT INTO transaction_flags "
                    "(transaction_id, booked_to_committee_id, flagged_by, note) "
                    "VALUES (?, ?, ?, ?)",
                    (int(transaction_id), int(booked_to), actor, cleaned),
                )
                self._audit(
                    connection,
                    transaction_id=int(transaction_id),
                    action="flag",
                    actor=actor,
                    field="not our charge",
                    new_value=cleaned[:120],
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def resolve_flag(self, flag_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
        """Close an open flag as resolved or dismissed. The treasurer's call."""
        result = UpdateResult()
        if status not in flags_domain.CLOSING_STATUSES:
            result.error = f"Unknown status {status!r}."
            return result
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status, transaction_id FROM transaction_flags WHERE flag_id = ?",
                    (int(flag_id),),
                ).fetchone()
                if row is None:
                    result.error = f"Flag {flag_id} does not exist."
                    return result
                if row["status"] != flags_domain.OPEN:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE transaction_flags SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
                    "resolved_by = ?, resolution_note = ? WHERE flag_id = ? AND status = 'open'",
                    (status, actor, (note or "").strip() or None, int(flag_id)),
                )
                self._audit(
                    connection,
                    transaction_id=int(row["transaction_id"]),
                    action="flag_resolved",
                    actor=actor,
                    field=f"flag {flag_id}",
                    old_value=flags_domain.OPEN,
                    new_value=status,
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Model runs and spend reviews -----------------------------------------

    def fetch_llm_runs(self) -> pd.DataFrame:
        return self._read("SELECT * FROM llm_runs ORDER BY run_id DESC")

    def start_llm_run(
        self,
        *,
        purpose: str,
        model: str,
        actor: str,
        rows_planned: int,
        calls_planned: int,
        reserved_usd: float,
        stale_before: str,
        started_at: str,
    ) -> tuple[int | None, UpdateResult]:

        result = UpdateResult()
        try:
            with self._connect() as connection:
                # A run still "running" from before `stale_before` crashed.
                connection.execute(
                    "UPDATE llm_runs SET status = 'abandoned', "
                    "error = COALESCE(error, 'never finished; its reservation still counts') "
                    "WHERE status = 'running' AND started_at < ?",
                    (stale_before,),
                )
                try:
                    cursor = connection.execute(
                        "INSERT INTO llm_runs (purpose, model, status, started_by, started_at, "
                        "rows_planned, calls_planned, reserved_usd) "
                        "VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                        (
                            purpose, model, actor,
                            started_at,
                            int(rows_planned), int(calls_planned), float(reserved_usd),
                        ),
                    )
                except sqlite3.IntegrityError:
                    connection.commit()  # keep the stale-run cleanup
                    result.error = "Another model run is in progress. Try again in a few minutes."
                    return None, result
                connection.commit()
                result.updated = 1
                return int(cursor.lastrowid), result
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return None, result

    def finish_llm_run(
        self,
        *,
        run_id: int,
        status: str,
        calls_made: int,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        error: str | None,
    ) -> UpdateResult:
        from datetime import datetime, timezone

        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE llm_runs SET status = ?, finished_at = ?, calls_made = ?, "
                    "input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, "
                    "cache_write_tokens = ?, cost_usd = ?, error = ? WHERE run_id = ?",
                    (
                        status, datetime.now(timezone.utc).isoformat(), int(calls_made),
                        int(input_tokens), int(output_tokens), int(cache_read_tokens),
                        int(cache_write_tokens), float(cost_usd), error, int(run_id),
                    ),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def fetch_spend_reviews(self) -> pd.DataFrame:
        return self._read(
            "SELECT * FROM spend_reviews "
            "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, review_id DESC"
        )

    def insert_spend_reviews(self, reviews: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        columns = (
            "transaction_id", "booked_committee_id", "verdict", "suggested_committee_id",
            "confidence", "reason", "message_to_vp", "status", "run_id", "model",
            "prompt_version",
        )
        try:
            with self._connect() as connection:
                for review in reviews:
                    cursor = connection.execute(
                        f"INSERT OR IGNORE INTO spend_reviews ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        tuple(review.get(column) for column in columns),
                    )
                    if cursor.rowcount:
                        result.updated += 1
                    else:
                        result.unchanged += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def resolve_spend_review(
        self, review_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        result = UpdateResult()
        if status not in ("resolved", "dismissed"):
            result.error = f"Unknown status {status!r}."
            return result
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE spend_reviews SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
                    "resolved_by = ?, resolution_note = ? WHERE review_id = ? AND status = 'open'",
                    (status, actor, (note or "").strip() or None, int(review_id)),
                )
                connection.commit()
                if cursor.rowcount:
                    result.updated = 1
                else:
                    result.unchanged = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Committee plans and budget requests ----------------------------------

    def _scoped_read(
        self, table: str, committee_ids: tuple[int, ...] | None, semester: str | None, order: str
    ) -> pd.DataFrame:
        clauses, params = [], []
        if committee_ids is not None:
            ids = [int(committee_id) for committee_id in committee_ids]
            if not ids:
                return self._read(f"SELECT * FROM {table} WHERE 0")
            clauses.append(f"committee_id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if semester is not None:
            clauses.append("semester = ?")
            params.append(semester)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._read(f"SELECT * FROM {table} {where} ORDER BY {order}", tuple(params))

    def fetch_plan_lines(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        return self._scoped_read(
            "committee_plan_lines", committee_ids, semester, "committee_id, amount DESC, purpose"
        )

    def replace_plan_lines(
        self,
        committee_id: int,
        semester: str,
        lines: list[dict],
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        """The page has already cleaned `lines` (`committee_plan.clean_lines`)."""
        result = UpdateResult()
        if int(committee_id) not in {int(i) for i in allowed_committee_ids}:
            result.error = "You can only plan for your own committee."
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                before = connection.execute(
                    "SELECT COUNT(*) FROM committee_plan_lines WHERE committee_id = ? AND semester = ?",
                    (int(committee_id), semester),
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM committee_plan_lines WHERE committee_id = ? AND semester = ?",
                    (int(committee_id), semester),
                )
                for line in lines:
                    connection.execute(
                        "INSERT INTO committee_plan_lines (committee_id, semester, purpose, amount, "
                        "note, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            int(committee_id), semester, line["purpose"], float(line["amount"]),
                            line.get("note"), actor,
                        ),
                    )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="plan",
                    actor=actor,
                    field=f"committee {int(committee_id)} / {semester}",
                    old_value=f"{before} line(s)",
                    new_value=f"{len(lines)} line(s)",
                )
                connection.commit()
                result.updated = len(lines) or 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def fetch_planned_events(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        return self._scoped_read(
            "planned_events", committee_ids, semester, "committee_id, event_date IS NULL, event_date, title"
        )

    def replace_planned_events(
        self,
        committee_id: int,
        semester: str,
        events: list[dict],
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        """Make `events` the committee's whole event plan for `semester` (cleaned by the page)."""
        result = UpdateResult()
        if int(committee_id) not in {int(i) for i in allowed_committee_ids}:
            result.error = "You can only plan for your own committee."
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                before = connection.execute(
                    "SELECT COUNT(*) FROM planned_events WHERE committee_id = ? AND semester = ?",
                    (int(committee_id), semester),
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM planned_events WHERE committee_id = ? AND semester = ?",
                    (int(committee_id), semester),
                )
                for event in events:
                    connection.execute(
                        "INSERT INTO planned_events (committee_id, semester, event_date, title, kind, "
                        "estimated_cost, expected_attendance, note, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            int(committee_id), semester, event.get("event_date"), event["title"],
                            event.get("kind"), float(event.get("estimated_cost") or 0),
                            None if event.get("expected_attendance") is None else int(event["expected_attendance"]),
                            event.get("note"), actor,
                        ),
                    )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="event plan",
                    actor=actor,
                    field=f"committee {int(committee_id)} / {semester}",
                    old_value=f"{before} event(s)",
                    new_value=f"{len(events)} event(s)",
                )
                connection.commit()
                result.updated = len(events) or 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def fetch_budget_requests(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        return self._scoped_read(
            "budget_requests", committee_ids, semester, "semester, committee_id"
        )

    def save_budget_request(
        self,
        committee_id: int,
        semester: str,
        amount: float,
        reason: str,
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        result = UpdateResult()
        if int(committee_id) not in {int(i) for i in allowed_committee_ids}:
            result.error = "You can only send a request for your own committee."
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                previous = connection.execute(
                    "SELECT amount FROM budget_requests WHERE committee_id = ? AND semester = ?",
                    (int(committee_id), semester),
                ).fetchone()
                connection.execute(
                    "INSERT INTO budget_requests (committee_id, semester, amount, reason, requested_by) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (committee_id, semester) DO UPDATE SET "
                    "amount = excluded.amount, reason = excluded.reason, "
                    "requested_by = excluded.requested_by, requested_at = CURRENT_TIMESTAMP",
                    (int(committee_id), semester, float(amount), reason, actor),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="budget request",
                    actor=actor,
                    field=f"committee {int(committee_id)} / {semester}",
                    old_value=None if previous is None else previous["amount"],
                    new_value=float(amount),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def _read(self, query: str, params: tuple = ()) -> pd.DataFrame:
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        if not rows:
            return pd.DataFrame(columns=_columns_for(query))
        return pd.DataFrame([dict(row) for row in rows])

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        transaction_id: int | None,
        action: str,
        actor: str,
        field: str | None = None,
        old_value: object = None,
        new_value: object = None,
    ) -> None:
        connection.execute(
            "INSERT INTO transaction_audit "
            "(transaction_id, action, field, old_value, new_value, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                transaction_id,
                action,
                field,
                None if old_value is None else str(old_value),
                None if new_value is None else str(new_value),
                actor,
            ),
        )

    # --- reads ---------------------------------------------------------------

    def fetch_committees(self) -> pd.DataFrame:
        return self._read("SELECT * FROM committees ORDER BY CommitteeID")

    def fetch_terms(self) -> pd.DataFrame:
        return self._read("SELECT * FROM terms ORDER BY start_date")

    def fetch_budgets(self) -> pd.DataFrame:
        return self._read("SELECT * FROM committeebudgets ORDER BY termid, committeeid")

    def fetch_transactions(self) -> pd.DataFrame:
        frame = self._read("SELECT * FROM transactions ORDER BY transaction_date, transactionid")
        if not frame.empty:
            frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], errors="coerce")
            frame["budget_category"] = frame["budget_category"].astype("Int64")
        return frame

    def fetch_uploaded_files(self) -> pd.DataFrame:
        return self._read("SELECT * FROM uploaded_files ORDER BY uploaded_at DESC")

    def fetch_merchants(self) -> pd.DataFrame:
        return self._read("SELECT * FROM merchants ORDER BY hit_count DESC")

    def fetch_members(self, term_id: str | None = None) -> pd.DataFrame:
        if term_id:
            return self._read(
                "SELECT * FROM members WHERE term_id = ? ORDER BY full_name", (term_id,)
            )
        return self._read("SELECT * FROM members ORDER BY term_id, full_name")

    def replace_members(
        self, term_id: str, members: list[dict], source_file: str, actor: str
    ) -> UpdateResult:
        """
        Replace one term's roster wholesale.

        Replace rather than merge: a membership export is a snapshot of who is
        on the roster *now*, so a name absent from the new file is a name that
        left. Merging would make removals impossible and quietly inflate the
        denominator of every collection-rate figure from then on.

        Both statements run in one transaction, so a failure part-way cannot
        leave the term with no roster at all.
        """
        result = UpdateResult()
        if not term_id:
            result.error = "A term is required to store a roster."
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                connection.execute("DELETE FROM members WHERE term_id = ?", (term_id,))
                connection.executemany(
                    "INSERT INTO members "
                    "(term_id, full_name, match_key, alt_keys, preferred_name, "
                    " claims_paid, email, ufid, committee_id, notes, source_file, "
                    " uploaded_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            term_id,
                            member.get("full_name"),
                            member.get("match_key"),
                            ",".join(member.get("alt_keys") or ()) or None,
                            member.get("preferred_name") or None,
                            1 if member.get("claims_paid") else 0,
                            member.get("email") or None,
                            member.get("ufid") or None,
                            member.get("committee_id"),
                            member.get("notes") or None,
                            source_file,
                            actor,
                        )
                        for member in members
                    ],
                )
                connection.commit()
            result.updated = len(members)
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def add_member_alias(
        self, term_id: str, match_key: str, alias_key: str, actor: str
    ) -> UpdateResult:
        """
        Teach the roster a second name for one member, permanently.

        This is how a "likely match" in the Roster page becomes a real one: a
        treasurer confirms that "zachariah calloway" is the same person as
        "zach calloway", and every future statement matches directly instead of
        landing in "likely matches" again. Same shape as "Remember this
        merchant" on the Review Queue -- a human's one-time confirmation turned
        into a rule the next import benefits from.

        Appends rather than replaces `alt_keys`, so confirming one alias cannot
        erase another confirmed earlier. A no-op, not an error, if the alias is
        already there or is the member's own primary key.
        """
        result = UpdateResult()
        if not (term_id and match_key and alias_key):
            result.error = "Term, member and alias are all required."
            return result
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT alt_keys FROM members WHERE term_id = ? AND match_key = ?",
                    (term_id, match_key),
                ).fetchone()
                if row is None:
                    result.error = f"No member {match_key!r} on the {term_id} roster."
                    return result
                existing = {
                    part.strip()
                    for part in str(row["alt_keys"] or "").split(",")
                    if part.strip()
                }
                if alias_key == match_key or alias_key in existing:
                    result.unchanged = 1
                    return result
                existing.add(alias_key)
                connection.execute(
                    "UPDATE members SET alt_keys = ? WHERE term_id = ? AND match_key = ?",
                    (",".join(sorted(existing)), term_id, match_key),
                )
                connection.commit()
            result.updated = 1
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    # --- Events (domain/events.py) --------------------------------------------

    EVENT_FIELDS = (
        "term_id", "event_date", "title", "start_time", "end_time", "location",
        "owner", "audience", "status", "committee_id", "attendance",
        "attendance_source", "description", "source", "calendar_id",
    )

    def fetch_events(self, term_id: str | None = None) -> pd.DataFrame:
        if term_id:
            return self._read(
                "SELECT * FROM events WHERE term_id = ? ORDER BY event_date, start_time", (term_id,)
            )
        return self._read("SELECT * FROM events ORDER BY event_date, start_time")

    def sync_events(self, events: list[dict], actor: str) -> UpdateResult:
        """
        Add new events and update known ones, matched on external_key.

        Never deletes: an event dropped from the calendar may already have
        spending linked to it, and silently losing that link would undo a
        person's confirmation. Attendance already recorded is kept when the
        incoming row has none (the schedule sheets rarely carry it).
        """
        result = UpdateResult()
        fields = self.EVENT_FIELDS
        assignments = ", ".join(
            f"{f} = COALESCE(excluded.{f}, events.{f})" if f in ("attendance", "attendance_source")
            else f"{f} = excluded.{f}"
            for f in fields
        )
        try:
            with self._connect() as connection:
                known = {
                    row["external_key"]
                    for row in connection.execute("SELECT external_key FROM events")
                }
                connection.executemany(
                    f"INSERT INTO events (external_key, {', '.join(fields)}, synced_by) "
                    f"VALUES ({', '.join('?' * (len(fields) + 2))}) "
                    f"ON CONFLICT(external_key) DO UPDATE SET {assignments}, "
                    "synced_by = excluded.synced_by, synced_at = CURRENT_TIMESTAMP, "
                    "edited_in_app = 0",
                    [
                        (event["external_key"], *(event.get(f) for f in fields), actor)
                        for event in events
                    ],
                )
                connection.commit()
            keys = {event["external_key"] for event in events}
            result.updated = len(keys - known)
            result.unchanged = len(keys & known)
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def set_event_attendance(
        self, event_id: int, attendance: int | None, source: str, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE events SET attendance = ?, attendance_source = ? WHERE event_id = ?",
                    (attendance, source if attendance is not None else None, event_id),
                )
                connection.commit()
            result.updated = cursor.rowcount
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def fetch_event_links(self) -> pd.DataFrame:
        return self._read("SELECT * FROM event_links ORDER BY linked_at")

    def link_transactions_to_events(self, pairs: list[tuple[int, int]], actor: str) -> UpdateResult:
        """
        Record that each (transaction_id, event_id) paid for that event.

        Audited per transaction, because which event a purchase belongs to is
        what a committee's per-event costs are later built from.
        """
        result = UpdateResult()
        try:
            with self._connect() as connection:
                for transaction_id, event_id in pairs:
                    title = connection.execute(
                        "SELECT title, event_date FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    if title is None:
                        result.failed.append(int(transaction_id))
                        continue
                    before = connection.execute(
                        "SELECT e.title FROM event_links l JOIN events e USING (event_id) "
                        "WHERE l.transaction_id = ?",
                        (transaction_id,),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO event_links (transaction_id, event_id, linked_by) VALUES (?, ?, ?) "
                        "ON CONFLICT(transaction_id) DO UPDATE SET event_id = excluded.event_id, "
                        "linked_by = excluded.linked_by, linked_at = CURRENT_TIMESTAMP",
                        (int(transaction_id), int(event_id), actor),
                    )
                    self._audit(
                        connection,
                        transaction_id=int(transaction_id),
                        action="event_link",
                        actor=actor,
                        field="event",
                        old_value=before["title"] if before else None,
                        new_value=f"{title['title']} ({title['event_date']})",
                    )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def unlink_transaction_from_event(self, transaction_id: int, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                before = connection.execute(
                    "SELECT e.title FROM event_links l JOIN events e USING (event_id) "
                    "WHERE l.transaction_id = ?",
                    (transaction_id,),
                ).fetchone()
                if before is None:
                    result.unchanged = 1
                    return result
                connection.execute("DELETE FROM event_links WHERE transaction_id = ?", (transaction_id,))
                self._audit(
                    connection, transaction_id=int(transaction_id), action="event_link",
                    actor=actor, field="event", old_value=before["title"], new_value=None,
                )
                connection.commit()
            result.updated = 1
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def add_event(self, event: dict, actor: str) -> UpdateResult:
        """
        Add one event typed in the app. `updated` is the new event_id. Refused
        when an event with the same day and title already exists.
        """
        result = UpdateResult()
        fields = self.EVENT_FIELDS
        try:
            with self._connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM events WHERE external_key = ?", (event["external_key"],)
                ).fetchone():
                    result.error = "There is already an event with that title on that day."
                    return result
                cursor = connection.execute(
                    f"INSERT INTO events (external_key, {', '.join(fields)}, synced_by, "
                    "edited_in_app, edited_at) "
                    f"VALUES ({', '.join('?' * (len(fields) + 2))}, 1, CURRENT_TIMESTAMP)",
                    (event["external_key"], *(event.get(f) for f in fields), actor),
                )
                connection.commit()
            result.updated = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    EDITABLE_EVENT_FIELDS = (
        "event_date", "title", "start_time", "end_time", "location", "owner",
        "audience", "status", "committee_id", "description", "term_id",
    )

    def update_event(self, event_id: int, changes: dict, actor: str) -> UpdateResult:
        """
        Edit or move an event. The day|title key follows the change, so a later
        sync of the same event from Google Calendar updates it rather than
        adding a copy.
        """
        result = UpdateResult()
        changes = {k: v for k, v in changes.items() if k in self.EDITABLE_EVENT_FIELDS}
        if not changes:
            result.unchanged = 1
            return result
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT event_date, title FROM events WHERE event_id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    result.error = "That event no longer exists."
                    return result
                day = str(changes.get("event_date", row["event_date"]))[:10]
                title = str(changes.get("title", row["title"]))
                key = f"{day}|{re.sub(r'[^a-z0-9]+', ' ', title.lower()).strip()}"
                clash = connection.execute(
                    "SELECT 1 FROM events WHERE external_key = ? AND event_id <> ?", (key, event_id)
                ).fetchone()
                if clash:
                    result.error = "There is already an event with that title on that day."
                    return result
                assignments = ", ".join(f"{name} = ?" for name in changes)
                connection.execute(
                    f"UPDATE events SET {assignments}, external_key = ?, edited_in_app = 1, "
                    "edited_at = CURRENT_TIMESTAMP, synced_by = ? WHERE event_id = ?",
                    (*changes.values(), key, actor, event_id),
                )
                connection.commit()
            result.updated = 1
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def delete_event(self, event_id: int, actor: str) -> UpdateResult:
        """Remove an event and its typed costs. Refused while purchases are linked to it."""
        result = UpdateResult()
        try:
            with self._connect() as connection:
                linked = connection.execute(
                    "SELECT COUNT(*) FROM event_links WHERE event_id = ?", (event_id,)
                ).fetchone()[0]
                if linked:
                    result.error = (
                        f"{linked} purchase(s) are linked to this event. Move or unlink them first."
                    )
                    return result
                connection.execute("DELETE FROM event_costs WHERE event_id = ?", (event_id,))
                cursor = connection.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
                connection.commit()
            result.updated = cursor.rowcount
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def fetch_event_costs(self) -> pd.DataFrame:
        return self._read("SELECT * FROM event_costs ORDER BY event_id, kind, added_at")

    def add_event_cost(
        self, event_id: int, kind: str, label: str, amount: float, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        if kind not in ("estimate", "manual"):
            result.error = f"Unknown cost kind: {kind}"
            return result
        try:
            with self._connect() as connection:
                if connection.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone() is None:
                    result.error = "That event no longer exists."
                    return result
                cursor = connection.execute(
                    "INSERT INTO event_costs (event_id, kind, label, amount, added_by) VALUES (?, ?, ?, ?, ?)",
                    (int(event_id), kind, label.strip() or ("Estimate" if kind == "estimate" else "Cost"),
                     round(float(amount), 2), actor),
                )
                connection.commit()
            result.updated = int(cursor.lastrowid)
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def delete_event_cost(self, cost_id: int, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM event_costs WHERE cost_id = ?", (int(cost_id),))
                connection.commit()
            result.updated = cursor.rowcount
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    # --- Module M22: VP portal access ----------------------------------------

    def fetch_profiles(self) -> pd.DataFrame:
        return self._read("SELECT * FROM profiles ORDER BY committee_id, display_name")

    def fetch_profile(self, email: str) -> dict | None:
        """
        One person's access record, or None if they have never been assigned one.

        A lowercase, case-folded lookup: Google's identity token capitalises
        emails the way the account was originally typed, which does not always
        match how a treasurer typed it into the roster.
        """
        if not email:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM profiles WHERE lower(email) = lower(?)", (email,)
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_profile(
        self,
        email: str,
        role: str,
        committee_id: int | None,
        display_name: str,
        actor: str,
    ) -> UpdateResult:
        """
        Create or update one person's committee/role assignment.

        This is what gates a real login, not a display preference -- a wrong
        role here is a real access grant, which is why it goes through the
        audited `UpdateResult` path like a term lock or a dues-rate change
        rather than a plain write.
        """
        result = UpdateResult()
        email = normalize_email(email)  # stored lowercase, like the Supabase backend
        if not email:
            result.error = "An email is required."
            return result
        if role not in {"member", "officer", "treasurer", "president", "admin"}:
            result.error = f"Unknown role {role!r}."
            return result
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO profiles (email, display_name, role, committee_id, created_by) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (email) DO UPDATE SET "
                    "  display_name = excluded.display_name, "
                    "  role = excluded.role, "
                    "  committee_id = excluded.committee_id",
                    (email, display_name or None, role, committee_id, actor),
                )
                connection.commit()
            result.updated = 1
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def remove_profile(self, email: str) -> UpdateResult:
        result = UpdateResult()
        if not email:
            result.error = "An email is required."
            return result
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM profiles WHERE lower(email) = lower(?)", (email,)
                )
                connection.commit()
            result.updated = cursor.rowcount
        except sqlite3.Error as exc:
            result.error = str(exc)
        return result

    def fetch_audit(self, limit: int = 500) -> pd.DataFrame:
        return self._read(
            "SELECT * FROM transaction_audit ORDER BY audit_id DESC LIMIT ?", (limit,)
        )

    def fetch_statement_balances(self) -> pd.DataFrame:
        return self._read("SELECT * FROM statement_balances ORDER BY account, period_start")

    def is_file_uploaded(self, file_name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM uploaded_files WHERE file_name = ?", (file_name,)
            ).fetchone()
        return row is not None

    def fetch_labeled_examples(self) -> pd.DataFrame:
        """M19: every label a human has assigned, from any source."""
        return self._read("SELECT * FROM labeled_examples ORDER BY labeled_at, label_id")

    def insert_labeled_examples(self, examples: list[dict], actor: str) -> UpdateResult:
        """
        Record human labels. Idempotent on `natural_key`, so re-importing the
        same ledger adds nothing rather than inflating the training set with
        duplicates -- which would silently over-weight whatever was re-imported.
        """
        result = UpdateResult()
        if not examples:
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for example in examples:
                    cursor = connection.execute(
                        """
                        INSERT INTO labeled_examples (
                            source, source_ref, era, transaction_id, transaction_date,
                            amount, details, account, committee_id, purpose,
                            model_committee, model_confidence, model_source,
                            sample_weight, labeled_by, natural_key
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT (natural_key) DO NOTHING
                        """,
                        (
                            example.get("source", "review"),
                            example.get("source_ref"),
                            example.get("era"),
                            example.get("transaction_id"),
                            example.get("transaction_date"),
                            example.get("amount"),
                            example.get("details"),
                            example.get("account"),
                            int(example["committee_id"]),
                            example.get("purpose"),
                            example.get("model_committee"),
                            example.get("model_confidence"),
                            example.get("model_source"),
                            example.get("sample_weight"),
                            example.get("labeled_by", actor),
                            example.get("natural_key"),
                        ),
                    )
                    if cursor.rowcount:
                        result.updated += 1
                    else:
                        result.unchanged += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- writes --------------------------------------------------------------

    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt:
        """
        FINDING F4. Transactions and the uploaded_files marker are written inside
        one transaction. Either both land or neither does, so a partial failure
        can no longer leave a statement importable a second time.
        """
        receipt = UploadReceipt(file_name=file_name)
        if not records:
            receipt.error = "No records to insert."
            return receipt

        try:
            with self._connect() as connection:
                connection.execute("BEGIN")

                # M10: refuse the whole import if any row lands in a closed
                # period. Rejecting the batch rather than filtering it keeps the
                # statement and the ledger in agreement -- a partial import would
                # silently drop rows the treasurer believes were loaded.
                locked = self._locked_ranges(connection)
                if locked:
                    blocked = {
                        term
                        for record in records
                        if (term := self._locked_term_for(record.get("transaction_date"), locked))
                    }
                    if blocked:
                        receipt.error = (
                            f"{', '.join(sorted(blocked))} is closed. Reopen the term on "
                            f"the Treasury page to import transactions dated inside it."
                        )
                        return receipt

                inserted = 0
                skipped = 0
                for record in records:
                    # Targeted conflict clause, not a blanket INSERT OR IGNORE.
                    #
                    # OR IGNORE swallows *every* constraint violation, including
                    # foreign-key failures -- so a transaction referencing a
                    # committee that does not exist would vanish silently and the
                    # upload would report success. Naming the natural_key index as
                    # the conflict target means duplicates are skipped while any
                    # other violation raises and rolls the whole import back.
                    cursor = connection.execute(
                        "INSERT INTO transactions "
                        "(transaction_date, amount, details, budget_category, purpose, "
                        " account, source_file, natural_key) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (natural_key) WHERE natural_key IS NOT NULL "
                        "DO NOTHING",
                        (
                            record.get("transaction_date"),
                            record.get("amount"),
                            record.get("details"),
                            record.get("budget_category"),
                            record.get("purpose"),
                            record.get("account"),
                            file_name,
                            record.get("natural_key"),
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        self._audit(
                            connection,
                            transaction_id=cursor.lastrowid,
                            action="insert",
                            actor=actor,
                            new_value=f"{record.get('transaction_date')} "
                            f"{record.get('amount')} {record.get('details')}",
                        )
                    else:
                        skipped += 1

                # Accumulate rather than replace. A re-import legitimately adds
                # zero rows, and OR REPLACE would rewrite the count to 0 --
                # making a fully-imported statement look like it never loaded.
                connection.execute(
                    "INSERT INTO uploaded_files (file_name, row_count, uploaded_by) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT (file_name) DO UPDATE SET "
                    "  row_count   = uploaded_files.row_count + excluded.row_count, "
                    "  uploaded_at = CURRENT_TIMESTAMP",
                    (file_name, inserted, actor),
                )
                connection.commit()

            receipt.inserted = inserted
            receipt.duplicates_skipped = skipped
            receipt.committed = True
        except sqlite3.Error as exc:
            receipt.error = f"{type(exc).__name__}: {exc}"
        return receipt

    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult:
        """
        FINDING F6. The original compared an int against a display string, so the
        change test was always true and every visible row was written back one
        HTTP request at a time. Only genuinely changed fields are written here,
        and the whole set goes in one database transaction.
        """
        result = UpdateResult()
        if not changes:
            return result

        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                locked = self._locked_ranges(connection)
                for change in changes:
                    row = connection.execute(
                        "SELECT purpose, budget_category, transaction_date FROM transactions "
                        "WHERE transactionid = ?",
                        (change.transaction_id,),
                    ).fetchone()
                    if row is None:
                        result.failed.append(change.transaction_id)
                        continue

                    # M10: a closed period is read-only.
                    closed = self._locked_term_for(row["transaction_date"], locked)
                    if closed:
                        result.failed.append(change.transaction_id)
                        result.error = (
                            f"Some transactions fall in {closed}, which is closed. "
                            f"Reopen the term before editing them."
                        )
                        continue

                    current = {"purpose": row["purpose"], "budget_category": row["budget_category"]}
                    desired = change.as_values()
                    dirty = {
                        field: value
                        for field, value in desired.items()
                        if _normalized(value) != _normalized(current[field])
                    }
                    if not dirty:
                        result.unchanged += 1
                        continue

                    assignments = ", ".join(f"{field} = ?" for field in dirty)
                    connection.execute(
                        f"UPDATE transactions SET {assignments} WHERE transactionid = ?",
                        (*dirty.values(), change.transaction_id),
                    )
                    for field, value in dirty.items():
                        self._audit(
                            connection,
                            transaction_id=change.transaction_id,
                            action="update",
                            actor=actor,
                            field=field,
                            old_value=current[field],
                            new_value=value,
                        )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for committee_id, amount in allocations.items():
                    existing = connection.execute(
                        "SELECT budget_amount FROM committeebudgets "
                        "WHERE termid = ? AND committeeid = ?",
                        (term_id, committee_id),
                    ).fetchone()
                    previous = existing["budget_amount"] if existing else None
                    if previous is not None and float(previous) == float(amount):
                        result.unchanged += 1
                        continue

                    connection.execute(
                        "INSERT INTO committeebudgets (termid, committeeid, budget_amount) "
                        "VALUES (?, ?, ?) "
                        "ON CONFLICT (termid, committeeid) "
                        "DO UPDATE SET budget_amount = excluded.budget_amount",
                        (term_id, committee_id, float(amount)),
                    )
                    self._audit(
                        connection,
                        transaction_id=None,
                        action="budget",
                        actor=actor,
                        field=f"{term_id}/committee {committee_id}",
                        old_value=previous,
                        new_value=amount,
                    )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT 1 FROM terms WHERE TermID = ?", (term_id,)
                ).fetchone()
                if existing:
                    result.error = f"Term {term_id} already exists."
                    return result
                connection.execute(
                    "INSERT INTO terms (TermID, Semester, start_date, end_date) "
                    "VALUES (?, ?, ?, ?)",
                    (term_id, semester, start_date, end_date),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="term",
                    actor=actor,
                    field=term_id,
                    new_value=f"{semester} {start_date}..{end_date}",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult:
        result = UpdateResult()
        if not rules:
            return result
        try:
            with self._connect() as connection:
                connection.execute("BEGIN")
                for rule in rules:
                    connection.execute(
                        "INSERT INTO merchants "
                        "(merchant_key, canonical_name, committee_id, purpose, hit_count, "
                        " source, committee_counts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (merchant_key) DO UPDATE SET "
                        "  canonical_name   = excluded.canonical_name, "
                        "  committee_id     = excluded.committee_id, "
                        "  purpose          = excluded.purpose, "
                        "  hit_count        = merchants.hit_count + 1, "
                        "  committee_counts = excluded.committee_counts, "
                        "  updated_at       = CURRENT_TIMESTAMP",
                        (
                            rule["merchant_key"],
                            rule.get("canonical_name"),
                            rule.get("committee_id"),
                            rule.get("purpose"),
                            rule.get("hit_count", 1),
                            rule.get("source", "learned"),
                            rule.get("committee_counts"),
                        ),
                    )
                    result.updated += 1
                connection.commit()
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO statement_balances "
                    "(account, period_start, period_end, opening_balance, closing_balance, source_file) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (account, period_start, period_end) DO UPDATE SET "
                    "  opening_balance = excluded.opening_balance, "
                    "  closing_balance = excluded.closing_balance, "
                    "  source_file     = excluded.source_file",
                    (
                        balance["account"],
                        balance["period_start"],
                        balance["period_end"],
                        float(balance["opening_balance"]),
                        float(balance["closing_balance"]),
                        balance.get("source_file"),
                    ),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Reimbursements (M8) -------------------------------------------------

    def fetch_reimbursements(self) -> pd.DataFrame:
        return self._read(
            "SELECT * FROM reimbursements ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
            "submitted_at DESC"
        )

    def create_reimbursement(self, request: dict, actor: str) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO reimbursements "
                    "(requester, committee_id, amount, description, incurred_on, "
                    " status, receipt_id) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        request["requester"],
                        request.get("committee_id"),
                        float(request["amount"]),
                        request.get("description"),
                        request.get("incurred_on"),
                        request.get("receipt_id"),
                    ),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="reimbursement_request",
                    actor=actor,
                    field=f"request {cursor.lastrowid}",
                    new_value=f"{request['requester']} ${float(request['amount']):.2f}",
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def decide_reimbursement(
        self, request_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        result = UpdateResult()
        if status not in {"approved", "rejected", "paid", "pending"}:
            result.error = f"Unknown status {status!r}."
            return result
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM reimbursements WHERE request_id = ?", (request_id,)
                ).fetchone()
                if row is None:
                    result.error = f"Request {request_id} does not exist."
                    return result
                if row["status"] == status:
                    result.unchanged = 1
                    return result

                connection.execute(
                    "UPDATE reimbursements SET status = ?, decided_at = CURRENT_TIMESTAMP, "
                    "decided_by = ?, decision_note = ? WHERE request_id = ?",
                    (status, actor, note or None, request_id),
                )
                self._audit(
                    connection,
                    transaction_id=None,
                    action="reimbursement_decision",
                    actor=actor,
                    field=f"request {request_id}",
                    old_value=row["status"],
                    new_value=status,
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    def link_reimbursement_to_transaction(
        self, request_id: int, transaction_id: int, actor: str
    ) -> UpdateResult:
        result = UpdateResult()
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE reimbursements SET matched_transaction_id = ?, "
                    "status = 'paid', decided_at = CURRENT_TIMESTAMP, decided_by = ? "
                    "WHERE request_id = ?",
                    (transaction_id, actor, request_id),
                )
                self._audit(
                    connection,
                    transaction_id=transaction_id,
                    action="reimbursement_matched",
                    actor=actor,
                    field=f"request {request_id}",
                    new_value=str(transaction_id),
                )
                connection.commit()
                result.updated = 1
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # --- Receipts (M9) -------------------------------------------------------

    def fetch_receipts(self) -> pd.DataFrame:
        return self._read("SELECT * FROM receipts ORDER BY uploaded_at DESC")

    def store_receipt(self, receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
        """Record receipt metadata. The file itself is written by domain.receipts."""
        result = UpdateResult()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO receipts "
                    "(file_name, stored_path, content_type, byte_size, uploaded_by, "
                    " transaction_id, request_id, vendor, amount, receipt_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt["file_name"],
                        receipt["stored_path"],
                        receipt.get("content_type"),
                        receipt.get("byte_size"),
                        actor,
                        receipt.get("transaction_id"),
                        receipt.get("request_id"),
                        receipt.get("vendor"),
                        receipt.get("amount"),
                        receipt.get("receipt_date"),
                    ),
                )
                receipt_id = cursor.lastrowid
                self._audit(
                    connection,
                    transaction_id=receipt.get("transaction_id"),
                    action="receipt_attached",
                    actor=actor,
                    field=receipt["file_name"],
                    new_value=str(receipt_id),
                )
                connection.commit()
                result.updated = 1
                return receipt_id, result
        except sqlite3.Error as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return None, result

    # --- sandbox-only --------------------------------------------------------

    def reset(self) -> None:
        """Drop everything and rebuild. Sandbox convenience, no production analogue."""
        with self._connect() as connection:
            tables = [
                "spend_reviews", "llm_runs",
                "committee_plan_lines", "budget_requests", "planned_events",
                "transaction_flags", "event_links",  # before transactions, which they reference
                "receipts", "reimbursements", "transaction_audit", "statement_balances",
                "merchants", "uploaded_files", "transactions", "committeebudgets",
                "terms", "committees",
            ]
            for table in tables:
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.commit()
        self._ensure_schema()


def _normalized(value: object) -> object:
    """Treat None, '' and whitespace as the same absent value when diffing."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _columns_for(query: str) -> list[str]:
    """Column names for an empty result, so downstream code sees a stable shape."""
    known = {
        "committees": ["CommitteeID", "Committee_Name", "Committee_Type"],
        "terms": [
            "TermID", "Semester", "start_date", "end_date", "locked", "locked_at",
            "locked_by", "dues_rates", "dues_rates_verified",
        ],
        "reimbursements": [
            "request_id", "requester", "committee_id", "amount", "description",
            "incurred_on", "status", "submitted_at", "decided_at", "decided_by",
            "decision_note", "matched_transaction_id", "receipt_id",
        ],
        "receipts": [
            "receipt_id", "file_name", "stored_path", "content_type", "byte_size",
            "uploaded_by", "uploaded_at", "transaction_id", "request_id",
            "vendor", "amount", "receipt_date",
        ],
        "committeebudgets": ["committeebudgetid", "termid", "committeeid", "budget_amount"],
        "transactions": [
            "transactionid", "transaction_date", "amount", "details",
            "budget_category", "purpose", "account", "source_file",
            "natural_key", "created_at",
        ],
        "uploaded_files": ["id", "file_name", "uploaded_at", "row_count", "uploaded_by"],
        "transaction_audit": [
            "audit_id", "transaction_id", "action", "field",
            "old_value", "new_value", "actor", "changed_at",
        ],
        "merchants": [
            "merchant_id", "merchant_key", "canonical_name",
            "committee_id", "purpose", "hit_count", "source", "updated_at",
        ],
        "statement_balances": [
            "id", "account", "period_start", "period_end",
            "opening_balance", "closing_balance", "source_file", "recorded_at",
        ],
        "llm_runs": [
            "run_id", "purpose", "model", "status", "started_by", "started_at",
            "finished_at", "rows_planned", "calls_planned", "calls_made",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reserved_usd", "cost_usd", "error",
        ],
        "spend_reviews": [
            "review_id", "transaction_id", "booked_committee_id", "verdict",
            "suggested_committee_id", "confidence", "reason", "message_to_vp",
            "status", "run_id", "model", "prompt_version", "reviewed_at",
            "resolved_at", "resolved_by", "resolution_note",
        ],
        "committee_plan_lines": [
            "line_id", "committee_id", "semester", "purpose", "amount", "note",
            "updated_by", "updated_at",
        ],
        "events": [
            "event_id", "external_key", "term_id", "event_date", "title", "start_time",
            "end_time", "location", "owner", "audience", "status", "committee_id",
            "attendance", "attendance_source", "description", "source", "calendar_id",
            "synced_by", "synced_at", "edited_in_app", "edited_at",
        ],
        "event_links": ["transaction_id", "event_id", "linked_by", "linked_at"],
        "event_costs": ["cost_id", "event_id", "kind", "label", "amount", "added_by", "added_at"],
        "budget_requests": [
            "request_id", "committee_id", "semester", "amount", "reason",
            "requested_by", "requested_at",
        ],
        "planned_events": [
            "planned_event_id", "committee_id", "semester", "event_date", "title", "kind",
            "estimated_cost", "expected_attendance", "note", "updated_by", "updated_at",
        ],
    }
    lowered = query.lower()
    for table, columns in known.items():
        if f"from {table}" in lowered:
            return columns
    return []
