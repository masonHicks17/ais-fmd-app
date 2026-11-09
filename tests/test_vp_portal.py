"""
VP portal: navigation, and proof that a VP only ever sees their own committee.

The navigation tests are pure. The isolation tests render the real pages
headlessly as an Officer for every budgeted committee and compare what is on
screen against the database, row for row -- an exact match, so a leak of even
one other-committee row fails.
"""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from ais_fmd import auth, nav
from ais_fmd.config import vp_committees
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import vp_metrics
from ais_fmd.domain.details import tidy

# Fixtures and helpers shared with the other headless view tests.
from tests.test_views import ROOT, TIMEOUT, VIEWS, assert_clean, seeded_db, use_db  # noqa: F401

OFFICER = auth.Role.OFFICER

# A line no VP committee owns: President (4). It plays the part Passport (16)
# used to -- the line next door that a VP roll-up must never pick up.
OUTSIDE = 4


def run_as(path, role, committee_id=None, **session) -> AppTest:
    app = AppTest.from_file(str(path), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(
        email="vp@sandbox.local", role=role, committee_id=committee_id
    )
    for key, value in session.items():
        app.session_state[key] = value
    return app.run()


@pytest.fixture
def two_line_committee(monkeypatch):
    """
    A committee that owns two budget lines, for tests of the roll-up machinery.

    No real committee owns more than one line right now, but the roll-up is kept
    for when one does, so it has to be tested. For the length of one test this
    makes Membership own Membership and the President's line.
    """
    patched = tuple(
        vp_committees.VpCommittee("Membership", (5, OUTSIDE)) if c.title == "Membership" else c
        for c in vp_committees.VP_COMMITTEES
    )
    monkeypatch.setattr(vp_committees, "VP_COMMITTEES", patched)


# --- Navigation (pure) ---------------------------------------------------------

FULL_PAGES = [
    ("ais_fmd/views/Dashboard.py", "Dashboard", "i", auth.Role.MEMBER),
    ("ais_fmd/views/Transactions.py", "Transactions", "i", auth.Role.MEMBER),
    ("ais_fmd/views/Treasury.py", "Treasury", "i", auth.Role.TREASURER),
]


def test_a_vp_gets_exactly_these_pages_with_the_assistant_last():
    vp = auth.Identity(email="vp@x.edu", role=OFFICER, committee_id=5)
    titles = [page[1] for page in nav.visible_pages(vp, FULL_PAGES)]
    assert titles == ["My Committee", "My Transactions", "My Plan", "Events", "Assistant"]


def test_a_vp_never_gets_a_page_that_shows_every_committee():
    vp = auth.Identity(email="vp@x.edu", role=OFFICER, committee_id=5)
    paths = {page[0] for page in nav.visible_pages(vp, FULL_PAGES)}
    assert not paths & {page[0] for page in FULL_PAGES}


def test_a_treasurer_keeps_the_full_navigation():
    treasurer = auth.Identity(email="t@x.edu", role=auth.Role.TREASURER)
    assert nav.visible_pages(treasurer, FULL_PAGES) == FULL_PAGES


def test_a_member_is_not_given_the_vp_pages():
    member = auth.Identity(email="m@x.edu", role=auth.Role.MEMBER)
    titles = [page[1] for page in nav.visible_pages(member, FULL_PAGES)]
    assert titles == ["Dashboard", "Transactions"]


@pytest.mark.parametrize("path", [page[0] for page in nav.VP_PAGES])
def test_every_vp_page_exists_and_is_gated(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "auth.require" in source or path.endswith("Home.py")


# --- Isolation (headless renders) ---------------------------------------------


def _committee_rows(committee_id: int) -> list[tuple]:
    """What the database holds for everything a VP with this profile owns."""
    from ais_fmd.data.sqlite_backend import SqliteBackend

    frame = SqliteBackend().fetch_transactions()
    owned = vp_committees.budget_ids_for(committee_id)
    rows = frame[frame["budget_category"].isin(owned)]
    return sorted(
        (
            str(pd.Timestamp(date).date()),
            round(float(amount), 2),
            tidy(details),  # what the page shows (domain/details.py), not the bank's text
        )
        for date, amount, details in zip(rows["transaction_date"], rows["amount"], rows["details"])
    )


def test_the_seed_gives_the_isolation_tests_something_to_prove(seeded_db, use_db):
    use_db(seeded_db)
    populated = [cid for cid in BUDGETED_COMMITTEE_IDS if _committee_rows(cid)]
    assert len(populated) >= 3, "too few committees have rows for these tests to mean anything"


@pytest.mark.parametrize("committee_id", BUDGETED_COMMITTEE_IDS)
def test_a_vp_sees_exactly_their_committees_transactions(committee_id, seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, committee_id, mytxn_semester="All")
    assert_clean(app, f"My Transactions as the {committee_name(committee_id)} VP")

    expected = _committee_rows(committee_id)
    if not expected:
        assert not app.dataframe
        return

    shown = app.dataframe[0].value
    got = sorted(
        (str(pd.Timestamp(date).date()), round(float(amount), 2), str(details))
        for date, amount, details in zip(shown["Date"], shown["Amount"], shown["Details"])
    )
    assert got == expected


def test_an_officer_with_no_committee_sees_nothing(seeded_db, use_db):
    use_db(seeded_db)
    for view in ("MyTransactions.py", "Officer.py"):
        app = run_as(VIEWS / view, OFFICER, None)
        assert_clean(app, f"{view} as an officer with no committee")
        assert not app.dataframe, f"{view} showed data to an officer with no committee"
        assert not app.selectbox, f"{view} offered a picker to an officer with no committee"


def test_a_vp_cannot_pick_another_committee_on_my_committee(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, 5)
    assert_clean(app, "My Committee as the Membership VP")
    assert "Committee" not in [box.label for box in app.selectbox]
    assert committee_name(5) in " ".join(block.value for block in app.markdown)


def test_a_treasurer_can_still_pick_any_committee(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", auth.Role.TREASURER)
    assert_clean(app, "My Committee as the treasurer")
    assert "Committee" in [box.label for box in app.selectbox]


def test_home_points_a_vp_only_at_pages_they_have(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Home.py", OFFICER, 5)
    assert_clean(app, "Home as a VP")
    # Home offers jump buttons to the org-wide pages; a VP's navigation does not
    # have those pages, so none of them may be drawn.
    labels = [button.label for button in app.button]
    assert "Open Dues" not in labels
    assert not any(label.startswith("All ") and label.endswith(" committees") for label in labels)


# --- Which budget lines each committee owns ------------------------------------


def test_no_budget_line_belongs_to_two_committees():
    """A line in two committees would be counted twice in a VP's totals."""
    seen: dict[int, str] = {}
    for committee in vp_committees.VP_COMMITTEES:
        for cid in committee.budget_ids:
            assert cid not in seen, f"line {cid} is in both {seen[cid]} and {committee.title}"
            seen[cid] = committee.title


def test_every_owned_line_is_a_real_budgeted_line():
    for committee in vp_committees.VP_COMMITTEES:
        for cid in committee.budget_ids:
            assert cid in BUDGETED_COMMITTEE_IDS, f"{committee.title}: {cid} has no budget"


def test_finance_is_left_alone():
    """Treasury (line 2) is the treasurer's own full view, not a VP roll-up."""
    assert vp_committees.for_committee_id(2) is None
    assert vp_committees.budget_ids_for(2) == (2,)


def test_membership_owns_only_its_own_line_and_the_presidents_belongs_to_no_vp():
    assert vp_committees.budget_ids_for(5) == (5,)
    assert vp_committees.for_committee_id(OUTSIDE) is None  # President: no VP roll-up
    assert vp_committees.budget_ids_for(OUTSIDE) == (OUTSIDE,)


def test_a_line_resolves_to_the_committee_that_owns_it(two_line_committee):
    assert vp_committees.for_committee_id(OUTSIDE).title == "Membership"
    assert vp_committees.budget_ids_for(OUTSIDE) == vp_committees.budget_ids_for(5)


def test_an_unmapped_committee_falls_back_to_just_itself():
    assert vp_committees.budget_ids_for(4) == (4,)  # President
    assert vp_committees.title_for(4) == committee_name(4)
    assert vp_committees.budget_ids_for(None) == ()


# --- The maths behind the page -------------------------------------------------


def _summary(rows):
    return pd.DataFrame(rows, columns=["Committee_Name", "Budget", "Spent"])


def test_rollup_adds_the_named_lines_and_ignores_the_rest():
    summary = _summary(
        [("Membership", 1000.0, 400.0), ("President", 500.0, 350.0), ("Consulting", 900.0, 900.0)]
    )
    total = vp_metrics.rollup(summary, ["Membership", "President"])
    assert total["budget"] == 1500.0
    assert total["spent"] == 750.0
    assert total["remaining"] == 750.0
    assert total["percent"] == pytest.approx(50.0)


def test_rollup_with_no_budget_has_no_percent_rather_than_infinity():
    total = vp_metrics.rollup(_summary([("President", 0.0, 25.0)]), ["President"])
    assert total["percent"] is None


def test_status_matches_the_org_wide_dashboard_at_every_boundary():
    """One committee must never read 'on track' here and 'approaching' there."""
    for percent in [0, 50, 84.9, 85, 99.9, 100, 100.1, 250]:
        row = pd.Series({"% Spent": percent, "Spent": 10.0})
        assert vp_metrics.status_for(percent, 10.0) == budget_domain._status_for_row(row)
    for spent in (0.0, 10.0):
        row = pd.Series({"% Spent": None, "Spent": spent})
        assert vp_metrics.status_for(None, spent) == budget_domain._status_for_row(row)


def test_term_elapsed_percent_runs_from_zero_to_a_hundred():
    terms = pd.DataFrame(
        {"TermID": ["T1"], "Semester": ["Fall 2026"], "start_date": ["2026-08-01"], "end_date": ["2026-11-30"]}
    )
    at = lambda day: vp_metrics.term_elapsed_percent(terms, "Fall 2026", as_of=pd.Timestamp(day))
    assert at("2026-07-01") == 0.0  # before the term starts
    assert at("2026-09-30") == pytest.approx(60 / 121 * 100)  # Aug 1 -> Sep 30 is 60 of 121 days
    assert at("2027-01-01") == 100.0  # after it ends
    assert vp_metrics.term_elapsed_percent(terms, "Spring 1999") is None


def test_projection_is_a_straight_line_and_declines_to_guess_early():
    assert vp_metrics.projected_spend(300.0, 50.0) == pytest.approx(600.0)
    assert vp_metrics.projected_spend(300.0, 100.0) == pytest.approx(300.0)
    assert vp_metrics.projected_spend(300.0, 5.0) is None  # too early to say
    assert vp_metrics.projected_spend(300.0, None) is None


# --- The roll-up on the page ---------------------------------------------------


@pytest.fixture
def membership_db(seeded_db, use_db):
    """
    The seed books plenty to Membership (5) but nothing to the President's line
    (4), so a leak of the latter would go unnoticed. Re-book three of
    Membership's Meeting Food rows onto it, on this test's private copy, so there
    is something to leak and something to roll up.
    """
    import sqlite3

    path = use_db(seeded_db)
    with sqlite3.connect(path) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT transactionid FROM transactions WHERE budget_category = 5 "
                "AND purpose = 'Meeting Food' ORDER BY transaction_date DESC, transactionid LIMIT 3"
            )
        ]
        connection.executemany(
            "UPDATE transactions SET budget_category = ? WHERE transactionid = ?",
            [(OUTSIDE, transaction_id) for transaction_id in ids],
        )
    return path


def test_the_fixture_really_put_three_rows_on_the_second_line(membership_db):
    membership, president = _committee_rows(5), _committee_rows(OUTSIDE)
    assert len(membership) >= 3 and len(president) == 3
    assert not set(membership) & set(president)  # whole rows: names alone repeat (two Chipotle runs)


# --- The President's line is outside the VP committees ----------------------------


def _raw_details(committee_id) -> list[str]:
    from ais_fmd.data.sqlite_backend import SqliteBackend

    frame = SqliteBackend().fetch_transactions()
    return [str(d) for d in frame.loc[frame["budget_category"] == committee_id, "details"].dropna()]


def _shown_rows(frame):
    return sorted(
        (str(pd.Timestamp(date).date()), round(float(amount), 2), str(details))
        for date, amount, details in zip(frame["Date"], frame["Amount"], frame["Details"])
    )


def test_the_membership_vp_sees_only_membership_rows_and_never_a_presidents_charge(membership_db):
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 5, mytxn_semester="All")
    assert_clean(app, "My Transactions as the Membership VP")
    shown = app.dataframe[0].value
    assert "Line" not in shown.columns  # one line, so no per-line column
    assert _shown_rows(shown) == _committee_rows(5)
    president_rows = set(_committee_rows(OUTSIDE))
    assert president_rows and not president_rows & set(_shown_rows(shown))


def test_the_membership_page_never_lists_a_presidents_charge(membership_db):
    app = run_as(VIEWS / "Officer.py", OFFICER, 5)
    assert_clean(app, "My Committee as the Membership VP")
    president_charges = {tidy(d) for d in _raw_details(OUTSIDE)} | set(_raw_details(OUTSIDE))
    for element in app.dataframe:
        assert not president_charges & set(element.value.get("Details", [])), "a President's charge was listed"
        assert "Budget line" not in element.value.columns  # a single line has no per-line table


def test_a_profile_on_the_presidents_line_sees_only_its_rows(membership_db):
    """The President's line belongs to no VP committee, so it is just itself -- and never Membership."""
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, OUTSIDE, mytxn_semester="All")
    assert_clean(app, "My Transactions on the President's line")
    shown = app.dataframe[0].value
    assert _shown_rows(shown) == _committee_rows(OUTSIDE)
    assert not set(_committee_rows(5)) & set(_shown_rows(shown))


