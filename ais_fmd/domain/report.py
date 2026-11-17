"""
Module M12 -- board pack.

One click to a self-contained semester report: headline figures, budget against
actual, largest expenses, dues collection, and anything currently alerting.

Produced as standalone HTML rather than PDF so it has no binary dependency --
it opens in any browser and prints to PDF from there. The print stylesheet is
included, so "print to PDF" produces something presentable.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import pandas as pd

from . import alerts as alerts_domain
from . import budgets as budget_domain
from . import dues as dues_domain
from .money import format_currency
from .terms import attach_semester, date_range_for_semester, previous_semester


@dataclass
class BoardPack:
    semester: str
    generated: pd.Timestamp
    totals: dict
    prior_totals: dict
    budget: pd.DataFrame
    largest_expenses: pd.DataFrame
    dues: dues_domain.DuesSummary
    alerts: list = field(default_factory=list)
    narrative: list[str] = field(default_factory=list)


def build(
    df_transactions: pd.DataFrame,
    df_budgets: pd.DataFrame,
    df_terms: pd.DataFrame,
    semester: str,
    df_balances: pd.DataFrame | None = None,
) -> BoardPack:
    totals = budget_domain.semester_totals(df_transactions, df_terms, semester)
    prior = previous_semester(df_terms, semester)
    prior_totals = (
        budget_domain.semester_totals(df_transactions, df_terms, prior)
        if prior
        else {"income": 0.0, "expenses": 0.0, "net": 0.0, "count": 0}
    )

    budget = budget_domain.budget_vs_actual(df_transactions, df_budgets, df_terms, semester)

    tagged = attach_semester(df_transactions, df_terms)
    scoped = tagged[tagged["Semester"] == semester].copy()
    largest = pd.DataFrame(columns=["Date", "Amount", "Details", "Committee"])
    if not scoped.empty:
        from .budgets import is_transfer

        # A transfer to savings is not an expense the board needs to read about.
        expenses = scoped[(scoped["amount"] < 0) & ~is_transfer(scoped)].copy()
        if not expenses.empty:
            from ..config.categories import committee_name

            expenses["Amount"] = expenses["amount"].abs()
            expenses["Committee"] = expenses["budget_category"].map(committee_name)
            expenses["Date"] = pd.to_datetime(expenses["transaction_date"]).dt.strftime("%Y-%m-%d")
            largest = (
                expenses.nlargest(10, "Amount")[["Date", "Amount", "details", "Committee"]]
                .rename(columns={"details": "Details"})
                .reset_index(drop=True)
            )

    pack = BoardPack(
        semester=semester,
        generated=pd.Timestamp.now(),
        totals=totals,
        prior_totals=prior_totals,
        budget=budget,
        largest_expenses=largest,
        dues=dues_domain.summarize(df_transactions, df_terms, semester),
        alerts=alerts_domain.evaluate(
            df_transactions, df_budgets, df_terms, df_balances, semester=semester
        ),
    )
    pack.narrative = _narrative(pack, prior)
    return pack


def _narrative(pack: BoardPack, prior: str | None) -> list[str]:
    """Plain-English commentary, generated from the figures rather than written."""
    lines = []
    totals, prior_totals = pack.totals, pack.prior_totals

    direction = "a surplus" if totals["net"] >= 0 else "a deficit"
    lines.append(
        f"{pack.semester} closed with {direction} of "
        f"{format_currency(abs(totals['net']))} — {format_currency(totals['income'])} "
        f"in and {format_currency(totals['expenses'])} out across "
        f"{totals['count']:,} transactions."
    )

    if prior and prior_totals["count"]:
        change = totals["expenses"] - prior_totals["expenses"]
        word = "more" if change > 0 else "less"
        lines.append(
            f"Spending was {format_currency(abs(change))} {word} than {prior}, "
            f"and income moved from {format_currency(prior_totals['income'])} to "
            f"{format_currency(totals['income'])}."
        )

    if not pack.budget.empty:
        over = pack.budget[pack.budget["Status"] == "over"]
        if len(over):
            names = ", ".join(over["Committee_Name"])
            lines.append(f"{len(over)} committee(s) finished over budget: {names}.")
        else:
            lines.append("Every committee finished within its allocation.")

    if pack.dues.payment_count:
        lines.append(
            f"Dues brought in {format_currency(pack.dues.total_collected)} from "
            f"{_plural(pack.dues.payment_count, 'payment')} by "
            f"{_plural(pack.dues.unique_payers, 'member')}."
        )

    critical = [alert for alert in pack.alerts if alert.severity == "critical"]
    if critical:
        lines.append(
            f"{_plural(len(critical), 'item')} need attention before this period "
            f"is considered closed."
        )

    return lines


def _plural(count: int, noun: str) -> str:
    """"1 payment" / "5 payments" -- a report that says "1 payments" reads as a draft."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


# --- HTML rendering ----------------------------------------------------------

