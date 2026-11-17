"""
Module M15 -- the assistant, rebuilt on tools.

FINDING F10. The original routed questions through an if/elif chain that tested
for the word "committee" *before* it tested for spending, income or budget, and
returned early with just the committee roster. So the app's own first advertised
example question -- "What's the total spending for the Marketing committee this
semester?" -- never reached the spending branch. Gemini received a list of
committee names with no financial data attached and answered from that.

Each capability is a *tool* with declared parameters. Something picks a tool
and its arguments -- the offline keyword router below, or a language model in
`assistant_model.py` -- and `run_tool` runs it. The tool computes the answer
from the data. The model never writes the answer and never sees the
transaction table, so:

  * every number in an answer was produced by a tool (the answer text *is*
    the tool's output);
  * who may see what is decided here, in `run_tool`, not by the model. A model
    that is talked into asking for another committee's figures gets a refusal
    from code, not from its own good judgement;
  * a question no tool covers gets "I don't know", never a different question's
    answer.

Roles are plain strings ("member", "officer", "treasurer", "admin") because this
module must not import `auth`, which imports Streamlit.

WHO SEES WHAT (an assumption for the treasurer to confirm):
  * Organisation-wide totals (all income, all spending, dues collected): anyone
    signed in. The Dashboard already shows these to members.
  * One committee's figures, a per-committee breakdown, or a list of
    transactions: treasurers and admins for any committee; an officer for their
    own committee only; members not at all.
  * Names of people who have not paid dues: treasurers and admins only, as on
    the Roster page.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field, replace
from typing import Callable

import pandas as pd

from ..config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from . import assistant_pages as pages
from . import budgets as budget_domain
from . import dues as dues_domain
from .money import format_currency
from .terms import attach_semester, date_range_for_semester, ordered_semesters

ROLE_RANK = pages.ROLE_RANK

ANSWERED = "answered"
REFUSED = "refused"
UNKNOWN = "unknown"


@dataclass
class Answer:
    text: str
    table: pd.DataFrame | None = None
    chart_hint: str | None = None
    tool: str = ""
    arguments: dict = field(default_factory=dict)
    status: str = ANSWERED
    source: str | None = None  # the page where the user can check the figure


@dataclass(frozen=True)
class Scope:
    """Who is asking. Defaults to the least access, so forgetting it fails closed."""

    role: str = "member"
    committee: str | None = None  # an officer's own committee

    @property
    def sees_every_committee(self) -> bool:
        return ROLE_RANK.get(self.role, 0) >= ROLE_RANK["treasurer"]


@dataclass
class Context:
    transactions: pd.DataFrame
    budgets: pd.DataFrame
    terms: pd.DataFrame
    scope: Scope = field(default_factory=Scope)
    as_of: pd.Timestamp | None = None  # "today"; fixed in tests so answers are repeatable
    # Membership roster per semester, as `roster.Member` objects. Only needed
    # for `unpaid_dues`, and only ever loaded for treasurers.
    members: dict[str, list] = field(default_factory=dict)

    def today(self) -> pd.Timestamp:
        if self.as_of is not None:
            return pd.Timestamp(self.as_of).normalize()
        return pd.Timestamp.today().normalize()


COMMITTEE_NAMES: tuple[str, ...] = tuple(committee_name(cid) for cid in BUDGETED_COMMITTEE_IDS)


# --- Shared helpers -----------------------------------------------------------

def current_semester(context: Context) -> str | None:
    """The term containing today, else the most recent one that has started."""
    order = ordered_semesters(context.terms)
    today = context.today()
    started = None
    for semester in order:
        window = date_range_for_semester(context.terms, semester)
        if window is None or pd.isna(window[0]):
            continue
        start, end = window
        if start <= today <= end:
            return semester
        if start <= today:
            started = semester
    return started or (order[-1] if order else None)


def canonical_committee(name: object) -> str | None:
    """'marketing ' -> 'Marketing'. None when it is not a budgeted committee."""
    if not isinstance(name, str):
        return None
    lowered = name.strip().lower()
    return next((c for c in COMMITTEE_NAMES if c.lower() == lowered), None)


def _dates(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["transaction_date"], errors="coerce")


def _in_semester(context: Context, semester: str | None) -> pd.DataFrame:
    if not semester:
        return context.transactions
    tagged = attach_semester(context.transactions, context.terms)
    return tagged[tagged["Semester"] == semester]


def _in_range(frame: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    dates = _dates(frame)
    mask = pd.Series(True, index=frame.index)
    if start:
        mask &= dates >= pd.Timestamp(start)
    if end:
        mask &= dates <= pd.Timestamp(end)
    return frame[mask]


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


def _day(value: str) -> str:
    stamp = pd.Timestamp(value)
    return f"{stamp.strftime('%b')} {stamp.day}, {stamp.year}"


def _window_label(args: dict) -> str:
    if args.get("start") or args.get("end"):
        start = _day(args["start"]) if args.get("start") else "the start"
        end = _day(args["end"]) if args.get("end") else "today"
        return f" from {start} to {end}"
    if args.get("semester"):
        return f" in {args['semester']}"
    return " across all terms"


def _scoped_rows(context: Context, args: dict) -> pd.DataFrame:
    """Rows for the requested window: a date range wins over a semester."""
    if args.get("start") or args.get("end"):
        return _in_range(context.transactions, args.get("start"), args.get("end"))
    return _in_semester(context, args.get("semester"))


def _committee_rows(frame: pd.DataFrame, committee: str) -> pd.DataFrame:
    return frame[frame["budget_category"].map(committee_name) == committee]


def _expenses(frame: pd.DataFrame) -> pd.DataFrame:
    """Money out, excluding transfers between the chapter's own accounts."""
    out = frame[frame["amount"] < 0]
    if "budget_category" in out.columns:
        out = out[~budget_domain.is_transfer(out)]
    return out