# --- The roll-up machinery, for a committee that owns two lines ------------------
#
# No real committee owns two lines right now, but the feature stays, so it is
# exercised with a test-only committee (the `two_line_committee` fixture).


def test_a_two_line_committee_sees_every_line_it_owns_and_nothing_else(membership_db, two_line_committee):
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 5, mytxn_semester="All")
    assert_clean(app, "My Transactions for a two-line committee")
    shown = app.dataframe[0].value
    assert set(shown["Line"]) == {"Membership", "President"}
    assert _shown_rows(shown) == _committee_rows(5)


def test_the_budget_line_filter_narrows_to_one_line(membership_db, two_line_committee):
    app = run_as(
        VIEWS / "MyTransactions.py", OFFICER, 5, mytxn_semester="All", mytxn_line="President"
    )
    assert_clean(app, "My Transactions filtered to President")
    assert set(app.dataframe[0].value["Line"]) == {"President"}


def test_my_committee_shows_a_by_line_table_for_a_two_line_committee(membership_db, two_line_committee):
    app = run_as(VIEWS / "Officer.py", OFFICER, 5)
    assert_clean(app, "My Committee for a two-line committee")
    by_line = next(
        (element.value for element in app.dataframe if "Budget line" in element.value.columns),
        None,
    )
    assert by_line is not None, "no per-line table for a committee that owns two lines"
    assert set(by_line["Budget line"]) == {"Membership", "President"}


