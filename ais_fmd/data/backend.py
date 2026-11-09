"""
Backend abstraction.

Two implementations share this interface:

  * `SqliteBackend`  -- the sandbox. A local file, seeded with fake data.
  * `SupabaseBackend` -- production. Refuses to load in sandbox mode, and its
    dependency is not installed in the sandbox venv.

Every method that mutates data takes an `actor`, because the audit trail (M2)
is not optional. There is no write path that skips it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from .. import settings
from ..config.categories import COMMITTEE_BY_ID, committee_name


def normalize_email(value: object) -> str:
    """
    The one spelling of an email that profiles are stored and looked up by.

    Google's token capitalises an address however the account was first typed, so
    lookups must ignore case. Doing that by storing lowercase and comparing for
    equality -- rather than with `ILIKE` -- matters: in a LIKE pattern `_` and
    `%` are wildcards, and `_` is common in real addresses, so `a_b@x.edu` would
    also match `a.b@x.edu`. For a lookup that decides who gets in as which
    committee, "matches a different person's row" is the failure to rule out.
    """
    return str(value or "").strip().lower()


@dataclass
class UploadReceipt:
    """Result of an atomic statement upload."""

    inserted: int = 0
    duplicates_skipped: int = 0
    file_name: str = ""
    committed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.committed and self.error is None


@dataclass
class TransactionChange:
    """One pending edit to one transaction."""

    transaction_id: int
    purpose: str | None = None
    budget_category: int | None = None

    def as_values(self) -> dict:
        return {"purpose": self.purpose, "budget_category": self.budget_category}


@dataclass
class UpdateResult:
    updated: int = 0
    unchanged: int = 0
    failed: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed


class Backend(ABC):
    """The complete data interface used by the application."""

    name: str = "abstract"
    read_only: bool = False

    # --- reads ---------------------------------------------------------------

    @abstractmethod
    def fetch_committees(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_terms(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_budgets(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_transactions(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_uploaded_files(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_merchants(self) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_audit(self, limit: int = 500) -> pd.DataFrame: ...

    @abstractmethod
    def fetch_statement_balances(self) -> pd.DataFrame: ...

    def fetch_labeled_examples(self) -> pd.DataFrame:
        """M19 training labels. Optional: backends without it report empty."""
        raise NotImplementedError(
            f"fetch_labeled_examples is not implemented for the {self.name} backend."
        )

    def insert_labeled_examples(self, examples: list[dict], actor: str) -> UpdateResult:
        raise NotImplementedError(
            f"insert_labeled_examples is not implemented for the {self.name} backend."
        )

    # --- writes --------------------------------------------------------------

    @abstractmethod
    def insert_transactions(
        self, records: list[dict], file_name: str, actor: str
    ) -> UploadReceipt: ...

    @abstractmethod
    def update_transactions(
        self, changes: list[TransactionChange], actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def upsert_budgets(
        self, term_id: str, allocations: dict[int, float], actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def insert_term(
        self, term_id: str, semester: str, start_date: str, end_date: str, actor: str
    ) -> UpdateResult: ...

    @abstractmethod
    def upsert_merchants(self, rules: list[dict], actor: str) -> UpdateResult: ...

    @abstractmethod
    def record_statement_balance(self, balance: dict, actor: str) -> UpdateResult: ...

    # --- Later modules -------------------------------------------------------
    #
    # Concrete rather than abstract so an implementation that predates these
    # (SupabaseBackend) still constructs, and fails with a clear message at the
    # point of use instead of at import.

    def _unsupported(self, feature: str) -> UpdateResult:
        return UpdateResult(
            error=f"{feature} is not implemented for the {self.name} backend."
        )

    def fetch_reimbursements(self) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} does not support reimbursements yet.")

    def create_reimbursement(self, request: dict, actor: str) -> UpdateResult:
        return self._unsupported("Reimbursement submission")

    def decide_reimbursement(
        self, request_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        return self._unsupported("Reimbursement approval")

    def link_reimbursement_to_transaction(
        self, request_id: int, transaction_id: int, actor: str
    ) -> UpdateResult:
        return self._unsupported("Reimbursement matching")

    def fetch_receipts(self) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} does not support receipts yet.")

    def store_receipt(self, receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
        return None, self._unsupported("Receipt storage")

    def set_term_lock(self, term_id: str, locked: bool, actor: str) -> UpdateResult:
        return self._unsupported("Period locking")

    def set_term_dues_rates(
        self, term_id: str, rates: str, verified: bool, actor: str
    ) -> UpdateResult:
        return self._unsupported("Per-term dues rates")

    def set_term_sponsorship_goal(
        self, term_id: str, goal: float | None, actor: str
    ) -> UpdateResult:
        return self._unsupported("Sponsorship goal")

    def set_term_dues_schedule(
        self,
        term_id: str,
        early_price: float | None,
        late_price: float | None,
        increase_date: str | None,
        late_cutoff: str | None,
        actor: str,
    ) -> UpdateResult:
        return self._unsupported("Dues price dates")

    def set_transaction_sponsor_name(
        self, transaction_id: int, sponsor_name: str | None, actor: str
    ) -> UpdateResult:
        return self._unsupported("Sponsor name")

    def fetch_members(self, term_id: str | None = None) -> pd.DataFrame:
        return pd.DataFrame()

    def replace_members(
        self, term_id: str, members: list[dict], source_file: str, actor: str
    ) -> UpdateResult:
        return self._unsupported("Membership roster")

    def add_member_alias(
        self, term_id: str, match_key: str, alias_key: str, actor: str
    ) -> UpdateResult:
        return self._unsupported("Member alias confirmation")

    # Events (domain/events.py). SQLite only for now: see migrations/016.
    def fetch_events(self, term_id: str | None = None) -> pd.DataFrame:
        return pd.DataFrame()

    def sync_events(self, events: list[dict], actor: str) -> UpdateResult:
        return self._unsupported("Calendar sync")

    def set_event_attendance(
        self, event_id: int, attendance: int | None, source: str, actor: str
    ) -> UpdateResult:
        return self._unsupported("Event attendance")

    def fetch_event_links(self) -> pd.DataFrame:
        return pd.DataFrame()

    def link_transactions_to_events(self, pairs: list[tuple[int, int]], actor: str) -> UpdateResult:
        return self._unsupported("Linking spending to events")

    def unlink_transaction_from_event(self, transaction_id: int, actor: str) -> UpdateResult:
        return self._unsupported("Linking spending to events")

    def fetch_profiles(self) -> pd.DataFrame:
        return pd.DataFrame()

    def fetch_profile(self, email: str) -> dict | None:
        return None

    def upsert_profile(
        self,
        email: str,
        role: str,
        committee_id: int | None,
        display_name: str,
        actor: str,
    ) -> UpdateResult:
        return self._unsupported("VP portal access")

    def remove_profile(self, email: str) -> UpdateResult:
        return self._unsupported("VP portal access")

    # --- Model runs and spend reviews -----------------------------------------
    #
    # `llm_runs` is the spending ledger every model call is metered against
    # (domain/categorize/metering.py). `spend_reviews` holds one verdict per
    # charge per committee it was booked to, so nothing is sent twice. Both need
    # migrations/013_model_runs.sql. A backend without them cannot meter, so it
    # refuses to start a run -- the model is never called unmetered.

    def fetch_llm_runs(self) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} has no model-run ledger.")

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
        """
        Record a run as running and return its id -- or None when another run
        is still going. Runs started before `stale_before` and still marked
        running crashed; they are marked abandoned first (their reservation
        keeps counting against the month).
        """
        return None, self._unsupported("Model run ledger")

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
        return self._unsupported("Model run ledger")

    def fetch_spend_reviews(self) -> pd.DataFrame:
        raise NotImplementedError(f"{self.name} has no spend reviews.")

    def insert_spend_reviews(self, reviews: list[dict], actor: str) -> UpdateResult:
        """
        Store verdicts. A charge already reviewed while booked to the same
        committee is skipped (`unchanged`), never overwritten.
        """
        return self._unsupported("Spend review")

    def resolve_spend_review(
        self, review_id: int, status: str, actor: str, note: str = ""
    ) -> UpdateResult:
        return self._unsupported("Spend review")

    # --- "This isn't ours" flags ---------------------------------------------
    #
    # A VP disputing a charge booked to their committee. A flag is a note to the
    # treasurer; it never changes the booking.

    def fetch_flags(self, committee_ids: tuple[int, ...] | None = None) -> pd.DataFrame:
        """All flags, or with `committee_ids` only those on charges booked to them."""
        return pd.DataFrame()

    def fetch_flags_moved_into(self, committee_ids: tuple[int, ...]) -> pd.DataFrame:
        """
        Charges the treasurer moved INTO these budget lines because another
        committee flagged them -- so the receiving VP is told, and the charge
        does not just appear in their totals unexplained.

        Written once here, on top of two methods every backend already has, so
        SQLite and Supabase both get it.

        What comes back is deliberately only the charge (date, amount, details,
        purpose) and when it was decided. Not who flagged it, not their note, not
        the treasurer's reply (which names the committee it came from), and not
        the line it was moved out of: those belong to another committee, and the
        caller here is one VP, so they are dropped in this layer rather than
        trusting a page to hide them.
        """
        columns = [
            "flag_id", "transaction_id", "resolved_at",
            "transaction_date", "amount", "details", "purpose",
        ]
        empty = pd.DataFrame(columns=columns)
        ids = {int(committee_id) for committee_id in committee_ids}
        if not ids:
            return empty

        flags = self.fetch_flags(None)
        if flags is None or flags.empty:
            return empty
        decided = flags[
            (flags["status"] == "resolved") & ~flags["booked_to_committee_id"].isin(ids)
        ]
        if decided.empty:
            return empty

        charges = self.fetch_transactions()
        if charges is None or charges.empty:
            return empty
        here = charges[charges["budget_category"].isin(ids)][
            ["transactionid", "transaction_date", "amount", "details", "purpose"]
        ]
        moved = decided.merge(here, left_on="transaction_id", right_on="transactionid", how="inner")
        if moved.empty:
            return empty
        moved = moved.sort_values("resolved_at", ascending=False).drop_duplicates("transaction_id")
        return moved[columns].reset_index(drop=True)

    def create_flag(
        self,
        transaction_id: int,
        allowed_committee_ids: tuple[int, ...],
        note: str,
        actor: str,
    ) -> UpdateResult:
        """
        `allowed_committee_ids` is every budget line the caller owns; the backend
        refuses a charge booked anywhere else.
        """
        return self._unsupported("Flagging a charge")

    def resolve_flag(self, flag_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
        return self._unsupported("Resolving a flag")

    def move_flagged_charge(
        self, flag_id: int, new_committee_id: int, actor: str, note: str = ""
    ) -> UpdateResult:
        """
        The treasurer's verdict "this isn't theirs": move the charge to
        `new_committee_id` and close the flag as resolved, as one decision.

        Written once here, on top of methods every backend already implements, so
        SQLite and Supabase both get it -- and with it the closed-period check and
        the audit trail of `update_transactions`, which the move goes *through*
        rather than around.

        The order is deliberate. The charge is moved first and the flag closed
        second, so the only way to fail halfway is "moved but the flag is still
        open" (which the treasurer can simply close) -- never "flag closed but the
        charge never moved". If the move is refused, the flag stays open.

        Refuses, leaving everything as it was, when the flag is not open, the
        destination is unknown or the same, or the charge has been moved since it
        was flagged (the treasurer should then just close the flag).
        """
        result = UpdateResult()

        flags = self.fetch_flags(None)
        match = flags[flags["flag_id"] == int(flag_id)] if not flags.empty else flags
        if match.empty:
            result.error = f"Flag {flag_id} does not exist."
            return result
        flag = match.iloc[0]
        if flag["status"] != "open":
            result.unchanged = 1
            return result

        if int(new_committee_id) not in COMMITTEE_BY_ID:
            result.error = f"Committee {new_committee_id} does not exist."
            return result

        charges = self.fetch_transactions()
        charge = charges[charges["transactionid"] == int(flag["transaction_id"])]
        if charge.empty:
            result.error = f"Charge {flag['transaction_id']} does not exist."
            return result
        charge = charge.iloc[0]

        booked_now = None if pd.isna(charge["budget_category"]) else int(charge["budget_category"])
        flagged_from = None if pd.isna(flag["booked_to_committee_id"]) else int(flag["booked_to_committee_id"])
        if booked_now != flagged_from:
            result.error = (
                "This charge has changed since it was flagged: it is now booked to "
                f"{committee_name(booked_now) or 'no committee'}. Close the flag instead of moving it."
            )
            return result
        if booked_now == int(new_committee_id):
            result.error = f"It is already booked to {committee_name(booked_now)}."
            return result

        # `update_transactions` writes the purpose too, so pass the current one or
        # it would be blanked.
        purpose = None if pd.isna(charge["purpose"]) else str(charge["purpose"])
        moved = self.update_transactions(
            [
                TransactionChange(
                    transaction_id=int(flag["transaction_id"]),
                    purpose=purpose,
                    budget_category=int(new_committee_id),
                )
            ],
            actor,
        )
        if moved.error or moved.failed:
            result.error = moved.error or "The charge could not be moved."
            return result  # nothing changed, so the flag stays open

        summary = f"Moved from {committee_name(booked_now)} to {committee_name(int(new_committee_id))}."
        closing = self.resolve_flag(int(flag_id), "resolved", actor, f"{summary} {note}".strip())
        result.updated = 1  # the charge did move
        if closing.error:
            result.error = (
                f"The charge was moved, but the flag could not be closed ({closing.error}). "
                "Close it by hand."
            )
        return result

    # --- Committee plans and budget requests ----------------------------------
    #
    # A VP's own estimate of what they will spend, and what they would like next
    # term. Neither books, moves or reserves money; the treasurer sets budgets.
    # Reads raise where the backend cannot store plans, so pages hide the feature.

    def fetch_plan_lines(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        """Plan lines (category and amount), optionally only these committees' and one semester's."""
        raise NotImplementedError(f"{self.name} does not store committee plans yet.")

    def replace_plan_lines(
        self,
        committee_id: int,
        semester: str,
        lines: list[dict],
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        """
        Make `lines` the committee's whole plan for `semester`. Refused unless
        `committee_id` is one of `allowed_committee_ids`, the caller's own lines.
        """
        return self._unsupported("Saving a committee plan")

    def fetch_budget_requests(
        self, committee_ids: tuple[int, ...] | None = None, semester: str | None = None
    ) -> pd.DataFrame:
        """Budget requests, optionally only these committees' and one semester's."""
        raise NotImplementedError(f"{self.name} does not store budget requests yet.")

    def save_budget_request(
        self,
        committee_id: int,
        semester: str,
        amount: float,
        reason: str,
        allowed_committee_ids: tuple[int, ...],
        actor: str,
    ) -> UpdateResult:
        """One request per committee and semester; sending again replaces it."""
        return self._unsupported("Sending a budget request")


def get_backend() -> Backend:
    """
    Build the backend for the active environment.

    Sandbox is the default and requires no configuration. Reaching the Supabase
    backend requires AIS_FMD_ENV=production *and* the supabase package, which
    the sandbox venv does not install.
    """
    if settings.is_sandbox():
        from .sqlite_backend import SqliteBackend

        return SqliteBackend()

    from .supabase_backend import SupabaseBackend

    return SupabaseBackend()
