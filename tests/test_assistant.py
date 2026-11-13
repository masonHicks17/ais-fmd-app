"""
The assistant: tools, permissions, wayfinding, the model layer, and the fixed
test set.

Permissions are tested by calling `run_tool` directly with the arguments a
model could be talked into choosing, not only through the keyword router.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from ais_fmd import nav
from ais_fmd.domain import assistant, assistant_pages
from ais_fmd.domain.assistant import Context, Scope, run_tool

ROOT = Path(__file__).resolve().parent.parent

# Committee ids from config/categories.py: 9 Marketing, 6 Operations, 3 Transfers.
MARKETING, OPERATIONS, TRANSFERS = 9, 6, 3


@pytest.fixture
def frames():
    terms = pd.DataFrame(
        [
            {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05", "end_date": "2026-05-01"},
            {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18"},
        ]
    )
    rows = [
        ("2026-02-10", -100.00, MARKETING, "Spring flyers"),
        ("2026-09-03", -250.00, MARKETING, "Fall banner"),
        ("2026-10-02", -40.00, MARKETING, "Stickers"),
        ("2026-10-05", -300.00, OPERATIONS, "Laptop stand"),
        ("2026-10-06", -500.00, TRANSFERS, "Move to savings"),
        ("2026-10-07", 65.00, 1, "Fall dues"),
        ("2026-11-20", -999.00, MARKETING, "Future-dated row"),
    ]
    transactions = pd.DataFrame(
        [
            {"transactionid": i, "transaction_date": d, "amount": a, "budget_category": c,
             "details": t, "purpose": "x", "account": "Wells Fargo"}
            for i, (d, a, c, t) in enumerate(rows, start=1)
        ]
    )
    transactions["budget_category"] = transactions["budget_category"].astype("Int64")
    budgets = pd.DataFrame(
        [
            {"committeebudgetid": 1, "termid": "FA26", "committeeid": MARKETING, "budget_amount": 1000.0},
            {"committeebudgetid": 2, "termid": "FA26", "committeeid": OPERATIONS, "budget_amount": 400.0},
        ]
    )
    return transactions, budgets, terms


def ctx(frames, role="treasurer", committee=None):
    transactions, budgets, terms = frames
    return Context(transactions, budgets, terms, scope=Scope(role, committee), as_of=pd.Timestamp("2026-10-15"))


# --- Tools compute the right numbers --------------------------------------------

def test_spending_for_a_date_range(frames):
    answer = run_tool("committee_spending", {"committee": "Marketing", "start": "2026-10-01", "end": "2026-10-15"},
                      ctx(frames))
    assert "$40.00" in answer.text


def test_spending_for_a_semester_includes_budget_use(frames):
    answer = run_tool("committee_spending", {"committee": "Marketing", "semester": "Fall 2026"}, ctx(frames))
    assert "$1,289.00" in answer.text  # 250 + 40 + 999
    assert "$1,000.00" in answer.text


def test_transfers_are_not_spending(frames):
    answer = run_tool("committee_spending", {"start": "2026-10-01", "end": "2026-10-15"}, ctx(frames))
    assert "$340.00" in answer.text  # 40 + 300; the $500 transfer is excluded


def test_budget_remaining_for_one_committee(frames):
    answer = run_tool("budget_status", {"committee": "Operations"}, ctx(frames))
    assert answer.arguments["semester"] == "Fall 2026"
    assert "$100.00 left" in answer.text


def test_over_budget_is_reported_as_over(frames):
    answer = run_tool("budget_status", {"committee": "Marketing"}, ctx(frames))
    assert "$289.00 over" in answer.text


def test_recent_charges_ignore_future_dated_rows(frames):
    answer = run_tool("recent_charges", {"committee": "Marketing"}, ctx(frames))
    assert "Future-dated" not in answer.table.to_csv()
    assert answer.table.iloc[0]["Details"] == "Stickers"


def test_briefing_reports_pace(frames):
    answer = run_tool("committee_briefing", {"committee": "Operations"}, ctx(frames))
    assert "75% used" in answer.text
    assert "ahead of pace" in answer.text


# --- I don't know -------------------------------------------------------------

def test_unknown_committee_is_not_guessed(frames):
    answer = run_tool("committee_spending", {"committee": "Programming"}, ctx(frames))
    assert answer.status == assistant.UNKNOWN
    assert "$" not in answer.text


def test_unknown_term_is_not_widened_to_all_terms(frames):
    answer = assistant.answer_question("How much did we spend in Summer 2024?", ctx(frames))
    assert answer.status == assistant.UNKNOWN
    assert "$" not in answer.text


def test_a_question_matching_no_tool_gets_i_dont_know(frames):
    answer = assistant.answer_question("What's the weather tomorrow?", ctx(frames))
    assert answer.status == assistant.UNKNOWN
    assert answer.text.startswith("I don't know")


def test_unknown_tool_name_from_a_model_is_not_run(frames):
    answer = run_tool("drop_table_transactions", {}, ctx(frames))
    assert answer.status == assistant.UNKNOWN


def test_committee_passed_to_a_tool_that_cannot_filter_is_not_dropped(frames):
    """Otherwise 'Membership's income' would quietly come back as everyone's income."""
    answer = run_tool("income_summary", {"committee": "Membership"}, ctx(frames))
    assert answer.status == assistant.UNKNOWN
    assert "$" not in answer.text


# --- Who sees what ------------------------------------------------------------

def test_scope_defaults_to_least_access():
    assert Scope().role == "member"
    assert not Scope().sees_every_committee


@pytest.mark.parametrize("tool", ["committee_spending", "budget_status", "committee_briefing",
                                  "recent_charges", "largest_transactions"])
def test_officer_cannot_get_another_committee_whatever_the_model_asks(frames, tool):
    answer = run_tool(tool, {"committee": "Operations"}, ctx(frames, "officer", "Marketing"))
    assert answer.status == assistant.REFUSED
    assert "$" not in answer.text


def test_officer_lists_are_forced_to_their_own_committee(frames):
    answer = run_tool("largest_transactions", {}, ctx(frames, "officer", "Marketing"))
    assert set(answer.table["Committee"]) == {"Marketing"}


def test_officer_gets_org_totals_without_a_breakdown(frames):
    answer = run_tool("budget_status", {}, ctx(frames, "officer", "Marketing"))
    assert answer.status == assistant.ANSWERED
    assert answer.table is None
    assert "Operations" not in answer.text


def test_member_cannot_get_committee_figures(frames):
    answer = run_tool("committee_spending", {"committee": "Marketing"}, ctx(frames, "member"))
    assert answer.status == assistant.REFUSED


def test_officer_without_a_linked_committee_gets_nothing_committee_level(frames):
    answer = run_tool("recent_charges", {"committee": "Marketing"}, ctx(frames, "officer", None))
    assert answer.status == assistant.REFUSED


def test_unpaid_dues_names_are_treasurer_only(frames):
    answer = run_tool("unpaid_dues", {}, ctx(frames, "officer", "Membership"))
    assert answer.status == assistant.REFUSED


def test_leak_check_catches_another_committee_in_a_table():
    leaky = assistant.Answer(text="ok", table=pd.DataFrame({"Committee": ["Marketing", "Operations"]}))
    assert assistant.names_other_committee(leaky, Scope("officer", "Marketing"))


# --- Wayfinding -----------------------------------------------------------------

def _nav_entries() -> dict[str, tuple[str, str]]:
    """{nav label: (view file, role)} from app.py's PAGES literal, plus the VP-only pages."""
    tree = ast.parse((ROOT / "app.py").read_text())
    pages = next(n.value for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PAGES")
    out = {}
    for section in pages.values:
        for entry in section.elts:
            path, title, _icon, role = entry.elts
            out[title.value] = (Path(path.value).name, role.attr.lower())
    for path, title, _icon, role in nav.VP_PAGES:
        out.setdefault(title, (Path(path).name, role.name.lower()))
    return out


def test_the_assistant_knows_exactly_the_pages_a_vp_can_open():
    """`assistant_pages` cannot import `nav` (it imports Streamlit), so check they agree."""
    vp_titles = {title for _path, title, _icon, _role in nav.VP_PAGES}
    assert assistant_pages.VP_PAGE_NAMES == vp_titles
    openable = {page.name for page in assistant_pages.PAGES if page.can_open("officer")}
    assert openable == vp_titles


@pytest.mark.parametrize("tool_name, args", [
    ("committee_spending", {}),
    ("income_summary", {}),
    ("largest_transactions", {}),
    ("dues_summary", {}),
    ("unpaid_dues", {}),
    ("find_page", {"topic": "dashboard"}),
    ("find_page", {"topic": "transactions"}),
])
def test_the_assistant_never_points_a_vp_at_a_page_outside_their_menu(frames, tool_name, args):
    vp_paths = {assistant_pages.by_name(name).path for name in assistant_pages.VP_PAGE_NAMES}
    answer = run_tool(tool_name, args, ctx(frames, "officer", "Marketing"))
    if answer.source is not None:
        assert answer.source in vp_paths, f"{tool_name} sent a VP to {answer.source}"
    for page in assistant_pages.PAGES:
        if page.name not in assistant_pages.VP_PAGE_NAMES and page.name not in ("Assistant",):
            assert f"**{page.path}**" not in answer.text, f"{tool_name} told a VP to open {page.path}"


def _hub_tabs(view_file: str) -> dict[str, str]:
    """{tab label: role} for a hub view, or {} when the view is not a hub."""
    tree = ast.parse((ROOT / "ais_fmd" / "views" / view_file).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "hub":
            return {t.elts[0].value: t.elts[2].attr.lower() for t in node.args[1].elts}
    return {}


def _page_tabs(view_file: str) -> list[str]:
    """The labels of a page's own keyed `st.tabs([...], key=...)`, e.g. Treasury's."""
    tree = ast.parse((ROOT / "ais_fmd" / "views" / view_file).read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "tabs"
            and node.args and isinstance(node.args[0], ast.List)
            and any(keyword.arg == "key" for keyword in node.keywords)
        ):
            return [element.value for element in node.args[0].elts if isinstance(element, ast.Constant)]
    return []


@pytest.mark.parametrize("page", assistant_pages.PAGES, ids=lambda p: p.name)
def test_every_page_the_assistant_names_exists_with_that_role(page):
    nav = _nav_entries()
    assert page.nav in nav, f"{page.nav} is not in app.py; update assistant_pages.py"
    view_file, nav_role = nav[page.nav]
    tabs = _hub_tabs(view_file)
    if tabs:
        label = page.tab or page.nav
        assert label in tabs, f"{label} is not a tab of {page.nav}"
        assert tabs[label] == page.min_role
    elif page.tab and page.tab != page.nav:
        # A tab of a single page (Treasury -> Reconcile): same role as the page.
        assert page.tab in _page_tabs(view_file), f"{page.tab} is not a tab of {page.nav}"
        assert nav_role == page.min_role
    else:
        assert nav_role == page.min_role


def test_find_page_never_sends_someone_to_a_page_they_cannot_open(frames):
    answer = run_tool("find_page", {"topic": "Where do I see unpaid dues?"}, ctx(frames, "member"))
    assert not answer.text.startswith("Go to")
    assert answer.source == "Dues"


# --- The page, headless, as each role ---------------------------------------------

from tests.test_views import TIMEOUT, assert_clean, seeded_db, use_db  # noqa: E402,F401  (fixtures)

ASSISTANT_VIEW = ROOT / "ais_fmd" / "views" / "Assistant.py"


def _ask(role, question: str, committee_id: int | None = None):
    from streamlit.testing.v1 import AppTest

    from ais_fmd import auth

    app = AppTest.from_file(str(ASSISTANT_VIEW), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(
        email="tester@sandbox.local", role=role, committee_id=committee_id
    )
    app.run()
    app.chat_input[0].set_value(question).run()
    return app


def _page_text(app) -> str:
    return "\n".join(m.value for m in app.markdown) + "\n".join(c.value for c in app.caption)


def test_page_answers_an_officer_about_their_own_committee(seeded_db, use_db):
    from ais_fmd import auth

    use_db(seeded_db)
    app = _ask(auth.Role.OFFICER, "How much is left in my budget?", committee_id=MARKETING)
    assert_clean(app, "Assistant as a Marketing officer")
    text = _page_text(app)
    assert "Marketing" in text and "Check it on" in text


def test_page_refuses_an_officer_asking_about_another_committee(seeded_db, use_db):
    from ais_fmd import auth

    use_db(seeded_db)
    app = _ask(auth.Role.OFFICER, "How much did Operations spend this semester?", committee_id=MARKETING)
    assert_clean(app, "Assistant refusing a Marketing officer")
    assert "I can only share figures for your own committee (Marketing)" in _page_text(app)


def test_page_answers_a_member_with_wayfinding(seeded_db, use_db):
    from ais_fmd import auth

    use_db(seeded_db)
    app = _ask(auth.Role.MEMBER, "Where do I see unpaid dues?")
    assert_clean(app, "Assistant wayfinding as a member")
    assert "only the treasurer can open" in _page_text(app)
