"""
The one caching layer.

FINDING (caching). The original had two. `utils.py` loaders were correctly
decorated with `@st.cache_data(ttl=300)`, and then all four pages wrapped them
in a second, un-TTL'd `@st.cache_data`. The outer cache never expired, so the
inner TTL never fired -- data refreshed only when something called the global
`st.cache_data.clear()`, which wipes every cached function for every user at
once. Four separate wrappers also meant four copies of the transaction table
resident at the same time.

Here there is exactly one cached read per table, keyed on a process-global
version counter. A write bumps the counter, which invalidates precisely the data
that changed for everyone, without nuking unrelated caches.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from .. import auth
from .backend import Backend, TransactionChange, UpdateResult, UploadReceipt, get_backend


@st.cache_resource(show_spinner=False)
def _version_holder() -> dict:
    """Process-global version counter. Shared across sessions, unlike session_state."""
    return {"value": 0}


def data_version() -> int:
    return _version_holder()["value"]


def invalidate() -> None:
    """Called after every write. Granular by design -- no global cache clear."""
    _version_holder()["value"] += 1


@st.cache_resource(show_spinner=False)
def backend() -> Backend:
    """
    The backend object holds no user identity, so caching it per process is safe.

    This is the distinction the original missed: caching a *connection factory*
    is fine, caching an object that carries an authenticated session is not
    (FINDING F1).
    """
    return get_backend()


# --- Cached reads ------------------------------------------------------------
# Each takes the version counter so a bump invalidates it.

@st.cache_data(ttl=300, show_spinner=False)
def _committees(version: int) -> pd.DataFrame:
    return backend().fetch_committees()


@st.cache_data(ttl=300, show_spinner=False)
def _terms(version: int) -> pd.DataFrame:
    return backend().fetch_terms()


@st.cache_data(ttl=300, show_spinner=False)
def _budgets(version: int) -> pd.DataFrame:
    return backend().fetch_budgets()


@st.cache_data(ttl=300, show_spinner=False)
def _transactions(version: int) -> pd.DataFrame:
    return backend().fetch_transactions()


@st.cache_data(ttl=300, show_spinner=False)
def _uploaded_files(version: int) -> pd.DataFrame:
    return backend().fetch_uploaded_files()


@st.cache_data(ttl=300, show_spinner=False)
def _merchants(version: int) -> pd.DataFrame:
    return backend().fetch_merchants()


@st.cache_data(ttl=60, show_spinner=False)
def _audit(version: int, limit: int) -> pd.DataFrame:
    return backend().fetch_audit(limit)


@st.cache_data(ttl=300, show_spinner=False)
def _statement_balances(version: int) -> pd.DataFrame:
    return backend().fetch_statement_balances()


@st.cache_data(ttl=300, show_spinner=False)
def _members(version: int, term_id: str | None) -> pd.DataFrame:
    return backend().fetch_members(term_id)


@st.cache_data(ttl=300, show_spinner=False)
def _profiles(version: int) -> pd.DataFrame:
    return backend().fetch_profiles()


# --- Public API --------------------------------------------------------------

def load_committees() -> pd.DataFrame:
    return _committees(data_version())


def load_terms() -> pd.DataFrame:
    return _terms(data_version())


def load_budgets() -> pd.DataFrame:
    return _budgets(data_version())


def load_transactions() -> pd.DataFrame:
    return _transactions(data_version())


def load_uploaded_files() -> pd.DataFrame:
    return _uploaded_files(data_version())


def load_merchants() -> pd.DataFrame:
    return _merchants(data_version())


def load_members(term_id: str | None = None) -> pd.DataFrame:
    return _members(data_version(), term_id)


def load_profiles() -> pd.DataFrame:
    return _profiles(data_version())


def load_audit(limit: int = 500) -> pd.DataFrame:
    return _audit(data_version(), limit)


def load_statement_balances() -> pd.DataFrame:
    return _statement_balances(data_version())


def load_merchant_memory():
    from ..domain.categorize.merchants import MerchantMemory

    frame = load_merchants()
    records = frame.to_dict("records") if not frame.empty else []
    return MerchantMemory.from_records(records)


@dataclass
class DataBundle:
    """Everything a page needs, loaded once."""

    committees: pd.DataFrame
    terms: pd.DataFrame
    budgets: pd.DataFrame
    transactions: pd.DataFrame


def load_bundle() -> DataBundle:
    return DataBundle(
        committees=load_committees(),
        terms=load_terms(),
        budgets=load_budgets(),
        transactions=load_transactions(),
    )


# --- Writes (each invalidates) -----------------------------------------------
#
# Every write below is wrapped so a read-only identity (the President) is refused
# here, whatever the page does. Pages also hide their edit controls, but this is
# the one place that cannot be forgotten. No identity in the session (a script or
# test calling the repository directly) is not blocked: every page checks the role
# before it renders, so a signed-in write always has one.

READ_ONLY_MESSAGE = "You have read-only access, so nothing was changed."


def _is_read_only() -> bool:
    try:
        identity = st.session_state.get(auth.SESSION_KEY)
    except Exception:  # noqa: BLE001 - no session at all (a plain script)
        return False
    return bool(getattr(identity, "read_only", False))


def _update_refusal() -> UpdateResult:
    return UpdateResult(error=READ_ONLY_MESSAGE)


def _upload_refusal() -> UploadReceipt:
    return UploadReceipt(error=READ_ONLY_MESSAGE)


def _receipt_refusal() -> tuple[int | None, UpdateResult]:
    return None, UpdateResult(error=READ_ONLY_MESSAGE)


def _blocked_when_read_only(refusal):
    def decorate(write):
        @functools.wraps(write)
        def guarded(*args, **kwargs):
            if _is_read_only():
                return refusal()
            return write(*args, **kwargs)

        return guarded

    return decorate


@_blocked_when_read_only(_upload_refusal)
def insert_transactions(records: list[dict], file_name: str, actor: str) -> UploadReceipt:
    receipt = backend().insert_transactions(records, file_name, actor)
    if receipt.ok:
        invalidate()
    return receipt


@_blocked_when_read_only(_update_refusal)
def update_transactions(changes: list[TransactionChange], actor: str) -> UpdateResult:
    result = backend().update_transactions(changes, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def upsert_budgets(term_id: str, allocations: dict[int, float], actor: str) -> UpdateResult:
    result = backend().upsert_budgets(term_id, allocations, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def insert_term(term_id: str, semester: str, start_date: str, end_date: str, actor: str) -> UpdateResult:
    result = backend().insert_term(term_id, semester, start_date, end_date, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def upsert_merchants(rules: list[dict], actor: str) -> UpdateResult:
    result = backend().upsert_merchants(rules, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def record_statement_balance(balance: dict, actor: str) -> UpdateResult:
    result = backend().record_statement_balance(balance, actor)
    if result.updated:
        invalidate()
    return result


# --- Reimbursements, receipts, locking ---------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _reimbursements(version: int) -> pd.DataFrame:
    return backend().fetch_reimbursements()


@st.cache_data(ttl=60, show_spinner=False)
def _receipts(version: int) -> pd.DataFrame:
    return backend().fetch_receipts()


def load_reimbursements() -> pd.DataFrame:
    return _reimbursements(data_version())


def load_receipts() -> pd.DataFrame:
    return _receipts(data_version())


@_blocked_when_read_only(_update_refusal)
def create_reimbursement(request: dict, actor: str) -> UpdateResult:
    result = backend().create_reimbursement(request, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def decide_reimbursement(request_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
    result = backend().decide_reimbursement(request_id, status, actor, note)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def link_reimbursement(request_id: int, transaction_id: int, actor: str) -> UpdateResult:
    result = backend().link_reimbursement_to_transaction(request_id, transaction_id, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_receipt_refusal)
def store_receipt(receipt: dict, actor: str) -> tuple[int | None, UpdateResult]:
    receipt_id, result = backend().store_receipt(receipt, actor)
    if result.updated:
        invalidate()
    return receipt_id, result


@_blocked_when_read_only(_update_refusal)
def set_term_lock(term_id: str, locked: bool, actor: str) -> UpdateResult:
    result = backend().set_term_lock(term_id, locked, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def set_term_dues_rates(
    term_id: str, rates: str, verified: bool, actor: str
) -> UpdateResult:
    """Record what a term charged for dues. Invalidates so the schedule rebuilds."""
    result = backend().set_term_dues_rates(term_id, rates, verified, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def set_term_sponsorship_goal(
    term_id: str, goal: float | None, actor: str
) -> UpdateResult:
    """Record the sponsorship dollar goal for a term."""
    result = backend().set_term_sponsorship_goal(term_id, goal, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def set_transaction_sponsor_name(
    transaction_id: int, sponsor_name: str | None, actor: str
) -> UpdateResult:
    """Record who a sponsorship transaction was actually from."""
    result = backend().set_transaction_sponsor_name(transaction_id, sponsor_name, actor)
    if result.updated:
        invalidate()
    return result


@st.cache_data(ttl=60, show_spinner=False)
def _labeled_examples(version: int) -> pd.DataFrame:
    return backend().fetch_labeled_examples()


def load_labeled_examples() -> pd.DataFrame:
    """M19 training labels: ledger imports plus every review-queue decision."""
    return _labeled_examples(data_version())


@_blocked_when_read_only(_update_refusal)
def record_labels(examples: list[dict], actor: str) -> UpdateResult:
    result = backend().insert_labeled_examples(examples, actor)
    if result.updated:
        invalidate()
    return result


# --- Cached derived computations ---------------------------------------------
#
# FINDING (performance). Every *read* above is cached against the version
# counter, and then nothing cached the work built on top of them. Measured with
# scripts/profile_hotpath.py on 513 transactions:
#
#     alerts.evaluate            389 ms
#     reconcile.reconcile_all    301 ms
#     quality.run_all_checks      62 ms
#
# Streamlit re-runs the whole script on every widget interaction, so Home paid
# ~363 ms and Alerts & Reports ~389 ms of that per click -- and roughly 300 ms
# of the second figure is `reconcile_all` running a *second* time, because
# `_reconciliation_alerts` calls it internally. On a shared-CPU host, 2-3x that.
#
# These three are the whole problem, and they are pure functions of data the
# version counter already tracks, so caching them needs no new invalidation
# logic. Keyed on `version` for the same reason the reads are: a write bumps it
# and every derived figure recomputes exactly once.
#
# These take no data arguments and load what they need themselves, deliberately.
# Passing the frames in would put them in the cache key, and Streamlit hashes a
# DataFrame by value -- so every call would walk the whole transactions table to
# decide whether it could skip walking the whole transactions table. Every
# caller was passing unfiltered frames straight from the loaders above anyway,
# so the version counter alone identifies the answer exactly.
#
# A caller that genuinely needs one of these over a *filtered* frame should call
# the domain function directly and accept the cost; nothing does today.

@st.cache_data(ttl=300, show_spinner=False)
def _cached_alerts(version: int, semester: str | None):
    from ..domain import alerts as alerts_domain

    bundle = load_bundle()
    return alerts_domain.evaluate(
        bundle.transactions,
        bundle.budgets,
        bundle.terms,
        load_statement_balances(),
        semester=semester,
    )


@st.cache_data(ttl=300, show_spinner=False)
def _cached_quality(version: int):
    from ..domain import quality

    bundle = load_bundle()
    return quality.run_all_checks(bundle.transactions, bundle.budgets, bundle.terms)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_reconciliation(version: int):
    from ..domain import reconcile

    return reconcile.reconcile_all(load_transactions(), load_statement_balances())


def evaluate_alerts(semester: str | None = None):
    """`domain.alerts.evaluate` over the current data, memoized until the next write."""
    return _cached_alerts(data_version(), semester)


def run_quality_checks():
    """`domain.quality.run_all_checks` over the current data, memoized until the next write."""
    return _cached_quality(data_version())


def reconcile_all():
    """`domain.reconcile.reconcile_all` over the current data, memoized until the next write."""
    return _cached_reconciliation(data_version())


# --- "This isn't ours" flags --------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _flags(version: int, committee_ids: tuple[int, ...] | None) -> pd.DataFrame:
    return backend().fetch_flags(committee_ids)


def load_flags(committee_ids: tuple[int, ...] | None = None) -> pd.DataFrame:
    """
    Flags, open first. Pass a VP's `committee_ids` to get only flags on charges
    booked to their lines; `None` is every flag, for the treasurer.

    Raises if the backend cannot answer (for example the table has not been
    created yet); callers check before drawing the feature rather than assuming.
    """
    scope = None if committee_ids is None else tuple(int(i) for i in committee_ids)
    return _flags(data_version(), scope)


@st.cache_data(ttl=60, show_spinner=False)
def _flags_moved_into(version: int, committee_ids: tuple[int, ...]) -> pd.DataFrame:
    return backend().fetch_flags_moved_into(committee_ids)


def load_flags_moved_into(committee_ids: tuple[int, ...]) -> pd.DataFrame:
    """
    Charges moved into these budget lines after another committee flagged them.
    Only the charge comes back, never who flagged it or where it came from (see
    `Backend.fetch_flags_moved_into`).
    """
    return _flags_moved_into(data_version(), tuple(int(i) for i in committee_ids))


@_blocked_when_read_only(_update_refusal)
def create_flag(
    transaction_id: int, allowed_committee_ids: tuple[int, ...], note: str, actor: str
) -> UpdateResult:
    result = backend().create_flag(transaction_id, allowed_committee_ids, note, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def resolve_flag(flag_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
    result = backend().resolve_flag(flag_id, status, actor, note)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def move_flagged_charge(
    flag_id: int, new_committee_id: int, actor: str, note: str = ""
) -> UpdateResult:
    """The treasurer's "not theirs" verdict: move the charge and close the flag."""
    result = backend().move_flagged_charge(flag_id, new_committee_id, actor, note)
    if result.updated:
        invalidate()  # every committee's totals changed, not just the flag list
    return result


