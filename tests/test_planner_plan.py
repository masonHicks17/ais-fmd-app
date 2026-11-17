"""
The Planner as a term plan (2026-11-17): typed income, sponsors saved per
semester, suggested committee budgets, and the projection fed by them.

The sponsor and budget tables are st.data_editor, which AppTest can't type
into; the storage they call is driven directly, and the page is checked to
read it back.
"""

from __future__ import annotations

import pytest

from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS
from ais_fmd.domain import scenarios

from tests.test_views import VIEWS, assert_clean, run_view, seeded_db, use_db  # noqa: F401

PLANNER = VIEWS / "Planner.py"


# --- Which term is planned ----------------------------------------------------------


@pytest.mark.parametrize(
    "current, following",
    [("Fall 2026", "Spring 2027"), ("Spring 2026", "Fall 2026"), ("Summer 2026", "Fall 2026"),
     ("fall 2026", "Spring 2027"), ("Winter 2026", "Spring 2027"), ("Someday", None), ("", None)],
)
def test_next_semester(current, following):
    assert scenarios.next_semester(current) == following


def test_the_term_after_the_latest_can_be_planned_before_it_exists():
    import pandas as pd

    terms = pd.DataFrame({
        "TermID": ["SP26", "FA26"], "Semester": ["Spring 2026", "Fall 2026"],
        "start_date": ["2026-01-05", "2026-08-20"], "end_date": ["2026-05-01", "2026-12-15"],
    })
    assert scenarios.planning_semesters(terms) == ["Spring 2026", "Fall 2026", "Spring 2027"]


# --- Sponsors ---------------------------------------------------------------------------


def test_sponsor_totals_split_paid_from_pledged_and_skip_blanks():
    totals = scenarios.sponsor_totals([
        {"sponsor": "A", "amount": 500, "status": "Paid"},
        {"sponsor": "B", "amount": "250.50", "status": "Pledged"},
        {"sponsor": "C", "amount": None, "status": "Paid"},
        {"sponsor": "D", "amount": "lots", "status": "Pledged"},
    ])
    assert (totals.paid, totals.pledged, totals.count, totals.total) == (500.0, 250.5, 2, 750.5)


def test_sponsors_are_saved_and_replaced_per_semester(tmp_path):
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "plan.db"))
    first = backend.replace_planned_sponsorships(
        "Spring 2027",
        [
            {"sponsor": "Example Corp", "amount": 1000, "status": "Paid", "paid_on": "2027-01-20",
             "notes": "Check, deposited"},
            {"sponsor": "Sample LLC", "amount": 500, "status": "Pledged"},
        ],
        "treasurer@sandbox.local",
    )
    assert first.ok and first.updated == 2
    backend.replace_planned_sponsorships("Fall 2026", [{"sponsor": "Other", "amount": 50}], "t")

    stored = backend.fetch_planned_sponsorships("Spring 2027")
    assert list(stored["sponsor"]) == ["Example Corp", "Sample LLC"]
    assert [v if isinstance(v, str) else None for v in stored["paid_on"]] == ["2027-01-20", None]
    assert list(stored["status"]) == ["Paid", "Pledged"]

    again = backend.replace_planned_sponsorships(
        "Spring 2027", [{"sponsor": "Sample LLC", "amount": 750, "status": "Paid"}], "t"
    )
    assert again.ok
    assert list(backend.fetch_planned_sponsorships("Spring 2027")["amount"]) == [750.0]
    assert len(backend.fetch_planned_sponsorships("Fall 2026")) == 1, "another semester is untouched"


