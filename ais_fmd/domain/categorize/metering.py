"""
Metering: what a model call may cost, and the rules that stop one.

Every model call in the app goes through a `ModelGate`. `llm._call_model`
refuses to run without one, so a page, a script or an evaluation that reaches
the model pass by accident gets a skipped pass, not a bill. A gate is only
built by `open_run`, which checks the month's spend, the day's run count and
that no other run is going -- and records the run before the first call, so a
crash mid-run still counts against the cap.

The layers, outermost first:

  1. The Anthropic workspace spend limit and prepaid credit (set in the
     Console, not here). Nothing in this file can exceed them.
  2. `settings.llm_enabled()` -- off in sandbox, off with AIS_FMD_LLM_ENABLED=0.
  3. The monthly cap (`settings.llm_monthly_cap_usd`), counted from the
     `llm_runs` ledger. A run that crashed counts at its full reservation.
  4. Per run: one run at a time, a row cap, a call cap, and a reservation of
     the worst case before it starts.
  5. Per call: the gate refuses a call whose worst case would overrun what the
     run reserved.

Pure: no Streamlit, no database. `open_run` takes the backend it writes to.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from ... import settings

# $ per million tokens: (input, output). Prompts here are under 100K tokens, so
# the standard rate card applies. A model that is not listed cannot be metered,
# and a model that cannot be metered is not called.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# Deliberately pessimistic: real text runs nearer 4 characters a token. Over-
# estimating only makes the gate stricter.
CHARS_PER_TOKEN = 3.0

# The SDK may send a call twice (one retry), so admission assumes it does.
ATTEMPTS_PER_CALL = 2

# A run still marked running after this long crashed; its lock is released and
# its reservation stays counted.
STALE_RUN_MINUTES = 15

PURPOSES = ("categorize", "spend_review")
RUNNING, DONE, FAILED, ABANDONED = "running", "done", "failed", "abandoned"


def price_for(model: str) -> tuple[float, float] | None:
    return PRICES_PER_MTOK.get(model)


def estimate_tokens(chars: int) -> int:
    return int(math.ceil(chars / CHARS_PER_TOKEN))


def worst_case_call_usd(model: str, prompt_chars: int, max_output_tokens: int) -> float:
    """The most one call could cost: every retry, full output, no cache."""
    price = price_for(model)
    if price is None:
        return math.inf
    input_rate, output_rate = price
    one = (estimate_tokens(prompt_chars) * input_rate + max_output_tokens * output_rate) / 1e6
    return one * ATTEMPTS_PER_CALL


def usage_cost_usd(model: str, usage) -> float:
    """The actual cost of one response, from its `usage` block."""
    price = price_for(model)
    if price is None or usage is None:
        return 0.0
    input_rate, output_rate = price
    plain = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    return (
        plain * input_rate
        + written * input_rate * CACHE_WRITE_MULTIPLIER
        + read * input_rate * CACHE_READ_MULTIPLIER
        + output * output_rate
    ) / 1e6


@dataclass
class ModelGate:
    """
    Permission to make at most `max_calls` calls costing at most `budget_usd`.

    `admit` is asked before every call and `record` told after it. Neither can
    raise the limits they were built with.
    """

    model: str
    budget_usd: float
    max_calls: int
    max_output_tokens: int
    run_id: int | None = None
    # time.monotonic() after which no call may start; None means no deadline.
    deadline: float | None = None
    calls: int = 0
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    refusals: list[str] = field(default_factory=list)

    def admit(self, prompt_chars: int) -> str | None:
        """None if the call may go ahead, else why not."""
        if price_for(self.model) is None:
            reason = f"No price is configured for {self.model!r}, so it cannot be metered."
        elif self.seconds_left() == 0:
            reason = "This run reached its time limit; the rest are left for the next run."
        elif self.calls >= self.max_calls:
            reason = f"This run has made its {self.max_calls} allowed call(s)."
        elif self.spent_usd + worst_case_call_usd(
            self.model, prompt_chars, self.max_output_tokens
        ) > self.budget_usd:
            reason = (
                f"The next call could cost more than the ${self.budget_usd:.4f} "
                f"this run reserved."
            )
        else:
            self.calls += 1
            return None
        self.refusals.append(reason)
        return reason

    def seconds_left(self) -> float | None:
        """Time left before the deadline (never negative), or None with no deadline."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def record(self, usage) -> None:
        if usage is None:
            return
        self.spent_usd += usage_cost_usd(self.model, usage)
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


# --------------------------------------------------------------------------
# The ledger: what has been spent, and whether a new run may start
# --------------------------------------------------------------------------

def _utc(value) -> pd.Timestamp:
    stamp = pd.to_datetime(value, errors="coerce", utc=True)
    return stamp


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def counted_cost(run: dict) -> float:
    """
    What a ledger row counts against the cap. A finished run counts what it
    spent; one that is still going, or that crashed, counts its whole
    reservation, because what it spent is unknown.
    """
    status = run.get("status")
    if status in (RUNNING, ABANDONED):
        return float(run.get("reserved_usd") or 0.0)
    return float(run.get("cost_usd") or 0.0)