# --- Model runs and spend reviews ---------------------------------------------
# Not cached: the run lock and the monthly spend must be read fresh, and both
# tables are small.

def load_spend_reviews() -> pd.DataFrame:
    """Raises if the table does not exist yet (migration 013); callers check."""
    return backend().fetch_spend_reviews()


def model_month_summary() -> tuple[float, float, int]:
    from ..domain import model_runs

    return model_runs.month_summary(backend())


def _run_refusal():
    from ..domain.model_runs import RunReport

    return RunReport(refusal=_update_refusal().error)


@_blocked_when_read_only(_run_refusal)
def run_spend_review(actor: str):
    """One metered spend review over the charges not yet reviewed."""
    from ..domain import model_runs

    report = model_runs.run_spend_review(backend(), actor, load_transactions(), load_terms())
    if report.started:
        invalidate()
    return report


@_blocked_when_read_only(lambda: (None, _run_refusal()))
def run_categorizer(actor: str, records: list[dict], dues=None):
    """One metered categorizer pass over an upload's new rows."""
    from ..domain import model_runs

    return model_runs.run_categorizer(backend(), actor, records, dues=dues)


@_blocked_when_read_only(_update_refusal)
def resolve_spend_review(review_id: int, status: str, actor: str, note: str = "") -> UpdateResult:
    result = backend().resolve_spend_review(review_id, status, actor, note)
    if result.updated:
        invalidate()
    return result