def test_a_single_line_committee_gets_no_by_line_table(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, 7)  # Consulting
    assert_clean(app, "My Committee as the Consulting VP")
    assert not any("Budget line" in element.value.columns for element in app.dataframe)


def test_either_line_of_a_two_line_committee_lands_on_the_same_page(membership_db, two_line_committee):
    """A profile may carry either line; both must land on the combined view."""
    as_first = run_as(VIEWS / "MyTransactions.py", OFFICER, 5, mytxn_semester="All")
    as_second = run_as(VIEWS / "MyTransactions.py", OFFICER, OUTSIDE, mytxn_semester="All")
    assert as_first.dataframe[0].value.equals(as_second.dataframe[0].value)


# --- The dashboard maths -------------------------------------------------------


def test_budget_flow_steps_add_up_to_the_remaining_balance():
    split = pd.DataFrame(
        {"Category": list("ABCDEFG"), "Amount": [70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]}
    )
    labels, values, measures = vp_metrics.budget_flow(split, 1000.0)
    assert labels == ["Budget", "A", "B", "C", "D", "E", "Other", "Remaining"]
    assert values[labels.index("Other")] == pytest.approx(-30.0)  # F + G
    assert sum(values[:-1]) == pytest.approx(1000.0 - 280.0)  # what really remains
    assert measures[-1] == "total"
    assert set(measures[:-1]) == {"relative"}