def _table(df: pd.DataFrame, currency_columns: tuple[str, ...] = ()) -> str:
    if df.empty:
        return '<p class="muted">Nothing to report.</p>'

    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for _, row in df.iterrows():
        cells = []
        for column in df.columns:
            value = row[column]
            if column in currency_columns:
                text = format_currency(value)
            elif isinstance(value, float):
                text = f"{value:,.1f}" if value == value else "—"
            else:
                text = "—" if value is None or value != value else str(value)
            numeric = column in currency_columns or isinstance(value, (int, float))
            cells.append(
                f'<td class="{"num" if numeric else ""}">{html.escape(text)}</td>'
            )
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def to_html(pack: BoardPack, organisation: str = "UF AIS") -> str:
    totals = pack.totals

    metrics = "".join(
        f'<div class="metric"><span class="label">{label}</span>'
        f'<span class="value">{value}</span></div>'
        for label, value in (
            ("Income", format_currency(totals["income"])),
            ("Expenses", format_currency(totals["expenses"])),
            ("Net", format_currency(totals["net"])),
            ("Transactions", f"{totals['count']:,}"),
        )
    )

    narrative = "".join(f"<p>{html.escape(line)}</p>" for line in pack.narrative)

    alert_rows = "".join(
        f'<li><strong>{html.escape(alert.title)}</strong> — '
        f"{html.escape(alert.detail)}</li>"
        for alert in pack.alerts
        if alert.severity in {"critical", "warning"}
    )
    alerts_section = (
        f"<ul>{alert_rows}</ul>" if alert_rows else '<p class="muted">Nothing outstanding.</p>'
    )

    budget_view = pack.budget.copy()
    if not budget_view.empty:
        budget_view = budget_view[
            ["Committee_Name", "Budget", "Spent", "Remaining", "% Spent", "Status"]
        ].rename(columns={"Committee_Name": "Committee"})

    dues_line = (
        f"{format_currency(pack.dues.total_collected)} from "
        f"{pack.dues.payment_count} payments by {pack.dues.unique_payers} members"
        if pack.dues.payment_count
        else "No dues recorded this period."
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<title>{html.escape(organisation)} — {html.escape(pack.semester)} financial report</title>
<style>
  :root {{ --ink:#16202a; --muted:#5f6f7a; --rule:#d8e0e5; --accent:#0b6f9a; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:Georgia,'Times New Roman',serif; color:var(--ink);
         max-width:46rem; margin:0 auto; padding:3rem 1.5rem 5rem; line-height:1.6; }}
  header {{ border-bottom:2px solid var(--ink); padding-bottom:1rem; margin-bottom:2rem; }}
  h1 {{ font-size:1.9rem; margin:0 0 .3rem; letter-spacing:-.01em; }}
  h2 {{ font-size:1.15rem; margin:2.5rem 0 .8rem; padding-bottom:.3rem;
        border-bottom:1px solid var(--rule); }}
  .eyebrow {{ font-family:system-ui,sans-serif; font-size:.72rem; letter-spacing:.14em;
              text-transform:uppercase; color:var(--accent); margin-bottom:.6rem; }}
  .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
              background:var(--rule); border:1px solid var(--rule); margin:1.5rem 0; }}
  .metric {{ background:#fff; padding:.9rem 1rem; }}
  .metric .label {{ display:block; font-family:system-ui,sans-serif; font-size:.7rem;
                    text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
  .metric .value {{ display:block; font-size:1.3rem; margin-top:.25rem;
                    font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-family:system-ui,sans-serif;
           font-size:.85rem; margin:.5rem 0 1rem; }}
  th {{ text-align:left; font-size:.68rem; text-transform:uppercase; letter-spacing:.07em;
        color:var(--muted); border-bottom:1px solid var(--ink); padding:.45rem .5rem; }}
  td {{ padding:.45rem .5rem; border-bottom:1px solid var(--rule); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  ul {{ font-family:system-ui,sans-serif; font-size:.88rem; padding-left:1.1rem; }}
  li {{ margin-bottom:.4rem; }}
  .muted {{ color:var(--muted); font-style:italic; }}
  footer {{ margin-top:3rem; padding-top:1rem; border-top:1px solid var(--rule);
            font-family:system-ui,sans-serif; font-size:.75rem; color:var(--muted); }}
  @media print {{
    body {{ padding:0; max-width:none; }}
    h2 {{ page-break-after:avoid; }}
    table {{ page-break-inside:avoid; }}
  }}
</style></head><body>
<header>
  <div class="eyebrow">{html.escape(organisation)} — Treasury report</div>
  <h1>{html.escape(pack.semester)}</h1>
  <div class="muted">Generated {pack.generated:%B %d, %Y at %H:%M}</div>
</header>

<div class="metrics">{metrics}</div>

<h2>Summary</h2>
{narrative}

<h2>Budget against actual</h2>
{_table(budget_view, currency_columns=("Budget", "Spent", "Remaining"))}

<h2>Largest expenses</h2>
{_table(pack.largest_expenses, currency_columns=("Amount",))}

<h2>Dues</h2>
<p>{html.escape(dues_line)}</p>

<h2>Outstanding items</h2>
{alerts_section}

<footer>
  Generated by the UF AIS Financial Management System. Figures are drawn directly
  from recorded transactions; confirm against bank statements before external use.
</footer>
</body></html>"""