# --- Roster and access list ---------------------------------------------------
# Pages used to call these on `backend()` directly, which skipped the read-only
# check above. Going through here keeps that check in one place. They do not
# invalidate; their pages call `invalidate()` themselves, as before.

@_blocked_when_read_only(_update_refusal)
def replace_members(
    term_id: str, members: list[dict], source_file: str, actor: str
) -> UpdateResult:
    return backend().replace_members(term_id, members, source_file, actor)


@_blocked_when_read_only(_update_refusal)
def add_member_alias(term_id: str, match_key: str, alias_key: str, actor: str) -> UpdateResult:
    return backend().add_member_alias(term_id, match_key, alias_key, actor)


# --- Committee plans and budget requests --------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _plan_lines(version: int, committee_ids: tuple[int, ...] | None, semester: str | None) -> pd.DataFrame:
    return backend().fetch_plan_lines(committee_ids, semester)


def load_plan_lines(
    committee_ids: tuple[int, ...] | None = None, semester: str | None = None
) -> pd.DataFrame:
    """
    Plan lines (category and amount). Raises if the backend cannot store plans
    yet (migration 014 not run); callers check before drawing the feature.
    """
    scope = None if committee_ids is None else tuple(int(i) for i in committee_ids)
    return _plan_lines(data_version(), scope, semester)