@pytest.mark.parametrize(
    "row, message",
    [
        ({"sponsor": " ", "amount": 10}, "needs a sponsor name"),
        ({"sponsor": "X", "amount": -5}, "can't be negative"),
        ({"sponsor": "X", "amount": "ten"}, "isn't a number"),
        ({"sponsor": "X", "amount": 5, "status": "Maybe"}, "Pledged or Paid"),
    ],
)
def test_a_bad_sponsor_row_saves_nothing(tmp_path, row, message):
    from ais_fmd.data.sqlite_backend import SqliteBackend

    backend = SqliteBackend(str(tmp_path / "plan.db"))
    backend.replace_planned_sponsorships("Spring 2027", [{"sponsor": "Kept", "amount": 1}], "t")
    result = backend.replace_planned_sponsorships("Spring 2027", [row], "t")
    assert not result.ok and message in result.error
    assert list(backend.fetch_planned_sponsorships("Spring 2027")["sponsor"]) == ["Kept"]


# --- Suggested budgets ----------------------------------------------------------------


def test_suggestions_follow_need_round_down_and_never_exceed_what_is_available():
    suggested = scenarios.suggest_budgets({1: 3000.0, 2: 1000.0, 3: 0.0}, 4010.0)
    assert suggested == {1: 3000.0, 2: 1000.0, 3: 0.0}
    odd = scenarios.suggest_budgets({1: 1.0, 2: 1.0, 3: 1.0}, 1000.0)
    assert set(odd.values()) == {325.0} and sum(odd.values()) <= 1000.0


def test_with_no_history_every_committee_gets_an_equal_share():
    assert scenarios.suggest_budgets({1: 0.0, 2: 0.0}, 500.0) == {1: 250.0, 2: 250.0}


def test_nothing_to_give_suggests_nothing():
    assert scenarios.suggest_budgets({1: 100.0}, -50.0) == {1: 0.0}


def test_need_is_average_spend_or_the_last_budget_when_higher():
    history = {
        1: {"Fall 2025": {"spent": 400.0, "budget": 500.0}, "Spring 2026": {"spent": 200.0, "budget": 900.0}},
        2: {"Fall 2025": {"spent": 800.0, "budget": 0.0}, "Spring 2026": {"spent": 1000.0, "budget": 600.0}},
    }
    assert scenarios.need_weights(history) == {1: 900.0, 2: 900.0}


def test_committee_history_covers_every_budgeted_committee(seeded_db, use_db):
    from ais_fmd.data import repositories as repo
    from ais_fmd.domain.terms import ordered_semesters

    use_db(seeded_db)
    bundle = repo.load_bundle()
    semesters = ordered_semesters(bundle.terms)[-2:]
    history = scenarios.committee_history(bundle.transactions, bundle.budgets, bundle.terms, semesters)
    assert set(history) == set(BUDGETED_COMMITTEE_IDS)
    assert all(set(by_term) == set(semesters) for by_term in history.values())
    assert any(by_term[semesters[-1]]["spent"] > 0 for by_term in history.values())


# --- The projection, fed by typed figures ---------------------------------------------


def test_typed_budgets_income_and_expenses_replace_the_baseline():
    baseline = scenarios.Baseline(
        semester="Fall 2026", income=10_000.0, expenses=8_000.0, net=2_000.0, dues_collected=6_000.0,
        dues_payers=150, dues_rate=40.0, non_dues_income=4_000.0, committee_spend={5: 5_000.0, 7: 2_000.0},
    )
    plan = scenarios.Scenario(
        dues_rate=45.0, expected_members=200, other_income=1_500.0, other_expenses=300.0,
        committee_budgets={5: 4_000.0, 7: 2_500.0, 9: 500.0},
    )
    projection = scenarios.project(baseline, plan)
    assert projection.projected_income == 45.0 * 200 + 1_500.0
    assert projection.committee_projection == {5: 4_000.0, 7: 2_500.0, 9: 500.0}
    assert projection.projected_expenses == 7_000.0 + 300.0


# --- The page ---------------------------------------------------------------------------


