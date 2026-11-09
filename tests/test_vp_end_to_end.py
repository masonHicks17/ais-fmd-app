"""
The whole VP portal against a richly seeded sandbox.

Built from the base seed plus `scripts/seed_vp_demo.py`, so there is real spread to
test against: every VP committee has spending in all five terms (back to Fall
2024), a mix of on-track / approaching / over, deposits and refunds, fake sign-in
profiles, and a few flags. Then every committee is taken through every term.

What this covers, end to end:
  * sign-in: a stored profile resolves to the right identity, opens exactly the
    right pages, and shows only that committee's rows;
  * the pages, for every committee and every term, including the early ones;
  * the downloads: the printable report for every committee x term, and the CSV;
  * the numbers agree with each other (report, chart, table and comparison);
  * the past-term comparison, with the clock pinned;
  * flags, from a VP flagging a charge to the treasurer seeing it.

All data and all emails are invented.
"""

from __future__ import annotations

import html
import importlib.util
import io
import os
import shutil
from datetime import datetime

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from ais_fmd.domain.details import tidy
from ais_fmd import auth
from ais_fmd.config import vp_committees
from ais_fmd.config.categories import committee_name
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import committee_report, exports, vp_history, vp_metrics
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import date_range_for_semester
from ais_fmd.ui.flags import VERDICT_FIXED

from tests.test_views import ROOT, TIMEOUT, VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_access_chain import ORG_WIDE_PAGES, VP_PAGES, open_app, opens
from tests.test_vp_portal import OFFICER, _committee_rows, run_as