def _transaction_table(frame: pd.DataFrame) -> pd.DataFrame:
    shown = frame.copy()
    shown["Committee"] = shown["budget_category"].map(committee_name)
    return shown[["transaction_date", "amount", "details", "Committee", "account"]].rename(
        columns={"transaction_date": "Date", "amount": "Amount", "details": "Details", "account": "Account"}
    )


# A VP's own version of an org-wide page.
_VP_EQUIVALENT = {"Transactions": "My Transactions", "Dashboard": "My Committee"}


def _verify_page(context: Context, preferred: str, fallback: str = "Dashboard") -> str | None:
    """
    The page to check a figure on: the preferred one if this user can open it,
    else the fallback if they can, else My Committee for a VP, else none -- never
    a page this user's navigation does not have.
    """
    role = context.scope.role
    if role == "officer":
        preferred = _VP_EQUIVALENT.get(preferred, preferred)
    for name in (preferred, fallback, "My Committee"):
        page = pages.by_name(name)
        if page is not None and page.can_open(role):
            return page.path
    return None


def _committee_page(context: Context) -> str:
    if context.scope.sees_every_committee:
        return _verify_page(context, "Dashboard")
    return _verify_page(context, "My Committee")


# --- Tools --------------------------------------------------------------------
#
# Each tool takes already-validated arguments and the context, and returns an
# Answer. Permission checks happen in `run_tool` before any tool is called.

def tool_committee_spending(args: dict, context: Context) -> Answer:
    rows = _expenses(_scoped_rows(context, args))
    window = _window_label(args)
    committee = args.get("committee")

    if committee:
        total = float(_committee_rows(rows, committee)["amount"].abs().sum())
        text = f"**{committee}** spent **{format_currency(total)}**{window}."
        semester = args.get("semester")
        if semester and not (args.get("start") or args.get("end")):
            summary = budget_domain.budget_vs_actual(context.transactions, context.budgets, context.terms, semester)
            match = summary[summary["Committee_Name"] == committee] if not summary.empty else summary
            if not match.empty and float(match["Budget"].iloc[0]) > 0:
                allocated = float(match["Budget"].iloc[0])
                text += f" Its budget is {format_currency(allocated)} ({total / allocated * 100:.1f}% used)."
        return Answer(text=text, chart_hint="committee_spending", source=_committee_page(context))

    budgeted = rows[rows["budget_category"].isin(BUDGETED_COMMITTEE_IDS)]
    total = float(budgeted["amount"].abs().sum())
    text = f"Total committee spending{window} was **{format_currency(total)}**."
    if not context.scope.sees_every_committee:
        return Answer(text=text, source=_verify_page(context, "Dashboard"))

    spending = budget_domain.spending_by_committee(budgeted)
    if not spending.empty:
        leader = spending.iloc[0]
        text += f" The largest was **{leader['Committee_Name']}** at {format_currency(leader['Spent'])}."
    return Answer(text=text, table=spending, chart_hint="committee_spending",
                  source=_verify_page(context, "Dashboard"))


def tool_budget_status(args: dict, context: Context) -> Answer:
    semester = args.get("semester") or current_semester(context)
    if not semester:
        return Answer(text="No terms are defined, so there is no budget to report on.", status=UNKNOWN)
    args["semester"] = semester

    summary = budget_domain.budget_vs_actual(context.transactions, context.budgets, context.terms, semester)
    committee = args.get("committee")
    if summary.empty:
        return Answer(text=f"I don't have budget data for {semester}.", status=UNKNOWN)

    if committee:
        match = summary[summary["Committee_Name"] == committee]
        if match.empty or float(match["Budget"].iloc[0]) <= 0:
            return Answer(
                text=f"I don't have a {semester} budget for **{committee}**, so I can't say what is left.",
                status=UNKNOWN,
                source=_committee_page(context),
            )
        row = match.iloc[0]
        budget, spent = float(row["Budget"]), float(row["Spent"])
        remaining = budget - spent
        position = (
            f"has **{format_currency(remaining)} left** of"
            if remaining >= 0
            else f"is **{format_currency(-remaining)} over**"
        )
        text = (
            f"**{committee}** {position} its {semester} budget: "
            f"{format_currency(spent)} spent of {format_currency(budget)} ({spent / budget * 100:.1f}% used)."
        )
        return Answer(text=text, table=match, chart_hint="budget", source=_committee_page(context))

    total_budget = float(summary["Budget"].sum())
    total_spent = float(summary["Spent"].sum())
    text = (
        f"For **{semester}**, {format_currency(total_spent)} of "
        f"{format_currency(total_budget)} allocated has been spent."
    )
    if not context.scope.sees_every_committee:
        return Answer(text=text, source=_verify_page(context, "Dashboard"))

    over = summary[summary["Status"] == "over"]
    approaching = summary[summary["Status"] == "approaching"]
    if len(over):
        text += f" **{len(over)} over budget**: {', '.join(over['Committee_Name'])}."
    if len(approaching):
        text += f" {len(approaching)} approaching the limit: {', '.join(approaching['Committee_Name'])}."
    if not len(over) and not len(approaching):
        text += " Every committee is within budget."
    return Answer(text=text, table=summary, chart_hint="budget", source=_verify_page(context, "Dashboard"))


