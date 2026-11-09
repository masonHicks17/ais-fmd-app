"""
The two metered model runs: the spend review, and the categorizer's residual.

Each one is decided in full before the first call -- which rows, how many
calls, the worst it could cost -- then admitted (or refused) by
`metering.open_run`, run under the gate, and closed in the ledger whatever
happens. Nothing here loops back to ask the model again.

WHAT THE SPEND REVIEW NEVER SENDS
  * a charge already reviewed while booked to the same committee (the unique
    key on `spend_reviews` enforces this in the database as well);
  * a charge in a closed term, or dated outside every term;
  * history: anything older than REVIEW_WINDOW_DAYS, and anything carried
    over from the original app (no `source_file`);
  * a charge a VP has flagged "This isn't ours" -- a person is already on it;
  * a charge with no committee (the categorizer's job, not the review's).

Plain functions over a backend and data frames -- no Streamlit -- so the
guardrails are tested directly against the SQLite backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .. import settings
from ..config.categories import COMMITTEE_BY_ID
from . import dues as dues_domain
from .categorize import llm, metering
from .categorize.pipeline import CategorizationRun, categorize_records
from .categorize.predicates import COMMITTEE_PURPOSE
from .terms import attach_semester

REVIEW_WINDOW_DAYS = 180


# --------------------------------------------------------------------------
# Which charges the spend review may send
# --------------------------------------------------------------------------

@dataclass
class ReviewSelection:
    eligible: pd.DataFrame
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.eligible)


def select_for_review(
    transactions: pd.DataFrame,
    terms: pd.DataFrame,
    reviews: pd.DataFrame | None,
    flags: pd.DataFrame | None,
    *,
    today: datetime | None = None,
) -> ReviewSelection:
    """The charges a spend review may send, oldest first, and why the rest were left out."""
    today = today or datetime.now(timezone.utc)
    skipped = {
        "no committee": 0,
        "from before this app": 0,
        "older than the review window": 0,
        "outside every term": 0,
        "in a closed term": 0,
        "flagged by a VP": 0,
        "already reviewed": 0,
    }
    if transactions is None or transactions.empty:
        return ReviewSelection(pd.DataFrame(), skipped)

    frame = transactions.copy()
    keep = pd.Series(True, index=frame.index)

    def drop(mask: pd.Series, reason: str) -> None:
        nonlocal keep
        newly = mask & keep
        skipped[reason] += int(newly.sum())
        keep = keep & ~mask

    committee = pd.to_numeric(frame["budget_category"], errors="coerce")
    drop(~committee.isin(list(COMMITTEE_BY_ID)), "no committee")

    if "source_file" in frame.columns:
        drop(frame["source_file"].isna(), "from before this app")
    else:
        drop(pd.Series(True, index=frame.index), "from before this app")

    dates = pd.to_datetime(frame["transaction_date"], errors="coerce")
    window_start = pd.Timestamp(today.date()) - pd.Timedelta(days=REVIEW_WINDOW_DAYS)
    drop(dates.isna() | (dates < window_start), "older than the review window")

    with_terms = attach_semester(frame, terms, column="_semester")
    drop(with_terms["_semester"].isna(), "outside every term")
    closed = _locked_semesters(terms)
    drop(with_terms["_semester"].isin(closed), "in a closed term")

    if flags is not None and not flags.empty and "transaction_id" in flags.columns:
        flagged = set(pd.to_numeric(flags["transaction_id"], errors="coerce").dropna().astype(int))
        drop(frame["transactionid"].astype(int).isin(flagged), "flagged by a VP")

    if reviews is not None and not reviews.empty:
        done = set(
            zip(
                pd.to_numeric(reviews["transaction_id"], errors="coerce").fillna(-1).astype(int),
                pd.to_numeric(reviews["booked_committee_id"], errors="coerce").fillna(-1).astype(int),
            )
        )
        pairs = list(zip(frame["transactionid"].astype(int), committee.fillna(-1).astype(int)))
        drop(pd.Series([pair in done for pair in pairs], index=frame.index), "already reviewed")

    eligible = frame[keep].assign(_date=dates[keep], _semester=with_terms["_semester"][keep])
    eligible = eligible.sort_values(["_date", "transactionid"]).reset_index(drop=True)
    return ReviewSelection(eligible, skipped)


def _locked_semesters(terms: pd.DataFrame) -> set[str]:
    if terms is None or terms.empty or "locked" not in terms.columns:
        return set()
    locked = pd.to_numeric(terms["locked"], errors="coerce").fillna(0).astype(int) == 1
    return set(terms.loc[locked, "Semester"].dropna())


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------

@dataclass
class RunReport:
    started: bool = False
    refusal: str | None = None
    run_id: int | None = None
    rows_sent: int = 0
    saved: int = 0
    flagged: int = 0
    batches_failed: int = 0
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    left_for_next_run: int = 0


class _SaveFailed(RuntimeError):
    """A batch's verdicts could not be stored, so the run stops before paying for more."""