def _load_demo_script():
    spec = importlib.util.spec_from_file_location("seed_vp_demo", ROOT / "scripts" / "seed_vp_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEMO = _load_demo_script()
PROFILES = DEMO.PROFILES
VP_PROFILES = [p for p in PROFILES if p[1] == "officer"]
SEMESTERS = ["Fall 2024", "Spring 2025", "Fall 2025", "Spring 2026", "Fall 2026"]
VP_LINES = [committee.budget_ids[0] for committee in vp_committees.VP_COMMITTEES]  # 10, 7, 9, 5
PINNED_TODAY = pd.Timestamp("2026-10-01")  # about 38% of the way through Fall 2026


@pytest.fixture(scope="session")
def demo_template(tmp_path_factory, seeded_db):
    """The base seed plus the VP demo seed, built once and copied per test."""
    target = tmp_path_factory.mktemp("demo") / "demo.db"
    shutil.copy(seeded_db, target)
    previous = os.environ.get("AIS_FMD_SANDBOX_DB")
    os.environ["AIS_FMD_SANDBOX_DB"] = str(target)
    try:
        assert _load_demo_script().main() == 0
    finally:
        if previous is None:
            os.environ.pop("AIS_FMD_SANDBOX_DB", None)
        else:
            os.environ["AIS_FMD_SANDBOX_DB"] = previous
    return target


@pytest.fixture
def demo_db(demo_template, use_db):
    return use_db(demo_template)


def _frames():
    backend = SqliteBackend()
    return backend.fetch_transactions(), backend.fetch_budgets(), backend.fetch_terms()


def _rollup(transactions, budgets, terms, line, semester) -> dict:
    owned = vp_committees.budget_ids_for(line)
    summary = budget_domain.budget_vs_actual(transactions, budgets, terms, semester)
    return vp_metrics.rollup(summary, [committee_name(cid) for cid in owned])


def _pin_the_clock(monkeypatch) -> None:
    original = vp_history.same_point_in_past_terms

    def pinned(*args, **kwargs):
        kwargs["today"] = PINNED_TODAY
        return original(*args, **kwargs)

    monkeypatch.setattr(vp_history, "same_point_in_past_terms", pinned)


# --- The seed itself -------------------------------------------------------------


def test_every_vp_committee_has_spending_in_every_term_back_to_fall_2024(demo_db):
    transactions, _, terms = _frames()
    for line in VP_LINES:
        owned = vp_committees.budget_ids_for(line)
        mine = transactions[transactions["budget_category"].isin(owned) & (transactions["amount"] < 0)]
        for semester in SEMESTERS:
            start, end = date_range_for_semester(terms, semester)
            dates = pd.to_datetime(mine["transaction_date"])
            in_term = mine[(dates >= start) & (dates <= end)]
            assert len(in_term) >= 2, f"{vp_committees.title_for(line)} has almost nothing in {semester}"


def test_the_seed_added_the_fake_profiles_and_sample_flags(demo_db):
    backend = SqliteBackend()
    assert {p[0] for p in PROFILES} == set(backend.fetch_profiles()["email"])
    flags = backend.fetch_flags()
    assert sorted(flags["status"]) == ["dismissed", "open", "open"]


# --- Sign-in: profile -> identity -> pages -> rows ---------------------------------


@pytest.mark.parametrize(("email", "role", "line", "name"), VP_PROFILES, ids=[p[0] for p in VP_PROFILES])
def test_signing_in_as_each_vp_opens_the_right_pages_and_only_their_rows(email, role, line, name, demo_db):
    backend = SqliteBackend()
    # Google capitalises however the account was first typed.
    identity = auth._identity_for_google_user(
        {"email": email.upper(), "email_verified": True}, fetch_profile=backend.fetch_profile
    )
    assert identity is not None, f"{email} could not sign in"
    assert (identity.role, identity.committee_id) == (auth.Role.OFFICER, line)

    app = open_app(identity)
    assert_clean(app, f"app.py as {email}")
    for page in VP_PAGES:
        assert opens(app, page), f"{page} did not open for {email}"
    for page in ORG_WIDE_PAGES:
        assert not opens(app, page), f"{page} opened for {email} but shows every committee"

    assert opens(app, "Officer")
    assert any(vp_committees.title_for(line) in block.value for block in app.markdown)

    assert opens(app, "MyTransactions")
    app.session_state["mytxn_semester"] = "All"
    app.run()
    assert_clean(app, f"My Transactions as {email}")
    shown = app.dataframe[0].value
    got = sorted(
        (str(pd.Timestamp(d).date()), round(float(a), 2), str(t))
        for d, a, t in zip(shown["Date"], shown["Amount"], shown["Details"])
    )
    assert got == _committee_rows(line)


def test_the_treasurer_profile_signs_in_to_the_full_app_and_a_stranger_is_refused(demo_db):
    backend = SqliteBackend()
    treasurer = auth._identity_for_google_user(
        {"email": "Treasurer@Sandbox.Local", "email_verified": True}, fetch_profile=backend.fetch_profile
    )
    assert treasurer.role == auth.Role.TREASURER and not treasurer.committee_scoped
    app = open_app(treasurer)
    for page in ("Home", "TransactionsHub", "Treasury", "Officer"):
        assert opens(app, page), f"{page} should open for the treasurer"
    assert auth._identity_for_google_user(
        {"email": "stranger@sandbox.local", "email_verified": True}, fetch_profile=backend.fetch_profile
    ) is None


# --- Every committee, every term -----------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
@pytest.mark.parametrize("semester", SEMESTERS)
def test_my_committee_renders_for_every_committee_in_every_term(line, semester, demo_db):
    app = run_as(VIEWS / "Officer.py", OFFICER, line, officer_semester=semester)
    assert_clean(app, f"My Committee, {vp_committees.title_for(line)}, {semester}")
    assert len(app.get("download_button")) == 1
    assert "Spending pace" in [s.value for s in app.subheader]


@pytest.mark.parametrize("line", [2, 4])  # Treasury, President: no VP roll-up
def test_committees_without_a_vp_roll_up_still_render_cleanly(line, demo_db):
    for view in ("Officer.py", "MyTransactions.py"):
        assert_clean(run_as(VIEWS / view, OFFICER, line), f"{view} for line {line}")


# --- Downloads: the printable report ---------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
@pytest.mark.parametrize("semester", SEMESTERS)
def test_the_report_for_every_committee_and_term_is_scoped_and_reconciles(line, semester, demo_db):
    transactions, budgets, terms = _frames()
    owned = vp_committees.budget_ids_for(line)
    report = committee_report.build_for_committee(
        transactions, budgets, terms, line, semester, generated=datetime(2026, 10, 1, 9, 30), sandbox=True
    )

    # Well-formed, self-contained, and says what it is.
    assert report.startswith("<!doctype html>") and report.rstrip().endswith("</html>")
    assert html.escape(vp_committees.title_for(line)) in report and semester in report
    assert "SANDBOX DATA" in report
    for forbidden in ("<script", "<link", "src=", "http://", "https://"):
        assert forbidden not in report

    # Nothing from any other committee. The report truncates descriptions to 90
    # characters and escapes them, so compare exactly that form. (A shorter prefix
    # is not enough: two committees can buy from the same vendor on the same day,
    # and differ only in the reference digits at the end.)
    own = set(transactions[transactions["budget_category"].isin(owned)]["details"].dropna())
    foreign = set(transactions[~transactions["budget_category"].isin(owned).fillna(False)]["details"].dropna()) - own
    leaked = [d for d in foreign if html.escape(d[:90]) in report]
    assert not leaked, f"another committee's charge is in the report: {leaked[:2]}"

    # The headline figures agree with the page's own arithmetic.
    position = _rollup(transactions, budgets, terms, line, semester)
    assert format_currency(position["budget"]) in report
    assert format_currency(position["spent"]) in report


def test_the_report_download_is_served_by_the_page_for_a_vp(demo_db):
    app = run_as(VIEWS / "Officer.py", OFFICER, 5, officer_semester="Fall 2025")
    assert_clean(app, "My Committee as the Membership VP")
    [button] = app.get("download_button")
    assert "Download committee report" in str(button.proto)


# --- Downloads: the CSV ------------------------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
def test_the_csv_holds_exactly_the_committees_rows_and_nothing_a_spreadsheet_would_run(line, demo_db):
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, line, mytxn_semester="All")
    assert_clean(app, "My Transactions")
    csv = exports.transactions_csv(app.dataframe[0].value).decode("utf-8-sig")
    parsed = pd.read_csv(io.StringIO(csv))

    expected = _committee_rows(line)
    assert len(parsed) == len(expected)
    assert round(float(parsed["Amount"].sum()), 2) == round(sum(row[1] for row in expected), 2)
    assert not parsed["Details"].astype(str).str.startswith(("=", "+", "-", "@")).any()


