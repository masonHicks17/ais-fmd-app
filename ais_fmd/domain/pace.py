"""
Term pace, same-day comparison and cash runway.

FINDING (comparisons). The Dashboard compared a term that was 35% complete
with the whole of the previous term, so every delta looked alarming. Every
comparison here is made at the same day of the equivalent term instead
(Fall 2026 at day 44 against Fall 2025 at day 44).

Pure pandas, like the rest of `domain` -- no Streamlit import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from ..config.categories import BUDGETED_COMMITTEE_IDS
from .budgets import INCOME, budget_vs_actual, burn_rate_projection, categorize_flow, is_transfer, spending_by_committee
from .terms import (
    attach_semester,
    date_range_for_semester,
    ordered_semesters,
    prepare_terms,
    previous_semester,
)

# The balance the treasurer does not want to go below. Treasury to confirm.
RESERVE_FLOOR = 2_000.0
# Share of remaining budget assumed to be spent when no prior term exists.
DEFAULT_SPEND_SHARE = 0.85

_YEAR = re.compile(r"(19|20)\d{2}")


# --- Term clock --------------------------------------------------------------

@dataclass(frozen=True)
class TermClock:
    semester: str
    start: pd.Timestamp
    end: pd.Timestamp
    today: pd.Timestamp  # clamped into [start, end]

    @property
    def length(self) -> int:
        return max((self.end - self.start).days, 1)

    @property
    def day(self) -> int:
        return min(max((self.today - self.start).days, 0), self.length)

    @property
    def fraction(self) -> float:
        return self.day / self.length

    @property
    def week(self) -> int:
        return min(self.day // 7 + 1, self.weeks)

    @property
    def weeks(self) -> int:
        return -(-self.length // 7)

    @property
    def finished(self) -> bool:
        return self.day >= self.length


def term_clock(
    df_terms: pd.DataFrame, semester: str, *, as_of: pd.Timestamp | None = None
) -> TermClock | None:
    window = date_range_for_semester(df_terms, semester)
    if window is None:
        return None
    start, end = window
    if pd.isna(start) or pd.isna(end):
        return None
    now = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.today().normalize()
    return TermClock(semester, start, end, min(max(now, start), end))


def _season_year(name: object) -> tuple[str, int | None]:
    text = str(name or "").strip()
    season = text.split()[0].lower() if text else ""
    match = _YEAR.search(text)
    return season, (int(match.group(0)) if match else None)


def equivalent_prior(df_terms: pd.DataFrame, semester: str) -> str | None:
    """The same season one year earlier, else the latest same season, else the previous term."""
    order = ordered_semesters(df_terms)
    if semester not in order:
        return None
    season, year = _season_year(semester)
    earlier = order[: order.index(semester)]
    same_season = [s for s in earlier if _season_year(s)[0] == season]
    if year is not None:
        exact = [s for s in same_season if _season_year(s)[1] == year - 1]
        if exact:
            return exact[-1]
    if same_season:
        return same_season[-1]
    return previous_semester(df_terms, semester)


# --- Same-day totals ---------------------------------------------------------

def _amounts(frame: pd.DataFrame) -> pd.Series:
    return pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)


def _without_transfers(frame: pd.DataFrame) -> pd.DataFrame:
    """Transfers move money between the org's own accounts: never income or spend."""
    return frame[~is_transfer(frame)] if not frame.empty else frame


def _totals(scoped: pd.DataFrame) -> dict[str, float]:
    if scoped is not None and not scoped.empty:
        scoped = _without_transfers(scoped)
    if scoped is None or scoped.empty:
        return {"income": 0.0, "expenses": 0.0, "net": 0.0, "count": 0, "budgeted_spent": 0.0}
    amounts = _amounts(scoped)
    income = float(amounts[amounts > 0].sum())
    expenses = float(-amounts[amounts < 0].sum())
    budgeted = scoped["budget_category"].isin(BUDGETED_COMMITTEE_IDS) & (amounts < 0)
    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "count": int(len(scoped)),
        "budgeted_spent": float(-amounts[budgeted].sum()),
    }


def budget_totals(df_budgets: pd.DataFrame | None, df_terms: pd.DataFrame) -> dict[str, float]:
    """Total budgeted-committee allocation per semester."""
    if df_budgets is None or df_budgets.empty or df_terms.empty:
        return {}
    rows = df_budgets[df_budgets["committeeid"].isin(BUDGETED_COMMITTEE_IDS)]
    per_term = rows.groupby("termid")["budget_amount"].sum()
    term_ids = df_terms.drop_duplicates(subset=["Semester"]).set_index("Semester")["TermID"]
    return {semester: float(per_term.get(term_id, 0.0)) for semester, term_id in term_ids.items()}