@st.cache_data(ttl=60, show_spinner=False)
def _budget_requests(
    version: int, committee_ids: tuple[int, ...] | None, semester: str | None
) -> pd.DataFrame:
    return backend().fetch_budget_requests(committee_ids, semester)


def load_budget_requests(
    committee_ids: tuple[int, ...] | None = None, semester: str | None = None
) -> pd.DataFrame:
    """Budget requests; `None` committees is every committee's, for the treasurer."""
    scope = None if committee_ids is None else tuple(int(i) for i in committee_ids)
    return _budget_requests(data_version(), scope, semester)


@_blocked_when_read_only(_update_refusal)
def replace_plan_lines(
    committee_id: int,
    semester: str,
    lines: list[dict],
    allowed_committee_ids: tuple[int, ...],
    actor: str,
) -> UpdateResult:
    result = backend().replace_plan_lines(committee_id, semester, lines, allowed_committee_ids, actor)
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def save_budget_request(
    committee_id: int,
    semester: str,
    amount: float,
    reason: str,
    allowed_committee_ids: tuple[int, ...],
    actor: str,
) -> UpdateResult:
    result = backend().save_budget_request(
        committee_id, semester, amount, reason, allowed_committee_ids, actor
    )
    if result.updated:
        invalidate()
    return result


@_blocked_when_read_only(_update_refusal)
def upsert_profile(
    email: str, role: str, committee_id: int | None, display_name: str, actor: str
) -> UpdateResult:
    return backend().upsert_profile(email, role, committee_id, display_name, actor)


@_blocked_when_read_only(_update_refusal)
def remove_profile(email: str) -> UpdateResult:
    return backend().remove_profile(email)


def locked_semesters() -> set[str]:
    """Names of terms currently closed to edits."""
    terms = load_terms()
    if terms.empty or "locked" not in terms.columns:
        return set()
    return set(terms[terms["locked"].fillna(0).astype(int) == 1]["Semester"].dropna())


def is_file_uploaded(file_name: str) -> bool:
    frame = load_uploaded_files()
    if frame.empty or "file_name" not in frame.columns:
        return False
    return bool((frame["file_name"] == file_name).any())