def spent_this_month(runs: pd.DataFrame, now: datetime) -> float:
    if runs is None or runs.empty:
        return 0.0
    started = runs["started_at"].map(_utc)
    this_month = runs[started >= pd.Timestamp(month_start(now))]
    return round(sum(counted_cost(row) for row in this_month.to_dict("records")), 6)


def runs_today(runs: pd.DataFrame, now: datetime) -> int:
    if runs is None or runs.empty:
        return 0
    midnight = pd.Timestamp(now.replace(hour=0, minute=0, second=0, microsecond=0))
    return int((runs["started_at"].map(_utc) >= midnight).sum())


@dataclass
class RunPlan:
    """How big a run is, decided before it starts."""

    purpose: str
    rows: int
    calls: int
    prompt_chars_per_call: int

    def reservation_usd(self, model: str, max_output_tokens: int) -> float:
        return self.calls * worst_case_call_usd(model, self.prompt_chars_per_call, max_output_tokens)


def plan_calls(rows: int, batch_size: int) -> int:
    return 0 if rows <= 0 else int(math.ceil(rows / max(1, batch_size)))


@dataclass
class Admission:
    gate: ModelGate | None
    reason: str | None = None
    spent_this_month: float = 0.0
    cap: float = 0.0


def open_run(backend, plan: RunPlan, actor: str, *, now: datetime | None = None) -> Admission:
    """
    Check every limit, record the run, and hand back the gate for its calls.

    Refuses -- with a reason the page can show -- when the model is switched
    off, the model has no price, the plan is over a per-run cap, the month's
    cap would be passed, the day's run limit is reached, or another run holds
    the lock.
    """
    now = now or datetime.now(timezone.utc)
    cap = settings.llm_monthly_cap_usd()
    model = settings.llm_model()
    max_output = settings.llm_max_output_tokens()

    if not settings.llm_enabled():
        return Admission(None, "The model is switched off.", cap=cap)
    if plan.purpose not in PURPOSES:
        return Admission(None, f"Unknown model pass {plan.purpose!r}.", cap=cap)
    if price_for(model) is None:
        return Admission(None, f"No price is configured for {model!r}, so it is not called.", cap=cap)
    if plan.calls <= 0:
        return Admission(None, "Nothing to send.", cap=cap)
    if plan.rows > settings.llm_max_rows_per_run():
        return Admission(None, f"{plan.rows} rows is over the {settings.llm_max_rows_per_run()}-row limit per run.", cap=cap)
    if plan.calls > settings.llm_max_calls_per_run():
        return Admission(None, f"{plan.calls} calls is over the {settings.llm_max_calls_per_run()}-call limit per run.", cap=cap)

    try:
        runs = backend.fetch_llm_runs()
    except Exception as exc:  # noqa: BLE001 - no ledger means no metering, so no call
        return Admission(None, f"The spending ledger could not be read ({type(exc).__name__}), so no call is made.", cap=cap)

    spent = spent_this_month(runs, now)
    reserve = plan.reservation_usd(model, max_output)
    if spent + reserve > cap:
        return Admission(
            None,
            f"This run could cost up to ${reserve:.4f}, and ${spent:.4f} of this month's "
            f"${cap:.2f} cap is already used.",
            spent_this_month=spent,
            cap=cap,
        )
    if runs_today(runs, now) >= settings.llm_max_runs_per_day():
        return Admission(
            None,
            f"Today's limit of {settings.llm_max_runs_per_day()} model runs is reached.",
            spent_this_month=spent,
            cap=cap,
        )

    run_id, result = backend.start_llm_run(
        purpose=plan.purpose,
        model=model,
        actor=actor,
        rows_planned=plan.rows,
        calls_planned=plan.calls,
        reserved_usd=round(reserve, 6),
        stale_before=(now - timedelta(minutes=STALE_RUN_MINUTES)).isoformat(),
        started_at=now.isoformat(),
    )
    if run_id is None:
        return Admission(None, result.error or "Another model run is in progress.", spent_this_month=spent, cap=cap)

    gate = ModelGate(
        model=model,
        budget_usd=reserve,
        max_calls=plan.calls,
        max_output_tokens=max_output,
        run_id=run_id,
        deadline=time.monotonic() + settings.llm_run_deadline_seconds(),
    )
    return Admission(gate, None, spent_this_month=spent, cap=cap)


def close_run(backend, gate: ModelGate, *, failed: bool, error: str | None = None):
    """Write what the run actually used. Always called, success or not."""
    return backend.finish_llm_run(
        run_id=gate.run_id,
        status=FAILED if failed else DONE,
        calls_made=gate.calls,
        input_tokens=gate.input_tokens,
        output_tokens=gate.output_tokens,
        cache_read_tokens=gate.cache_read_tokens,
        cache_write_tokens=gate.cache_write_tokens,
        cost_usd=round(gate.spent_usd, 6),
        error=(error or "")[:500] or None,
    )