def tool_committee_briefing(args: dict, context: Context) -> Answer:
    """
    The quick summary an officer wants before a meeting: where the money
    stands, whether spending is on pace for the point in the term, and what was
    charged recently.
    """
    from .pace import term_clock

    semester = args.get("semester") or current_semester(context)
    if not semester:
        return Answer(text="No terms are defined, so there is nothing to summarise.", status=UNKNOWN)
    args["semester"] = semester
    committee = args.get("committee")
    clock = term_clock(context.terms, semester, as_of=context.today())
    elapsed = clock.fraction * 100 if clock else None

    if not committee:
        status = tool_budget_status({"semester": semester}, context)
        if elapsed is not None and status.status == ANSWERED:
            status.text += f" The term is {elapsed:.0f}% complete."
        return status

    summary = budget_domain.budget_vs_actual(context.transactions, context.budgets, context.terms, semester)
    match = summary[summary["Committee_Name"] == committee] if not summary.empty else summary
    if match.empty:
        return Answer(text=f"I don't have {semester} figures for **{committee}**.", status=UNKNOWN)
    budget, spent = float(match["Budget"].iloc[0]), float(match["Spent"].iloc[0])

    lines = [f"**{committee}, {semester}**"]
    if budget > 0:
        used = spent / budget * 100
        remaining = budget - spent
        lines.append(
            f"Spent {format_currency(spent)} of {format_currency(budget)} ({used:.0f}% used); "
            + (f"{format_currency(remaining)} left." if remaining >= 0
               else f"{format_currency(-remaining)} over budget.")
        )
        if elapsed is not None:
            if remaining < 0:
                verdict = ""
            elif used > elapsed + 10:
                verdict = "Spending is ahead of pace."
            elif used < elapsed - 25:
                verdict = "Spending is well under pace."
            else:
                verdict = "Spending is on pace."
            lines.append(f"The term is {elapsed:.0f}% complete. {verdict}".strip())
    else:
        lines.append(f"Spent {format_currency(spent)}. No budget is set for this term.")

    recent = _expenses(_committee_rows(_in_semester(context, semester), committee))
    recent = recent[_dates(recent) <= context.today()].sort_values("transaction_date", ascending=False)
    table = None
    if not recent.empty:
        latest = recent.iloc[0]
        lines.append(
            f"{_count(len(recent), 'charge')} so far; the latest was {format_currency(abs(latest['amount']))} "
            f"on {_day(latest['transaction_date'])}."
        )
        table = _transaction_table(recent.head(5))
    else:
        lines.append("No charges yet this term.")

    return Answer(text="  \n".join(lines), table=table, source=_committee_page(context))


def tool_recent_charges(args: dict, context: Context) -> Answer:
    committee = args["committee"]
    limit = int(args.get("limit") or 10)
    rows = _expenses(_committee_rows(_scoped_rows(context, args), committee))
    rows = rows[_dates(rows) <= context.today()].sort_values("transaction_date", ascending=False)
    window = _window_label(args) if (args.get("start") or args.get("end") or args.get("semester")) else ""
    if rows.empty:
        return Answer(text=f"I found no charges to **{committee}**{window}.",
                      source=_verify_page(context, "Transactions"))
    shown = rows.head(limit)
    return Answer(
        text=(
            f"The {_count(len(shown), 'most recent charge')} to **{committee}**{window}, totalling "
            f"{format_currency(shown['amount'].abs().sum())}. The latest was "
            f"{format_currency(abs(shown.iloc[0]['amount']))} on {_day(shown.iloc[0]['transaction_date'])}."
        ),
        table=_transaction_table(shown),
        source=_verify_page(context, "Transactions"),
    )