def test_budget_flow_has_no_other_step_when_everything_is_itemised():
    split = pd.DataFrame({"Category": ["A", "B"], "Amount": [30.0, 20.0]})
    labels, values, _ = vp_metrics.budget_flow(split, 100.0)
    assert labels == ["Budget", "A", "B", "Remaining"]
    assert sum(values[:-1]) == pytest.approx(50.0)


def test_budget_flow_ends_below_zero_when_over_budget():
    split = pd.DataFrame({"Category": ["A"], "Amount": [150.0]})
    _, values, _ = vp_metrics.budget_flow(split, 100.0)
    assert sum(values[:-1]) == pytest.approx(-50.0)


def test_historical_adds_the_lines_per_semester_and_ignores_other_committees():
    terms = pd.DataFrame(
        {
            "TermID": ["T1", "T2"],
            "Semester": ["Fall 2025", "Spring 2026"],
            "start_date": ["2025-08-01", "2026-01-05"],
            "end_date": ["2025-12-15", "2026-05-01"],
        }
    )
    budgets = pd.DataFrame(
        {
            "termid": ["T1", "T1", "T2", "T2"],
            "committeeid": [5, OUTSIDE, 5, OUTSIDE],
            "budget_amount": [100.0, 50.0, 200.0, 25.0],
        }
    )
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(
                ["2025-09-01", "2025-10-01", "2026-02-01", "2026-02-02"]
            ),
            "amount": [-30.0, -20.0, -75.0, -999.0],
            "budget_category": pd.array([5, OUTSIDE, 5, 9], dtype="Int64"),  # 9 = Marketing
        }
    )
    out = vp_metrics.historical(transactions, budgets, terms, ["Membership", "President"])
    assert out["Semester"].tolist() == ["Fall 2025", "Spring 2026"]
    assert out["Budget"].tolist() == [150.0, 225.0]
    assert out["Spent"].tolist() == [50.0, 75.0]  # Marketing's 999 is not counted
    assert out["% Spent"].tolist() == pytest.approx([50 / 150 * 100, 75 / 225 * 100])