def test_the_planner_plans_the_next_term_with_typed_figures(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(PLANNER)
    assert_clean(app, "Planner on first render")
    planning = app.selectbox(key="planner_semester").value
    assert planning == app.selectbox(key="planner_semester").options[-1]
    assert not app.slider, "every figure is typed now"

    # More members than the old 300 cap.
    app.number_input(key="planner_expected_members").set_value(400)
    app.number_input(key="planner_dues_rate").set_value(50.0)
    app = app.run()
    assert_clean(app, "Planner with 400 members")
    dues = next(m for m in app.metric if m.label == "Dues")
    assert dues.value == "$20,000.00"


def test_saved_sponsors_count_toward_the_plan(seeded_db, use_db):
    from ais_fmd.data import repositories as repo

    use_db(seeded_db)
    app = run_view(PLANNER)
    planning = app.selectbox(key="planner_semester").value
    result = repo.replace_planned_sponsorships(
        planning,
        [{"sponsor": "Example Corp", "amount": 1200, "status": "Paid", "paid_on": "2027-01-15"},
         {"sponsor": "Sample LLC", "amount": 300, "status": "Pledged"}],
        "tester@sandbox.local",
    )
    assert result.ok
    app = run_view(PLANNER)
    assert_clean(app, "Planner with saved sponsors")
    assert next(m for m in app.metric if m.label == "Sponsorships").value == "$1,500.00"
    captions = " ".join(c.value for c in app.caption)
    assert "Paid $1,200.00" in captions.replace("\\$", "$") and "2 sponsors" in captions


def test_budgets_default_to_the_suggestions_and_balance(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(PLANNER)
    assert_clean(app, "Planner budgets")
    labels = [m.label for m in app.metric]
    assert "Committee budgets" in labels
    assert "Left over" in labels, "suggestions never exceed what is available"


def test_adding_a_sponsor_saves_it_for_the_planned_term(seeded_db, use_db):
    from datetime import date

    from ais_fmd.data import repositories as repo

    use_db(seeded_db)
    app = run_view(PLANNER)
    planning = app.selectbox(key="planner_semester").value
    app.text_input(key="planner_new_sponsor").input("Example Corp")
    app.number_input(key="planner_new_amount").set_value(750.0)
    app.selectbox(key="planner_new_status").select("Paid")
    app.date_input(key="planner_new_paid_on").set_value(date(2027, 1, 20))
    app.text_input(key="planner_new_notes").input("Check at the career fair")
    next(b for b in app.button if b.label == "Add sponsor").click()
    app = app.run()
    assert_clean(app, "Planner after adding a sponsor")
    stored = repo.load_planned_sponsorships(planning)
    assert list(stored["sponsor"]) == ["Example Corp"]
    assert (stored.iloc[0]["amount"], stored.iloc[0]["status"], stored.iloc[0]["paid_on"]) == (750.0, "Paid", "2027-01-20")


def test_a_sponsor_without_a_name_is_not_added(seeded_db, use_db):
    from ais_fmd.data import repositories as repo

    use_db(seeded_db)
    app = run_view(PLANNER)
    planning = app.selectbox(key="planner_semester").value
    app.number_input(key="planner_new_amount").set_value(100.0)
    next(b for b in app.button if b.label == "Add sponsor").click()
    app = app.run()
    assert repo.load_planned_sponsorships(planning).empty


def test_a_typed_budget_is_kept_when_the_suggestions_move(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(PLANNER)
    planning = app.selectbox(key="planner_semester").value
    key = f"_planner_overrides_{planning}"
    app.session_state[key] = {BUDGETED_COMMITTEE_IDS[0]: 1234.0}
    app = app.run()
    assert_clean(app, "Planner with a typed budget")
    before = next(m for m in app.metric if m.label == "Committee budgets").value

    app.number_input(key="planner_expected_members").set_value(300)
    app = app.run()
    assert app.session_state[key] == {BUDGETED_COMMITTEE_IDS[0]: 1234.0}
    after = next(m for m in app.metric if m.label == "Committee budgets").value
    assert after != before, "the other committees follow the bigger income"

    next(b for b in app.button if b.label == "Use the suggestions for every committee").click()
    app = app.run()
    assert app.session_state[key] == {}