def tool_income_summary(args: dict, context: Context) -> Answer:
    income = _scoped_rows(context, args)
    income = income[income["amount"] > 0]
    if "budget_category" in income.columns:
        income = income[~budget_domain.is_transfer(income)]  # transfers are not income
    account = args.get("account")
    if account:
        income = income[income["account"].astype(str).str.contains(account.split()[0], case=False, na=False)]

    total = float(income["amount"].sum()) if not income.empty else 0.0
    via = f" via {account}" if account else ""
    # An officer's context holds only their committee's rows (`visible_to_asker`).
    whose = f"{context.scope.committee} income" if context.scope.role == "officer" else "Income"
    text = (
        f"{whose}{via}{_window_label(args)} totalled **{format_currency(total)}** "
        f"across {_count(len(income), 'transaction')}."
    )
    if not context.scope.sees_every_committee:
        return Answer(text=text, source=_verify_page(context, "Dashboard"))

    by_purpose = budget_domain.categorize_flow(income, budget_domain.INCOME)
    if not by_purpose.empty:
        leader = by_purpose.iloc[0]
        text += f" The largest source was **{leader['Category']}** at {format_currency(leader['Amount'])}."
    return Answer(text=text, table=by_purpose, chart_hint="income", source=_verify_page(context, "Dashboard"))


def tool_dues_summary(args: dict, context: Context) -> Answer:
    semester = args.get("semester") or current_semester(context)
    if not semester:
        return Answer(text="No terms are defined, so there are no dues to report.", status=UNKNOWN)
    args["semester"] = semester
    summary = dues_domain.summarize(context.transactions, context.terms, semester)
    if not summary.payment_count:
        return Answer(text=f"I found no dues payments for {semester}.", source=_verify_page(context, "Dues"))
    return Answer(
        text=(
            f"**{semester} dues:** {format_currency(summary.total_collected)} collected from "
            f"{summary.payment_count:,} payments by {summary.unique_payers:,} people."
        ),
        source=_verify_page(context, "Dues"),
    )


def tool_unpaid_dues(args: dict, context: Context) -> Answer:
    from . import roster as roster_domain

    roster_page = pages.by_name("Roster").path
    semester = args.get("semester") or current_semester(context)
    args["semester"] = semester
    members = context.members.get(semester or "", [])
    if not members:
        return Answer(
            text=(
                f"No membership roster is uploaded for {semester}, so I can't tell who hasn't paid. "
                f"Upload one on {roster_page}."
            ),
            status=UNKNOWN,
            source=roster_page,
        )
    paid = dues_domain.dues_transactions(context.transactions, dues_domain.schedule_from_terms(context.terms))
    if not paid.empty:
        paid = attach_semester(paid, context.terms)
        paid = paid[paid["Semester"].astype(str) == str(semester)]
    result = roster_domain.reconcile(paid, members)
    summary = result.summary()
    return Answer(
        text=(
            f"**{summary['unpaid']:,} of {summary['expected']:,}** members on the {semester} roster "
            f"haven't paid ({summary['rate']:.0f}% collected)."
        ),
        table=roster_domain.unpaid_frame(result),
        source=roster_page,
    )


def tool_largest_transactions(args: dict, context: Context) -> Answer:
    rows = _scoped_rows(context, args).copy()
    if args.get("committee"):
        rows = _committee_rows(rows, args["committee"])
    if args.get("account"):
        rows = rows[rows["account"].astype(str).str.contains(args["account"].split()[0], case=False, na=False)]
    rows["magnitude"] = rows["amount"].abs()
    threshold = args.get("threshold")
    if threshold:
        rows = rows[rows["magnitude"] >= float(threshold)]

    qualifier = f" over {format_currency(threshold)}" if threshold else ""
    who = f" for **{args['committee']}**" if args.get("committee") else ""
    on = f" on {args['account']}" if args.get("account") else ""
    window = _window_label(args) if (args.get("semester") or args.get("start") or args.get("end")) else ""
    if rows.empty:
        return Answer(text=f"I found no transactions{qualifier}{who}{on}{window}.",
                      source=_verify_page(context, "Transactions"))
    ranked = rows.sort_values("magnitude", ascending=False)
    return Answer(
        text=(
            f"Found **{_count(len(ranked), 'transaction')}**{qualifier}{who}{on}{window}. The largest is "
            f"{format_currency(abs(ranked.iloc[0]['amount']))} "
            f"{'received' if ranked.iloc[0]['amount'] > 0 else 'spent'}."
        ),
        table=_transaction_table(ranked.head(15)),
        source=_verify_page(context, "Transactions"),
    )


def tool_uncategorized(args: dict, context: Context) -> Answer:
    frame = context.transactions
    pending = frame[frame["budget_category"].isna() | frame["purpose"].isna()]
    review = pages.by_name("Needs attention")
    if context.scope.role == "officer":
        # Only this committee's rows are here, so org-wide uncategorized charges
        # are invisible -- say what the count covers instead of implying none exist.
        if pending.empty:
            return Answer(text=f"Every **{context.scope.committee}** charge has a purpose. "
                               "Charges not yet given a committee are the treasurer's to sort.")
        return Answer(text=f"**{len(pending):,}** {context.scope.committee} charges have no purpose yet. "
                           "The treasurer works through these on Needs attention.")
    if pending.empty:
        return Answer(text="Every transaction has a committee and a purpose.", source=review.path)
    text = (
        f"**{len(pending):,}** transactions are uncategorized, worth "
        f"{format_currency(pending['amount'].abs().sum())} in absolute value."
    )
    if not context.scope.sees_every_committee:
        return Answer(text=text + " The treasurer works through these on Needs attention.", source=None)
    return Answer(
        text=text + f" Work through them on {review.path}.",
        table=_transaction_table(pending.head(50)),
        source=review.path,
    )


