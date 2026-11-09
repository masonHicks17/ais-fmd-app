"""
Where things are in the app, for the assistant's `find_page` tool.

The assistant answers "where do I see X?" from this table rather than from a
model's guess, so it can never send someone to a page that does not exist or
that their role cannot open.

`tests/test_assistant.py` reads `app.py` and the hub files and fails if a page
here is renamed, removed, or has its role changed there. When the navigation
changes, update this table in the same pull request.

Roles are plain strings ("member", "officer", "treasurer", "admin") because
`domain/` must not import `auth` (which imports Streamlit).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "president" mirrors auth.Role.PRESIDENT (35): sees everything the treasurer
# sees, between treasurer and admin.
ROLE_RANK = {"member": 10, "officer": 20, "treasurer": 30, "president": 35, "admin": 40}


@dataclass(frozen=True)
class PageInfo:
    name: str           # what the user sees: the tab label, or the nav label
    nav: str            # the navigation entry to click, exactly as in app.py
    tab: str | None     # the switcher tab inside a hub page, if any
    min_role: str       # lowest role that can open it
    shows: str          # one line: what is on the page
    keywords: tuple[str, ...]
    # Opened from the profile menu (top right) rather than the top bar. Was the
    # "More" menu until 2026-11-08.
    under_more: bool = False

    @property
    def path(self) -> str:
        """How to get there, as the user would click it."""
        steps = (["Profile menu (top right)"] if self.under_more else []) + [self.nav]
        if self.tab and self.tab != self.nav:
            steps.append(self.tab)
        return " → ".join(steps)

    def can_open(self, role: str) -> bool:
        if role == "officer":
            # A VP's navigation is `nav.VP_PAGES`, not every page their rank allows:
            # the member-level pages show every committee and are left out of it.
            return self.name in VP_PAGE_NAMES
        return ROLE_RANK.get(role, 0) >= ROLE_RANK[self.min_role]


# The pages in `nav.VP_PAGES`, by the names below. Kept here as names because
# `nav` imports Streamlit and `domain/` must not; a test checks the two agree.
VP_PAGE_NAMES = frozenset({"My Committee", "My Transactions", "My Plan", "Assistant"})


PAGES: tuple[PageInfo, ...] = (
    PageInfo(
        "Home", "Home", None, "member",
        "where the term stands: cash runway, spend pace, dues and what needs attention; "
        "below it, income, spending and budget against actual for any semester, with charts",
        ("home", "overview", "start", "glance", "summary of the semester",
         "dashboard", "chart", "graph", "trend", "budget vs actual", "income vs expenses", "spending by committee"),
    ),
    PageInfo(
        "Dues", "Dues", "Dues", "member",
        "dues collected, how many people paid, and collection against previous terms",
        ("dues", "membership dues", "collected", "collection", "who paid", "paid dues"),
    ),
    PageInfo(
        "Roster", "Dues", "Roster", "treasurer",
        "the membership list matched against dues payments: who has and has not paid",
        ("unpaid", "not paid", "hasn't paid", "haven't paid", "outstanding dues", "roster", "membership list", "owe dues"),
    ),
    PageInfo(
        "Transactions", "Transactions", "Transactions", "member",
        "every transaction, with search and filters",
        ("transaction", "transactions", "purchase", "charge", "search", "find a payment", "receipt", "statement line"),
    ),
    PageInfo(
        "Review Queue", "Transactions", "Review Queue", "treasurer",
        "transactions the categorizer could not resolve, waiting for a person to decide",
        ("review queue", "uncategorized", "uncategorised", "needs review", "categorize", "categorise", "miscategorized"),
    ),
    PageInfo(
        "Upload statement", "Transactions", "Upload statement", "treasurer",
        "import a Venmo or Wells Fargo statement into the ledger",
        ("upload", "upload a bank statement", "upload a statement", "upload statements", "import statement"),
    ),
    PageInfo(
        "Events", "Events", None, "member",
        "the club calendar synced from the event schedule, with attendance and what each event cost; "
        "purchases that may belong to an event",
        ("event", "events", "calendar", "schedule", "gbm", "attendance", "what did the event cost",
         "event cost", "social", "tailgate", "google calendar"),
    ),
    PageInfo(
        "Planner", "Planner", None, "treasurer",
        "a model of next term's budget built from what happened last term",
        ("planner", "plan", "scenario", "next semester budget", "forecast", "what if"),
    ),
    PageInfo(
        "Assistant", "Assistant", "Assistant", "member",
        "this assistant",
        ("assistant", "ask a question", "chat"),
    ),
    PageInfo(
        "Help", "Assistant", "Help", "member",
        "the runbook: how to operate the app",
        ("help", "runbook", "how to use", "instructions", "how do i"),
    ),
    PageInfo(
        "My Committee", "My Committee", None, "officer",
        "your committee's budget, spending, remaining, and reimbursements",
        ("my committee", "my budget", "my spending", "committee page", "reimbursement", "how much do i have left"),
    ),
    PageInfo(
        "My Transactions", "My Transactions", None, "officer",
        "your committee's charges, where you can flag one with \"This isn't ours\"",
        ("my transactions", "my charges", "flag", "isn't ours", "not ours", "wrong charge", "dispute"),
    ),
    PageInfo(
        "My Plan", "My Plan", None, "officer",
        "your committee's plan for this semester and next, and a budget request to the treasurer",
        ("my plan", "plan", "planned", "next semester", "budget request", "request more budget"),
    ),
    PageInfo(
        "Reconciliation", "Reconciliation", None, "member",
        "whether each bank statement balances",
        ("reconcile", "reconciliation", "balance", "statement balance", "bank statement", "opening balance", "closing balance"),
        under_more=True,
    ),
    PageInfo(
        "Alerts & Reports", "Alerts & Data Quality", "Alerts & Reports", "member",
        "what needs attention now, and a printable summary for the board",
        ("alert", "alerts", "report", "reports", "board summary", "printable", "warning", "over budget alert"),
        under_more=True,
    ),
    PageInfo(
        "Data Quality", "Alerts & Data Quality", "Data Quality", "treasurer",
        "everything the integrity checks found, worst first",
        ("data quality", "integrity", "duplicate", "duplicates", "bad data", "errors in the data"),
        under_more=True,
    ),
    PageInfo(
        "Treasury", "Treasury", None, "treasurer",
        "budget allocations, academic terms, and exports",
        ("set budget", "allocate", "allocation", "terms", "semester dates", "dues rates", "export"),
    ),
    PageInfo(
        "Audit Log", "Settings", "Audit Log", "treasurer",
        "who changed what, and when",
        ("audit", "history of changes", "who changed", "change log", "log"),
        under_more=True,
    ),
)


def by_name(name: str) -> PageInfo | None:
    lowered = name.strip().lower()
    return next((page for page in PAGES if page.name.lower() == lowered), None)


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) is not None


def find(topic: str) -> list[PageInfo]:
    """
    Pages whose keywords appear in `topic` as whole words, best match first.

    The longest matching keyword decides, so "uncategorized transactions" lands
    on the Review Queue ("uncategorized") rather than Transactions, and
    "unpaid dues" lands on the Roster rather than matching "paid dues" inside it.
    """
    lowered = topic.lower()
    scored: list[tuple[int, int, PageInfo]] = []
    for position, page in enumerate(PAGES):
        phrases = page.keywords + (page.name.lower(),)
        hits = [len(phrase) for phrase in phrases if _mentions(lowered, phrase)]
        if hits:
            scored.append((max(hits), -position, page))
    scored.sort(reverse=True)
    return [page for _, _, page in scored]