def test_historical_with_nothing_to_show_is_an_empty_frame_with_the_right_columns():
    out = vp_metrics.historical(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), ["President"])
    assert list(out.columns) == ["Semester", "Budget", "Spent", "% Spent"]
    assert out.empty


# --- The dashboard page --------------------------------------------------------


@pytest.mark.parametrize("committee_id", BUDGETED_COMMITTEE_IDS)
def test_my_committee_renders_for_every_committee(committee_id, seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, committee_id)
    assert_clean(app, f"My Committee as the {committee_name(committee_id)} VP")


def test_payee_label_reduces_a_bank_line_to_who_was_paid():
    assert vp_metrics.payee_label(
        "PURCHASE AUTHORIZED ON 09/03 PANDA EXPRESS 2201 GAINESVILLE FL S969730"
    ) == "Panda Express"
    assert vp_metrics.payee_label(
        "ZELLE TO  JANE DOE ON 08/21 REF # WFCT22KG9MGT GBM #1 MEETING FOOD"
    ) == "Zelle to Jane Doe"
    assert vp_metrics.payee_label("") == "Other payees"


def test_payee_split_counts_only_spending_largest_first():
    df = pd.DataFrame(
        {
            "details": [
                "PURCHASE AUTHORIZED ON 09/03 CHIPOTLE 1462 GAINESVILLE FL S1",
                "PURCHASE AUTHORIZED ON 09/10 CHIPOTLE 1463 GAINESVILLE FL S2",
                "PURCHASE AUTHORIZED ON 09/17 PUBLIX SUPER MAR GAINESVILLE FL S3",
                "DEPOSIT FROM SPONSOR",
            ],
            "amount": [-30.0, -20.0, -40.0, 500.0],
        }
    )
    split = vp_metrics.payee_split(df)
    assert split["Category"].tolist() == ["Chipotle", "Publix Super Mar"]
    assert split["Amount"].tolist() == [50.0, 40.0]


def test_pareto_rows_run_to_the_whole_of_spending():
    split = pd.DataFrame(
        {"Category": list("ABCDEFG"), "Amount": [70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 10.0]}
    )
    rows = vp_metrics.pareto_rows(split, 1000.0)
    assert [row["label"] for row in rows] == ["A", "B", "C", "D", "E", "Other"]
    assert rows[-1]["amount"] == pytest.approx(30.0)  # F + G
    assert rows[-1]["cumulative"] == pytest.approx(100.0)
    assert rows[0]["share"] == pytest.approx(25.0)  # 70 of 280
    assert rows[0]["of_budget"] == pytest.approx(7.0)


def test_my_committee_draws_the_spend_bar_and_the_kinds_of_spending(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", OFFICER, 5)
    assert_clean(app, "My Committee as the Membership VP")
    html = " ".join(block.value for block in app.markdown)
    assert "ais-vbar" in html and "of the budget spent" in html
    assert "Where the money went" in [str(h.value) for h in app.subheader]
    assert any(chart.proto.spec and "Food & catering" in chart.proto.spec for chart in app.get("plotly_chart"))
    labels = [m.label for m in app.metric] if app.metric else []
    assert "Used" not in labels and "Spent" not in labels, "the bar card already says these"


def test_the_bank_text_toggle_shows_exactly_the_stored_details(membership_db):
    """The readable names are display only; the toggle shows what the bank wrote, row for row."""
    app = run_as(VIEWS / "MyTransactions.py", OFFICER, 5, mytxn_semester="All", mytxn_raw_details=True)
    assert_clean(app, "My Transactions with bank text")
    shown = app.dataframe[0].value
    assert sorted(shown["Details"]) == sorted(_raw_details(5))
    assert any(text.startswith("PURCHASE AUTHORIZED ON") for text in shown["Details"])