def tool_compare_semesters(args: dict, context: Context) -> Answer:
    order = ordered_semesters(context.terms)
    chosen = [s for s in (args.get("semesters") or []) if s in order]
    if len(chosen) < 2:
        upto = current_semester(context)
        known = order[: order.index(upto) + 1] if upto in order else order
        chosen = known[-2:]
    if len(chosen) < 2:
        return Answer(text="At least two terms are needed to compare.", status=UNKNOWN)
    args["semesters"] = chosen

    rows = []
    for semester in chosen:
        totals = budget_domain.semester_totals(context.transactions, context.terms, semester)
        rows.append({"Semester": semester, "Income": totals["income"], "Expenses": totals["expenses"],
                     "Net": totals["net"], "Transactions": totals["count"]})
    frame = pd.DataFrame(rows)
    first, last = frame.iloc[0], frame.iloc[-1]
    change = last["Expenses"] - first["Expenses"]
    return Answer(
        text=(
            f"Comparing **{first['Semester']}** with **{last['Semester']}**: spending was "
            f"{format_currency(abs(change))} {'more' if change > 0 else 'less'}, and net went from "
            f"{format_currency(first['Net'])} to {format_currency(last['Net'])}."
        ),
        table=frame,
        chart_hint="comparison",
        source=_verify_page(context, "Dashboard"),
    )


def tool_find_page(args: dict, context: Context) -> Answer:
    topic = str(args.get("topic") or "")
    matches = pages.find(topic)
    role = context.scope.role
    if not matches:
        openable = ", ".join(p.name for p in pages.PAGES if p.can_open(role))
        return Answer(text=f"I'm not sure which page covers that. Pages you can open: {openable}.",
                      status=UNKNOWN)
    best = matches[0]
    if best.can_open(role):
        return Answer(text=f"Go to **{best.path}**. It shows {best.shows}.", source=best.path)

    if role == "officer":
        # Rank is not the reason here: the page shows every committee, so it is
        # not in a VP's menu. Point to their own committee's version instead.
        own = pages.by_name(_VP_EQUIVALENT.get(best.name, "My Committee"))
        return Answer(
            text=f"That page covers every committee, so it isn't in a VP's menu. "
                 f"For your committee, go to **{own.path}**. It shows {own.shows}.",
            source=own.path,
        )

    text = f"That's on **{best.path}**, which only the {best.min_role} can open."
    alternative = next((p for p in matches[1:] if p.can_open(role)), None)
    if alternative is not None:
        text += f" You can see {alternative.shows} on **{alternative.path}**."
    return Answer(text=text, source=alternative.path if alternative else None)


def tool_cannot_answer(args: dict, context: Context) -> Answer:
    reason = str(args.get("reason") or "").strip()
    text = "I don't know. " + (reason + " " if reason else "That isn't something the app's data covers. ")
    text += (
        "I can answer questions about committee spending and budgets, recent charges, income, "
        "dues, and where things are in the app."
    )
    return Answer(text=text, status=UNKNOWN)


# --- The tool registry ---------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    keywords: tuple[str, ...]
    run: Callable[[dict, Context], Answer]
    parameters: tuple[str, ...] = ()
    # "org": organisation-wide totals; "list": transaction listings;
    # "names": people; "nav": wayfinding and "I don't know" only.
    access: str = "org"
    needs_committee: bool = False
    # Higher wins ties in the keyword router; specific tools sit above general ones.
    specificity: int = 0