def spend_review_plan(selection: ReviewSelection, transactions: pd.DataFrame, terms: pd.DataFrame):
    """(rows to send, dues baseline, RunPlan) for the next run -- no call is made."""
    batch = selection.eligible.head(settings.llm_max_rows_per_run())
    rows = [(int(record["transactionid"]), record) for record in batch.to_dict("records")]
    baseline = _baseline_for(batch, transactions, terms)
    plan = metering.RunPlan(
        purpose="spend_review",
        rows=len(rows),
        calls=metering.plan_calls(len(rows), settings.llm_batch_size()),
        prompt_chars_per_call=llm.review_listing_chars(rows, baseline) if rows else 0,
    )
    return rows, baseline, plan


def run_spend_review(
    backend,
    actor: str,
    transactions: pd.DataFrame,
    terms: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> RunReport:
    """Review the eligible charges, oldest first, up to the per-run row cap."""
    report = RunReport()
    try:
        reviews = backend.fetch_spend_reviews()
    except Exception as exc:  # noqa: BLE001 - without it, repeats cannot be ruled out
        report.refusal = f"Past reviews could not be read ({type(exc).__name__}), so nothing is sent."
        return report
    try:
        flags = backend.fetch_flags(None)
    except Exception:  # noqa: BLE001 - no flags table (migration 010 not run) means no flags
        flags = None
    selection = select_for_review(transactions, terms, reviews, flags, today=now)
    rows, baseline, plan = spend_review_plan(selection, transactions, terms)
    report.left_for_next_run = max(0, selection.count - len(rows))
    if not rows:
        report.refusal = "Nothing new to review."
        return report

    admission = metering.open_run(backend, plan, actor, now=now)
    if admission.gate is None:
        report.refusal = admission.reason
        return report
    gate = admission.gate
    report.started = True
    report.run_id = gate.run_id
    report.rows_sent = len(rows)
    version = llm.prompt_version()
    booked = {tid: int(record["budget_category"]) for tid, record in rows}

    def save(verdicts: dict[int, llm.SpendVerdict]) -> None:
        stored = [
            {
                "transaction_id": tid,
                "booked_committee_id": booked[tid],
                "verdict": verdict.verdict,
                "suggested_committee_id": verdict.suggested_committee_id,
                "confidence": round(verdict.confidence, 3),
                "reason": verdict.reason,
                "message_to_vp": verdict.message_to_vp or None,
                "status": "open" if verdict.flagged else "cleared",
                "run_id": gate.run_id,
                "model": gate.model,
                "prompt_version": version,
            }
            for tid, verdict in verdicts.items()
        ]
        result = backend.insert_spend_reviews(stored, actor)
        if result.error:
            raise _SaveFailed(result.error)
        report.saved += result.updated
        report.flagged += sum(1 for verdict in verdicts.values() if verdict.flagged)

    failure: str | None = None
    try:
        outcome = llm.review_spend(rows, dues_baseline=baseline, gate=gate, on_batch=save)
        report.batches_failed = outcome.batches_failed
        report.errors = outcome.errors
        if outcome.skipped_reason:
            failure = outcome.skipped_reason
            report.errors.append(outcome.skipped_reason)
        elif outcome.fully_failed:
            failure = "; ".join(outcome.errors[:2])
    except _SaveFailed as exc:
        failure = f"Verdicts could not be saved, so the run stopped: {exc}"
        report.errors.append(failure)
    except Exception as exc:  # noqa: BLE001 - the ledger is closed regardless
        failure = f"{type(exc).__name__}: {exc}"
        report.errors.append(failure)
    finally:
        metering.close_run(backend, gate, failed=failure is not None, error=failure)
        report.cost_usd = gate.spent_usd
    return report


def _baseline_for(batch: pd.DataFrame, transactions: pd.DataFrame, terms: pd.DataFrame) -> str | None:
    """The dues baseline for the term most of this batch falls in."""
    if batch.empty or "_semester" not in batch.columns:
        return None
    semester = batch["_semester"].mode()
    if semester.empty:
        return None
    try:
        baseline = dues_domain.dues_baseline(transactions, terms, str(semester.iloc[0]))
    except Exception:  # noqa: BLE001 - the prompt can infer the rate without it
        return None
    return baseline.render() if baseline else None


# --------------------------------------------------------------------------
# The categorizer's residual, for a statement being imported
# --------------------------------------------------------------------------

def categorizer_plan(run: CategorizationRun) -> metering.RunPlan:
    residual = run.residual
    return metering.RunPlan(
        purpose="categorize",
        rows=len(residual),
        calls=metering.plan_calls(len(residual), settings.llm_batch_size()),
        prompt_chars_per_call=llm.residual_listing_chars(residual, run.context) if residual else 0,
    )


def apply_categorizer_choices(
    records: list[dict],
    choices: dict[str, int | None],
    picks: dict[str, tuple[int | None, str | None, str]],
) -> None:
    """
    Write the treasurer's "Book as" choices onto the upload's rows, in place.

    `choices` maps a row's natural key to the committee chosen for it on the
    Import page's Haiku table (None = left blank, which sends the row to the
    Review Queue). `picks` is what the pipeline booked after Haiku, keyed the
    same way, as (committee, purpose, reason). A row whose choice matches that
    keeps Haiku's purpose and reason; a changed one gets the committee's own
    purpose and a reason that says a person decided it (treasury, 2026-11-03:
    "the ability to edit the haiku responses and have them post to the
    transactions for import").
    """
    for record in records:
        key = str(record.get("natural_key"))
        if key not in choices:
            continue
        chosen = choices[key]
        booked, purpose, reason = picks.get(key, (None, None, ""))
        if chosen is None:
            record["budget_category"], record["purpose"] = None, None
            record["match_rule"] = f"Left for the Review Queue. {reason}".strip()
        elif chosen == booked:
            record["budget_category"], record["purpose"], record["match_rule"] = booked, purpose, reason
        else:
            record["budget_category"] = chosen
            record["purpose"] = COMMITTEE_PURPOSE.get(chosen)
            record["match_rule"] = f"Chosen by the treasurer on import. Haiku: {reason}".strip()


def run_categorizer(
    backend,
    actor: str,
    records: list[dict],
    *,
    dues=None,
    now: datetime | None = None,
) -> tuple[CategorizationRun, RunReport]:
    """
    Categorize `records` -- the NEW rows of an upload, after duplicates are
    removed -- with one metered model pass over what the rules left.
    """
    report = RunReport()
    dry = categorize_records(records, dues=dues)  # rules only: free, and sizes the run
    plan = categorizer_plan(dry)
    if plan.rows == 0:
        report.refusal = "The rules resolved every row; nothing needs the model."
        return dry, report

    admission = metering.open_run(backend, plan, actor, now=now)
    if admission.gate is None:
        report.refusal = admission.reason
        return dry, report
    gate = admission.gate
    report.started = True
    report.rows_sent = plan.rows

    failure: str | None = None
    run: CategorizationRun = dry  # kept if the model pass throws
    try:
        run = categorize_records(records, dues=dues, gate=gate)
        report.batches_failed = run.llm.batches_failed
        report.errors = list(run.llm.errors)
        if run.llm.skipped_reason:
            failure = run.llm.skipped_reason
            report.errors.append(failure)
        elif run.llm.fully_failed:
            failure = "; ".join(run.llm.errors[:2])
        report.saved = len(run.llm.classifications)
    except Exception as exc:  # noqa: BLE001
        failure = f"{type(exc).__name__}: {exc}"
        report.errors.append(failure)
    finally:
        metering.close_run(backend, gate, failed=failure is not None, error=failure)
        report.cost_usd = gate.spent_usd
    return run, report


def month_summary(backend, *, now: datetime | None = None) -> tuple[float, float, int]:
    """(spent this month, monthly cap, runs today) -- for the page, read-only."""
    now = now or datetime.now(timezone.utc)
    try:
        runs = backend.fetch_llm_runs()
    except Exception:  # noqa: BLE001
        runs = pd.DataFrame()
    return (
        metering.spent_this_month(runs, now),
        settings.llm_monthly_cap_usd(),
        metering.runs_today(runs, now),
    )