def test_a_hostile_bank_description_is_defused_in_the_csv_but_not_altered_on_screen(demo_db):
    hostile = '=HYPERLINK("http://evil.example","click")'
    backend = SqliteBackend()
    from ais_fmd.domain.dedupe import assign_natural_keys

    records = assign_natural_keys([
        {"transaction_date": "2026-09-15", "amount": -42.0, "details": hostile,
         "budget_category": 7, "purpose": "Misc.", "account": "Wells Fargo"},
    ])
    assert backend.insert_transactions(records, "hostile.csv", "tests").ok

    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 7, mytxn_semester="All")
    table = app.dataframe[0].value
    assert tidy(hostile) in set(table["Details"])  # the page shows the readable form, unescaped
    csv = exports.transactions_csv(table).decode("utf-8-sig")
    assert "'=HYPERLINK" in csv and ",=HYPERLINK" not in csv


# --- The numbers agree with each other -----------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
@pytest.mark.parametrize("semester", ["Fall 2024", "Spring 2025", "Fall 2025", "Spring 2026"])
def test_the_pace_chart_ends_where_the_budget_table_says_the_term_ended(line, semester, demo_db):
    transactions, budgets, terms = _frames()
    owned = vp_committees.budget_ids_for(line)
    mine = transactions[transactions["budget_category"].isin(owned)]
    start, end = date_range_for_semester(terms, semester)

    series = vp_metrics.cumulative_spend(mine, start, end, through=end)
    expected = _rollup(transactions, budgets, terms, line, semester)["spent"]
    assert float(series["spent"].iloc[-1]) == pytest.approx(expected, abs=0.01)


# --- Data going back in the past ------------------------------------------------------------


@pytest.mark.parametrize("line", VP_LINES)
def test_the_past_term_comparison_works_for_every_committee_and_agrees_with_the_history(line, demo_db):
    transactions, budgets, terms = _frames()
    owned = vp_committees.budget_ids_for(line)
    through = pd.to_datetime(transactions["transaction_date"]).max()

    result = vp_history.same_point_in_past_terms(
        transactions, budgets, terms, owned, "Fall 2026", through=through, today=PINNED_TODAY
    )
    assert result is not None, f"no comparison for {vp_committees.title_for(line)}"
    assert result.table["Semester"].tolist() == ["Spring 2026", "Fall 2025", "Spring 2025", "Fall 2024"]
    assert vp_history.comparison_sentence(result)

    # "Ended the term at" must be that term's final figure, as the budget table sees it.
    for _, row in result.table.iterrows():
        position = _rollup(transactions, budgets, terms, line, row["Semester"])
        assert row["Ended the term at"] == pytest.approx(position["percent"], abs=0.01)