TOOLS: tuple[Tool, ...] = (
    Tool(
        "find_page",
        "Say which page of the app shows something. Use for 'where do I see/find...' questions.",
        ("where do i", "where can i", "where is", "where are", "where would", "which page", "what page",
         "how do i find", "where to find", "where's"),
        tool_find_page, ("topic",), access="nav", specificity=6,
    ),
    Tool(
        "unpaid_dues",
        "List members on the roster who have not paid dues this term (treasurer only).",
        ("unpaid", "hasn't paid", "haven't paid", "has not paid", "have not paid", "not paid",
         "still owe", "owes dues", "owe dues", "who hasn't"),
        tool_unpaid_dues, ("semester",), access="names", specificity=5,
    ),
    Tool(
        "compare_semesters",
        "Compare income, expenses and net between two terms.",
        ("compare", "versus", " vs ", "difference between"),
        tool_compare_semesters, ("semesters",), specificity=4,
    ),
    Tool(
        "uncategorized",
        "Count transactions still missing a committee or purpose.",
        ("uncategorized", "uncategorised", "missing category", "not categorized", "need review", "needs review"),
        tool_uncategorized, (), specificity=4,
    ),
    Tool(
        "dues_summary",
        "Dues collected in a term: total, number of payments, number of payers.",
        ("dues", "membership fee", "membership fees"),
        tool_dues_summary, ("semester",), specificity=3,
    ),
    Tool(
        "committee_briefing",
        "A quick summary of one committee's term: budget, spent, left, pace, and recent charges. "
        "Without a committee, an organisation-wide summary.",
        ("how is", "how's", "how are", "doing", "summary", "summarize", "summarise", "brief", "briefing",
         "overview", "on track", "on pace", "catch me up", "rundown", "status of"),
        tool_committee_briefing, ("committee", "semester"), specificity=3,
    ),
    Tool(
        "recent_charges",
        "The most recent charges to one committee, newest first.",
        ("recent", "recently", "latest", "last few", "what did", "spent money on", "spend money on",
         "spent on", "charges", "bought"),
        tool_recent_charges, ("committee", "limit", "start", "end", "semester"),
        access="list", needs_committee=True, specificity=3,
    ),
    Tool(
        "largest_transactions",
        "The biggest transactions, optionally above an amount, for one committee, on one account, or in a window.",
        ("largest", "biggest", "over $", "above $", "more than $", "top transaction", "show me all",
         "transactions over", "list all"),
        tool_largest_transactions, ("threshold", "account", "committee", "semester", "start", "end"),
        access="list", specificity=2,
    ),
    Tool(
        "budget_status",
        "Budget against actual for a term: how much is left, and who is over. "
        "With a committee, that committee only.",
        ("budget", "over budget", "allocation", "allocated", "left", "remaining", "how much do we have",
         "utilisation", "utilization"),
        tool_budget_status, ("committee", "semester"), specificity=2,
    ),
    Tool(
        "income_summary",
        "Total income, optionally for a term, a date range, or one account.",
        ("income", "revenue", "brought in", "earned", "received", "came in", "raised"),
        tool_income_summary, ("semester", "start", "end", "account"), specificity=1,
    ),
    Tool(
        "committee_spending",
        "How much one committee, or all committees together, spent in a term or date range.",
        ("spend", "spent", "spending", "expense", "expenses", "cost", "paid out"),
        tool_committee_spending, ("committee", "semester", "start", "end"), specificity=1,
    ),
    Tool(
        "cannot_answer",
        "Use when no other tool can answer from the app's data. Say why.",
        (),
        tool_cannot_answer, ("reason",), access="nav",
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


# --- Running a tool: validation and permissions --------------------------------

def _refuse(text: str, tool: str, args: dict) -> Answer:
    return Answer(text=text, tool=tool, arguments=args, status=REFUSED)


def _clean_arguments(tool: Tool, raw: dict, context: Context) -> tuple[dict, Answer | None]:
    """Keep only the tool's parameters, normalise them, and reject bad values."""
    if (raw or {}).get("committee") and "committee" not in tool.parameters and tool.access != "nav":
        return {}, Answer(
            text=(
                f"I can't break {tool.name.replace('_', ' ')} down by committee, so I can't answer that "
                f"for {raw['committee']}. I can tell you a committee's spending, budget, or recent charges."
            ),
            tool=tool.name, arguments={}, status=UNKNOWN,
        )
    args: dict = {}
    for key in tool.parameters:
        value = (raw or {}).get(key)
        if value is None or value == "" or value == []:
            continue
        args[key] = value

    if "committee" in args:
        canonical = canonical_committee(args["committee"])
        if canonical is None:
            return args, Answer(
                text=(
                    f"I don't know a committee called \"{args['committee']}\". "
                    f"The committees are: {', '.join(COMMITTEE_NAMES)}."
                ),
                tool=tool.name, arguments=args, status=UNKNOWN,
            )
        args["committee"] = canonical

    order = ordered_semesters(context.terms)
    if "semester" in args and args["semester"] not in order:
        return args, Answer(
            text=f"I don't have a term called \"{args['semester']}\". Terms on record: {', '.join(order)}.",
            tool=tool.name, arguments=args, status=UNKNOWN,
        )
    for key in ("start", "end"):
        if key in args:
            parsed = pd.to_datetime(args[key], errors="coerce")
            if pd.isna(parsed):
                return args, Answer(text=f"I couldn't read the date \"{args[key]}\".",
                                    tool=tool.name, arguments=args, status=UNKNOWN)
            args[key] = parsed.date().isoformat()
    if "threshold" in args:
        try:
            args["threshold"] = float(args["threshold"])
        except (TypeError, ValueError):
            args.pop("threshold")
    if "limit" in args:
        try:
            args["limit"] = max(1, min(int(args["limit"]), 50))
        except (TypeError, ValueError):
            args.pop("limit")
    return args, None


def names_other_committee(answer: Answer, scope: Scope) -> bool:
    """
    Last line of defence: does a non-treasurer's answer name a committee other
    than their own? Checks the text and the table's committee columns. (Not
    the bank's free-text details, where a merchant like "CUSTOMINK MERCH"
    would look like the Merch committee.)
    """
    others = [c for c in COMMITTEE_NAMES if c != scope.committee]
    pieces = [answer.text]
    if answer.table is not None and not answer.table.empty:
        for column in ("Committee", "Committee_Name", "Category"):
            if column in answer.table.columns:
                pieces.extend(answer.table[column].astype(str))
    blob = "\n".join(pieces).lower()
    return any(re.search(rf"(?<![a-z]){re.escape(name.lower())}(?![a-z])", blob) for name in others)


def visible_to_asker(context: Context) -> Context:
    """
    The context with every row an officer may not see removed, before any tool runs.

    `run_tool` already refuses a question that names another committee, but a
    tool that sums the whole table -- income, uncategorized, largest
    transactions -- would still add up other committees' money for a VP. Scoping
    the data rather than each tool closes that for every tool at once, including
    ones added later: a tool cannot leak a row it was never given. Rows with no
    committee (uncategorized, most income) are dropped too, because they are not
    provably the VP's. An officer with no committee gets an empty context.

    Members are left as they are: they already see org-wide totals on the
    Dashboard, and `run_tool` keeps committee detail from them.
    """
    scope = context.scope
    if scope.role != "officer":
        return context
    transactions = context.transactions
    budgets = context.budgets
    if scope.committee is None:
        transactions = transactions.iloc[0:0]
        budgets = budgets.iloc[0:0]
    else:
        transactions = transactions[transactions["budget_category"].map(committee_name) == scope.committee]
        if "committeeid" in budgets.columns:
            budgets = budgets[budgets["committeeid"].map(committee_name) == scope.committee]
    return replace(context, transactions=transactions, budgets=budgets, members={})


def run_tool(name: str, raw_args: dict | None, context: Context) -> Answer:
    """
    The only way an answer is produced. Validates arguments, enforces who may
    see what, runs the tool, and checks the result before returning it.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        answer = tool_cannot_answer({}, context)
        answer.tool = "cannot_answer"
        return answer

    args, problem = _clean_arguments(tool, dict(raw_args or {}), context)
    if problem is not None:
        return problem

    scope = context.scope
    asked_for = args.get("committee")

    if scope.role == "officer" and scope.committee is None and tool.access != "nav":
        # `visible_to_asker` gives this officer no rows, so any figure would be a
        # misleading $0.00. Say why there is nothing instead.
        return _refuse(
            "Your account isn't linked to a committee yet, so there are no figures I can show you. "
            "Ask the treasurer to link it.",
            tool.name, args,
        )

    if tool.access == "names" and not scope.sees_every_committee:
        dues_page = pages.by_name("Dues")
        return _refuse(
            "Only the treasurer can see who hasn't paid dues"
            + (f". You can see dues collected on {dues_page.path}." if dues_page.can_open(scope.role) else "."),
            tool.name, args,
        )

    if not scope.sees_every_committee:
        own = scope.committee if scope.role == "officer" else None
        wants_detail = tool.needs_committee or tool.access == "list" or bool(asked_for)
        if wants_detail and own is None:
            return _refuse(
                "Committee figures are only available to that committee's officer or the treasurer. "
                + ("Your account isn't linked to a committee yet; ask the treasurer to link it."
                   if scope.role == "officer" else "Ask your VP or the treasurer."),
                tool.name, args,
            )
        if asked_for and asked_for != own:
            return _refuse(
                f"I can only share figures for your own committee ({own}). "
                f"For {asked_for}, ask its VP or the treasurer.",
                tool.name, args,
            )
        if wants_detail or (tool.name == "committee_briefing" and own):
            args["committee"] = own
    elif tool.needs_committee and not asked_for:
        return Answer(
            text=f"Which committee? The committees are: {', '.join(COMMITTEE_NAMES)}.",
            tool=tool.name, arguments=args, status=UNKNOWN,
        )

    try:
        answer = tool.run(args, visible_to_asker(context))
    except Exception as exc:  # noqa: BLE001 - surfaced, never silently swallowed
        return Answer(text=f"The **{tool.name}** tool failed: {type(exc).__name__}: {exc}",
                      tool=tool.name, arguments=args, status=UNKNOWN)

    answer.tool = tool.name
    answer.arguments = args
    if tool.access != "nav" and not scope.sees_every_committee and names_other_committee(answer, scope):
        return _refuse("I can't share that, because it includes other committees' figures.", tool.name, args)
    return answer


# --- The offline router: keywords pick the tool, patterns pick the arguments ----

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_name) if name}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))
_NAMED_COMMITTEE = re.compile(r"\b([A-Z][\w&]*(?:\s+[A-Z][\w&]*)*)\s+committee\b")
_MY_COMMITTEE = re.compile(r"\b(my|our)\s+(committee|budget|team|spending|charges)\b", re.IGNORECASE)
# Forecasts and opinions: the data records what happened, not what will or should.
_OUT_OF_SCOPE = ("will we", "going to spend", "next year", "predict", "forecast", "should we", "should i")
_NOT_A_NAME = {"the", "our", "my", "which", "what", "each", "every", "this", "that", "a"}


def _find_semesters(question: str, context: Context) -> list[str]:
    lowered = question.lower()
    return [s for s in ordered_semesters(context.terms) if s.lower() in lowered]


def _find_semester(question: str, context: Context) -> str | None:
    named = _find_semesters(question, context)
    if named:
        return named[0]
    # A term-shaped name that is not on record ("Summer 2024") is passed through
    # as asked, so validation answers "I don't have that term" instead of the
    # question quietly widening to all terms.
    unrecorded = re.search(r"\b(fall|spring|summer|winter)\s+(20\d\d)\b", question, re.IGNORECASE)
    if unrecorded:
        return f"{unrecorded.group(1).title()} {unrecorded.group(2)}"
    lowered = question.lower()
    if any(word in lowered for word in ("this semester", "this term", "current", "so far")):
        return current_semester(context)
    if any(word in lowered for word in ("last semester", "last term", "previous semester", "previous term")):
        order = ordered_semesters(context.terms)
        now = current_semester(context)
        if now in order and order.index(now) > 0:
            return order[order.index(now) - 1]
    return None


def _find_dates(question: str, context: Context) -> tuple[str | None, str | None]:
    lowered = question.lower()
    today = context.today()
    first_of_month = today.replace(day=1)
    if "this month" in lowered:
        return first_of_month.date().isoformat(), today.date().isoformat()
    if "last month" in lowered:
        last_day = first_of_month - pd.Timedelta(days=1)
        return last_day.replace(day=1).date().isoformat(), last_day.date().isoformat()
    if any(phrase in lowered for phrase in ("this week", "past week", "last 7 days")):
        return (today - pd.Timedelta(days=6)).date().isoformat(), today.date().isoformat()
    match = re.search(rf"\b(in|during|for|since)\s+({_MONTH_PATTERN})\b", lowered)
    if match:
        month = _MONTHS[match.group(2)]
        year = today.year if month <= today.month else today.year - 1
        start = pd.Timestamp(year=year, month=month, day=1)
        if match.group(1) == "since":
            return start.date().isoformat(), today.date().isoformat()
        end = min(start + pd.offsets.MonthEnd(0), today)
        return start.date().isoformat(), end.date().isoformat()
    return None, None


def _find_committee(question: str) -> str | None:
    lowered = question.lower()
    matches = [c for c in COMMITTEE_NAMES if re.search(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])", lowered)]
    # Longest match wins, so "Professional Development" beats any shorter name.
    return max(matches, key=len) if matches else None


def _find_unknown_committee(question: str) -> str | None:
    """'the Programming committee' -> 'Programming', when that is not a real committee."""
    for match in _NAMED_COMMITTEE.finditer(question):
        words = [w for w in match.group(1).split() if w.lower() not in _NOT_A_NAME]
        candidate = " ".join(words)
        if candidate and canonical_committee(candidate) is None:
            return candidate
    return None


def _find_account(question: str) -> str | None:
    lowered = question.lower()
    if "venmo" in lowered:
        return "Venmo"
    if "wells" in lowered or "checking" in lowered:
        return "Wells Fargo"
    return None


def _find_amount(question: str) -> float | None:
    match = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", question)
    return float(match.group(1).replace(",", "")) if match else None


def select_tool(question: str) -> Tool:
    """
    Score every tool and pick the best, rather than returning on first match.

    This is the structural fix for F10: because all tools are scored, a question
    that mentions a committee *and* asks about spending routes on the spending
    intent instead of being captured by whichever branch happened to be first.
    A question that matches nothing goes to `cannot_answer`, never to a guess.
    """
    lowered = f" {question.lower()} "
    if any(phrase in lowered for phrase in _OUT_OF_SCOPE):
        return TOOLS_BY_NAME["cannot_answer"]
    best, best_score = TOOLS_BY_NAME["cannot_answer"], 0.0
    for tool in TOOLS:
        hits = sum(1 for keyword in tool.keywords if keyword in lowered)
        if not hits:
            continue
        score = hits * 10 + tool.specificity
        if score > best_score:
            best, best_score = tool, score
    return best


def extract_arguments(question: str, tool: Tool, context: Context) -> dict:
    """Pull the tool's arguments out of the question with plain patterns."""
    start, end = _find_dates(question, context)
    committee = _find_committee(question) or _find_unknown_committee(question)
    if committee is None and context.scope.committee and _MY_COMMITTEE.search(question):
        committee = context.scope.committee
    candidates = {
        "committee": committee,
        "semester": None if (start or end) else _find_semester(question, context),
        "semesters": _find_semesters(question, context),
        "start": start,
        "end": end,
        "account": _find_account(question),
        "threshold": _find_amount(question),
        "topic": question,
    }
    wanted = set(tool.parameters) | ({"committee"} if candidates["committee"] else set())
    return {key: candidates[key] for key in wanted if candidates.get(key) not in (None, [], "")}


def answer_question(question: str, context: Context) -> Answer:
    """Answer offline: keyword routing, then `run_tool`."""
    if not question.strip():
        return Answer(text="Ask something about the finances.", status=UNKNOWN)
    tool = select_tool(question)
    return run_tool(tool.name, extract_arguments(question, tool, context), context)


def tool_catalogue() -> pd.DataFrame:
    return pd.DataFrame([{"Tool": tool.name, "What it answers": tool.description} for tool in TOOLS])