def same_day_history(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semesters: list[str],
    day: int | None,
    df_budgets: pd.DataFrame | None = None,
) -> dict[str, dict]:
    """
    Totals for each semester through `day` days after its start (None = whole term).

    Tags the table once, not once per semester. Adds `budget` and `used`
    (budgeted spend as % of allocation) when budgets are supplied.
    """
    terms = prepare_terms(df_terms)
    starts = dict(zip(terms["Semester"], terms["start_date"])) if not terms.empty else {}
    tagged = attach_semester(df_transactions, df_terms) if not df_transactions.empty else None
    budgets = budget_totals(df_budgets, df_terms)

    out: dict[str, dict] = {}
    for semester in semesters:
        scoped = None
        if tagged is not None and semester in starts:
            scoped = tagged[tagged["Semester"] == semester]
            if day is not None and not scoped.empty:
                elapsed = (pd.to_datetime(scoped["transaction_date"], errors="coerce") - starts[semester]).dt.days
                scoped = scoped[elapsed <= day]
        totals = _totals(scoped)
        budget = budgets.get(semester, 0.0)
        totals["budget"] = budget
        totals["used"] = (totals["budgeted_spent"] / budget * 100) if budget else None
        out[semester] = totals
    return out


def cumulative_by_day(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
    *,
    measure: str = "net",
    through_day: int | None = None,
) -> pd.DataFrame:
    """
    Running total by day since the term began. Columns: Day, Value.

    `measure` is "net", "income" or "expenses" (expenses are positive).
    One row per day, so two terms line up on the same axis.
    """
    columns = ["Day", "Value"]
    window = date_range_for_semester(df_terms, semester)
    if window is None:
        return pd.DataFrame(columns=columns)
    start, end = window
    length = max((end - start).days, 1)
    last = length if through_day is None else min(max(int(through_day), 0), length)
    index = range(0, last + 1)

    if df_transactions.empty:
        return pd.DataFrame({"Day": list(index), "Value": [0.0] * len(index)})

    tagged = attach_semester(df_transactions, df_terms)
    scoped = _without_transfers(tagged[tagged["Semester"] == semester])
    days = (pd.to_datetime(scoped["transaction_date"], errors="coerce") - start).dt.days
    amounts = _amounts(scoped)
    if measure == "expenses":
        values = -amounts.clip(upper=0)
    elif measure == "income":
        values = amounts.clip(lower=0)
    else:
        values = amounts
    frame = pd.DataFrame({"Day": days, "Value": values}).dropna(subset=["Day"])
    daily = frame.groupby(frame["Day"].astype(int))["Value"].sum()
    series = daily.reindex(index, fill_value=0.0).cumsum()
    return pd.DataFrame({"Day": list(index), "Value": series.to_numpy(dtype=float)})


# --- Committee pace ----------------------------------------------------------

def committee_pace(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    clock: TermClock,
) -> pd.DataFrame:
    """
    % of budget used against % of term elapsed, worst first.

    Pace 1.0 means spending exactly in step with the calendar; 2.0 means twice
    as fast. `Runs out` is the projected exhaustion date at the current rate.
    """
    columns = ["Committee_Name", "Budget", "Spent", "Remaining", "Used", "Pace", "Runs out", "Status"]
    summary = budget_vs_actual(df_transactions, df_budgets, df_terms, clock.semester)
    if summary.empty:
        return pd.DataFrame(columns=columns)
    summary = summary[summary["Budget"] > 0].copy()
    if summary.empty:
        return pd.DataFrame(columns=columns)

    burn = burn_rate_projection(df_transactions, df_budgets, df_terms, clock.semester, as_of=clock.today)
    runs_out = dict(zip(burn["Committee_Name"], burn["Projected Exhaustion"])) if not burn.empty else {}

    summary["Used"] = summary["Spent"] / summary["Budget"] * 100
    summary["Pace"] = summary["Used"] / max(clock.fraction * 100, 1.0)
    summary["Runs out"] = summary["Committee_Name"].map(runs_out)
    return summary.sort_values(["Pace", "Used"], ascending=False)[columns].reset_index(drop=True)


# --- Cash runway -------------------------------------------------------------

@dataclass
class Runway:
    actual: pd.DataFrame       # Day, Balance -- term start through today
    projected: pd.DataFrame    # Day, Mid, Low, High -- today through term end
    today_balance: float
    end_mid: float
    end_low: float
    end_high: float
    anchor: str                # what the balance is tied to, for the caption
    anchored: bool             # True when a recorded statement balance is the anchor