@pytest.mark.parametrize("line", VP_LINES)
def test_my_committee_shows_the_past_term_comparison_for_every_committee(line, demo_db, monkeypatch):
    _pin_the_clock(monkeypatch)
    app = run_as(VIEWS / "Officer.py", OFFICER, line)
    assert_clean(app, f"My Committee, {vp_committees.title_for(line)}")
    assert any("This term vs. past terms" in block.value for block in app.markdown)
    table = next((block.value for block in app.markdown if "ais-compare" in block.value), None)
    assert table is not None and table.count("<tr") >= 5  # header, this term, three earlier ones


# --- Flags, from a VP to the treasurer ---------------------------------------------------------


@pytest.mark.parametrize(("email", "role", "line", "name"), VP_PROFILES, ids=[p[0] for p in VP_PROFILES])
def test_each_vp_can_flag_a_charge_and_only_sees_their_own_flags(email, role, line, name, demo_db):
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, line, mytxn_semester="All")
    app.selectbox(key="flag_charge_0").select_index(0)
    note = f"End-to-end check for {name}: this was not ours."
    app.text_area(key="flag_note_0").input(note)
    app.button(key="flag_submit_0").click()
    app.run()
    assert_clean(app, f"flagging a charge as {email}")

    backend = SqliteBackend()
    owned = vp_committees.budget_ids_for(line)
    mine = backend.fetch_flags(owned)
    assert note in set(mine["note"])
    assert set(mine["booked_to_committee_id"]) <= set(owned)
    everyone = backend.fetch_flags()
    assert len(everyone) > len(mine)  # other committees' flags exist, and are not in `mine`


def test_the_treasurer_sees_every_open_flag_and_can_close_one(demo_db):
    backend = SqliteBackend()
    before = backend.fetch_flags()
    open_before = before[before["status"] == "open"]
    assert len(open_before) == 2

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "Review Queue with sample flags")
    text = " ".join(block.value for block in app.markdown)
    for note in open_before["note"]:
        assert note in text, f"the treasurer cannot see: {note}"

    target = int(open_before.iloc[0]["flag_id"])
    app.radio(key=f"flag_verdict_{target}").set_value(VERDICT_FIXED).run()
    app.button(key=f"flag_apply_{target}").click().run()
    assert_clean(app, "closing a flag")
    assert backend.fetch_flags().set_index("flag_id").loc[target, "status"] == "resolved"


# --- The sandbox's demo sign-in picker ----------------------------------------------------------


@pytest.mark.parametrize(("email", "role", "line", "name"), VP_PROFILES, ids=[p[0] for p in VP_PROFILES])
def test_the_demo_sign_in_picker_signs_in_as_a_profile_through_the_real_lookup(email, role, line, name, demo_db):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT).run()
    assert_clean(app, "app.py as the default sandbox user")
    assert app.session_state[auth.SESSION_KEY].role == auth.Role.TREASURER

    app.selectbox(key="ais_demo_profile").select(email).run()
    assert_clean(app, f"after signing in as {email}")
    identity = app.session_state[auth.SESSION_KEY]
    assert (identity.email, identity.role, identity.committee_id) == (email, auth.Role.OFFICER, line)

    # And that identity really is the VP's: three pages, nothing org-wide.
    for page in VP_PAGES:
        assert opens(app, page), f"{page} did not open after signing in as {email}"
    assert not opens(app, "Home")
    assert app.session_state[auth.SESSION_KEY].committee_id == line  # the pickers did not undo it


def test_the_demo_sign_in_picker_can_switch_between_profiles(demo_db):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT).run()
    app.selectbox(key="ais_demo_profile").select("vp.consulting@sandbox.local").run()
    assert app.session_state[auth.SESSION_KEY].committee_id == 7
    app.selectbox(key="ais_demo_profile").select("vp.membership@sandbox.local").run()
    assert app.session_state[auth.SESSION_KEY].committee_id == 5
    app.selectbox(key="ais_demo_profile").select("treasurer@sandbox.local").run()
    assert app.session_state[auth.SESSION_KEY].role == auth.Role.TREASURER
