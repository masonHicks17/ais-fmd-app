"""
Module M6 -- alerts.

The README listed "real-time notifications for budget overruns" as a future
enhancement. This is the computation half: a set of rules over current data
returning ranked, actionable alerts. Delivery (email, Slack) would sit on top
without changing anything here.

The distinction from Data Quality (M13): that page reports on the *integrity of
the records*. This reports on the *state of the finances* -- things a treasurer
should act on even when the data is perfectly clean.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import budgets as budget_domain
from . import reconcile
from .money import format_currency
from .terms import attach_semester, date_range_for_semester, ordered_semesters

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# A transaction this many times the median expense is worth a second look.
OUTLIER_MULTIPLE = 8
# Days without any activity before a statement is presumed missing.
STALE_DAYS = 21


@dataclass
class Alert:
    code: str
    severity: str
    title: str
    detail: str
    action: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 9)


def evaluate(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    df_balances: pd.DataFrame | None = None,
    *,
    semester: str | None = None,
) -> list[Alert]:
    """Every alert rule, worst first."""
    semesters = ordered_semesters(df_terms)
    if not semesters:
        return []
    target = semester or semesters[-1]

    alerts: list[Alert] = []
    alerts.extend(_budget_alerts(df_transactions, df_budgets, df_terms, target))
    alerts.extend(_backlog_alerts(df_transactions))
    alerts.extend(_outlier_alerts(df_transactions, df_terms, target))
    alerts.extend(_staleness_alerts(df_transactions, df_terms, target))
    if df_balances is not None:
        alerts.extend(_reconciliation_alerts(df_transactions, df_balances))
    return sorted(alerts, key=lambda alert: alert.rank)


def _budget_alerts(df_transactions, df_budgets, df_terms, semester) -> list[Alert]:
    summary = budget_domain.budget_vs_actual(df_transactions, df_budgets, df_terms, semester)
    if summary.empty:
        return []

    alerts = []
    over = summary[summary["Status"] == "over"]
    for _, row in over.iterrows():
        overspend = row["Spent"] - row["Budget"]
        alerts.append(
            Alert(
                code="budget_over",
                severity="critical",
                title=f"{row['Committee_Name']} is over budget",
                detail=(
                    f"Spent {format_currency(row['Spent'])} against "
                    f"{format_currency(row['Budget'])} — over by "
                    f"{format_currency(overspend)}."
                ),
                action="Reallocate, or raise it at the next treasury meeting.",
            )
        )

    approaching = summary[summary["Status"] == "approaching"]
    for _, row in approaching.iterrows():
        alerts.append(
            Alert(
                code="budget_approaching",
                severity="warning",
                title=f"{row['Committee_Name']} is approaching its budget",
                detail=(
                    f"{row['% Spent']:.0f}% used, "
                    f"{format_currency(row['Remaining'])} remaining."
                ),
                action="Confirm remaining commitments will fit.",
            )
        )

    unbudgeted = summary[summary["Status"] == "unbudgeted"]
    if not unbudgeted.empty:
        names = ", ".join(unbudgeted["Committee_Name"].head(4))
        alerts.append(
            Alert(
                code="unbudgeted_spend",
                severity="warning",
                title=f"{len(unbudgeted)} committee(s) spending with no budget set",
                detail=f"{names} have expenses this term but no allocation.",
                action="Set budgets on the Treasury page.",
            )
        )
    return alerts


def _backlog_alerts(df_transactions) -> list[Alert]:
    if df_transactions.empty:
        return []
    pending = df_transactions[
        df_transactions["budget_category"].isna() | df_transactions["purpose"].isna()
    ]
    if pending.empty:
        return []

    share = len(pending) / len(df_transactions) * 100
    severity = "critical" if share > 30 else "warning" if share > 10 else "info"
    return [
        Alert(
            code="uncategorized_backlog",
            severity=severity,
            title=f"{len(pending):,} transactions awaiting categorisation",
            detail=(
                f"{share:.0f}% of all transactions. Committee spend is understated "
                f"until these are assigned."
            ),
            action="Work through Transactions → Needs attention.",
        )
    ]


def _outlier_alerts(df_transactions, df_terms, semester) -> list[Alert]:
    """Unusually large expenses, judged against the median rather than a fixed threshold."""
    if df_transactions.empty:
        return []
    tagged = attach_semester(df_transactions, df_terms)
    scoped = tagged[(tagged["Semester"] == semester) & (tagged["amount"] < 0)].copy()
    if len(scoped) < 10:
        return []

    scoped["magnitude"] = scoped["amount"].abs()
    median = float(scoped["magnitude"].median())
    if median <= 0:
        return []

    threshold = median * OUTLIER_MULTIPLE
    outliers = scoped[scoped["magnitude"] > threshold]
    if outliers.empty:
        return []

    largest = outliers.nlargest(1, "magnitude").iloc[0]
    return [
        Alert(
            code="large_transactions",
            severity="info",
            title=f"{len(outliers)} unusually large expense(s) this term",
            detail=(
                f"More than {OUTLIER_MULTIPLE}x the typical "
                f"{format_currency(median)} expense. Largest is "
                f"{format_currency(largest['magnitude'])} on "
                f"{pd.to_datetime(largest['transaction_date']):%b %d}."
            ),
            action="Confirm each has a receipt and the right committee.",
        )
    ]


def _staleness_alerts(df_transactions, df_terms, semester) -> list[Alert]:
    """A long silence usually means a statement was never uploaded."""
    window = date_range_for_semester(df_terms, semester)
    if window is None or df_transactions.empty:
        return []

    start, end = window
    today = pd.Timestamp.today().normalize()
    if today < start:
        return []

    tagged = attach_semester(df_transactions, df_terms)
    scoped = tagged[tagged["Semester"] == semester]
    if scoped.empty:
        return []

    latest = pd.to_datetime(scoped["transaction_date"], errors="coerce").max()
    if pd.isna(latest):
        return []

    reference = min(today, end)
    gap = (reference - latest).days
    if gap < STALE_DAYS:
        return []

    return [
        Alert(
            code="stale_data",
            severity="warning",
            title=f"No activity recorded for {gap} days",
            detail=(
                f"The most recent transaction in {semester} is "
                f"{latest:%b %d, %Y}. An active account rarely goes this quiet."
            ),
            action="Upload the latest statement.",
        )
    ]


def _reconciliation_alerts(df_transactions, df_balances) -> list[Alert]:
    results = reconcile.reconcile_all(df_transactions, df_balances)
    unbalanced = [result for result in results if not result.balanced]
    if not unbalanced:
        return []

    total = sum(abs(result.discrepancy) for result in unbalanced)
    worst = max(unbalanced, key=lambda r: abs(r.discrepancy))
    return [
        Alert(
            code="unreconciled",
            severity="critical",
            title=f"{len(unbalanced)} statement period(s) do not reconcile",
            detail=(
                f"{format_currency(total)} unexplained in total. Largest gap is "
                f"{format_currency(abs(worst.discrepancy))} on {worst.account} "
                f"({worst.period_start:%b %Y})."
            ),
            action="Open Reconciliation to find the missing transactions.",
        )
    ]


def to_frame(alerts: list[Alert]) -> pd.DataFrame:
    columns = ["Severity", "Alert", "Detail", "Suggested action"]
    if not alerts:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(
        [
            {
                "Severity": alert.severity,
                "Alert": alert.title,
                "Detail": alert.detail,
                "Suggested action": alert.action,
            }
            for alert in alerts
        ],
        columns=columns,
    )