def cash_runway(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    df_balances: pd.DataFrame | None,
    clock: TermClock,
    *,
    prior: str | None = None,
) -> Runway:
    """
    Daily balance so far, and a projected range to the end of the term.

    Balance: the latest recorded statement closing balance per account (on or
    before today), rolled forward and back through the ledger. Without a
    statement it is the ledger's running total, and `anchored` is False.

    Projection, linear from today to term end:
      mid  = today + income still to come - remaining budget x spend share
      low  = today + 75% of income to come - all remaining budget
      high = today + income to come - remaining budget x (spend share - 15 pts)
    "Income to come" is what the prior equivalent term took in after this day;
    "spend share" is the share of its budget that term actually spent.
    """
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(df_transactions.get("transaction_date"), errors="coerce"),
            "amount": _amounts(df_transactions) if not df_transactions.empty else pd.Series(dtype=float),
        }
    ).dropna(subset=["date"])
    frame["date"] = frame["date"].dt.normalize()
    running = frame.groupby("date")["amount"].sum().sort_index().cumsum()

    def through(when: pd.Timestamp) -> float:
        if running.empty:
            return 0.0
        position = running.index.searchsorted(when, side="right")
        return float(running.iloc[position - 1]) if position > 0 else 0.0

    base, anchored, anchor = 0.0, False, "Ledger running total; no statement balance recorded"
    if df_balances is not None and not df_balances.empty:
        balances = df_balances.copy()
        balances["period_end"] = pd.to_datetime(balances["period_end"], errors="coerce")
        balances = balances.dropna(subset=["period_end"])
        usable = balances[balances["period_end"] <= clock.today]
        if usable.empty:
            usable = balances
        if not usable.empty:
            latest = usable.sort_values("period_end").groupby("account").tail(1)
            anchor_date = latest["period_end"].max()
            anchor_total = float(pd.to_numeric(latest["closing_balance"], errors="coerce").fillna(0).sum())
            base = anchor_total - through(anchor_date)
            anchored = True
            anchor = f"Tied to the {anchor_date:%b %d} statement balance"

    days = list(range(0, clock.day + 1))
    actual = pd.DataFrame(
        {"Day": days, "Balance": [base + through(clock.start + pd.Timedelta(days=d)) for d in days]}
    )
    today_balance = float(actual["Balance"].iloc[-1])

    summary = budget_vs_actual(df_transactions, df_budgets, df_terms, clock.semester)
    remaining = float(summary["Remaining"].clip(lower=0).sum()) if not summary.empty else 0.0
    income_to_come, spend_share = 0.0, DEFAULT_SPEND_SHARE
    if prior:
        at_day = same_day_history(df_transactions, df_terms, [prior], clock.day, df_budgets)[prior]
        whole = same_day_history(df_transactions, df_terms, [prior], None, df_budgets)[prior]
        income_to_come = max(whole["income"] - at_day["income"], 0.0)
        if whole["budget"]:
            spend_share = min(max(whole["budgeted_spent"] / whole["budget"], 0.5), 1.0)

    end_mid = today_balance + income_to_come - remaining * spend_share
    end_low = today_balance + income_to_come * 0.75 - remaining
    end_high = today_balance + income_to_come - remaining * max(spend_share - 0.15, 0.4)

    span = max(clock.length - clock.day, 0)
    future = list(range(clock.day, clock.length + 1))

    def path(end_value: float) -> list[float]:
        if span == 0:
            return [today_balance]
        return [today_balance + (end_value - today_balance) * (d - clock.day) / span for d in future]

    projected = pd.DataFrame(
        {"Day": future, "Mid": path(end_mid), "Low": path(end_low), "High": path(end_high)}
    )
    return Runway(actual, projected, today_balance, end_mid, end_low, end_high, anchor, anchored)


# --- Where the money went ----------------------------------------------------

def flow_steps(
    df_transactions: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
    *,
    committee_id: int | None = None,
    top_income: int = 4,
    top_spend: int = 5,
) -> list[tuple[str, float, str]]:
    """
    Waterfall steps: (label, signed amount, kind) with kind in in / out / net.

    Income by purpose, then spending by committee, then the net. The "Other"
    steps make the bars sum exactly to the semester's net.
    """
    if df_transactions.empty:
        return []
    tagged = attach_semester(df_transactions, df_terms)
    scoped = tagged[tagged["Semester"] == semester]
    if committee_id is not None:
        scoped = scoped[scoped["budget_category"] == committee_id]
    scoped = _without_transfers(scoped)
    if scoped.empty:
        return []

    steps: list[tuple[str, float, str]] = []
    income = categorize_flow(scoped, INCOME)
    for _, row in income.head(top_income).iterrows():
        steps.append((str(row["Category"]), float(row["Amount"]), "in"))
    other_income = float(income["Amount"].iloc[top_income:].sum()) if len(income) > top_income else 0.0
    if other_income > 0.005:
        steps.append(("Other income", other_income, "in"))

    amounts = _amounts(scoped)
    total_spent = float(-amounts[amounts < 0].sum())
    spending = spending_by_committee(scoped, budgeted_only=True).head(top_spend)
    for _, row in spending.iterrows():
        steps.append((str(row["Committee_Name"]), -float(row["Spent"]), "out"))
    other_spent = total_spent - float(spending["Spent"].sum()) if not spending.empty else total_spent
    if other_spent > 0.005:
        steps.append(("Other spending", -other_spent, "out"))

    steps.append(("Net", float(amounts.sum()), "net"))
    return steps
