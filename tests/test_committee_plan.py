"""
My Plan: a VP's basic budget planner -- one line per category, Planned against
Spent -- and a budget request for next semester.

Three layers, as with flags:
  * the rules and arithmetic (`domain/committee_plan.py`), pure;
  * the sandbox backend, including that it refuses another committee's plan;
  * the page, rendered as every VP, reading only that VP's own plan.

Everything here is invented sandbox data.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd import auth, nav
from ais_fmd.config import vp_committees
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import committee_plan as plan

# Fixtures and helpers shared with the other VP tests.
from tests.test_flags import OFFICER, VP  # noqa: F401
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as  # noqa: F401

CONSULTING = 7
MARKETING = 9
VP_COMMITTEE_IDS = [vp.budget_ids[0] for vp in vp_committees.VP_COMMITTEES]


def lines(*rows) -> pd.DataFrame:
    """Rows as (category, amount[, note])."""
    width = len(plan.LINE_FIELDS)
    return pd.DataFrame([list(row) + [None] * (width - len(row)) for row in rows], columns=list(plan.LINE_FIELDS))


# --- Semesters --------------------------------------------------------------------


@pytest.mark.parametrize("now, after", [
    ("Fall 2026", "Spring 2027"),
    ("Spring 2027", "Fall 2027"),
    ("Summer 2026", "Fall 2026"),
    ("fall 2026", "Spring 2027"),
])
def test_the_next_semester_is_worked_out_from_the_name(now, after):
    assert plan.next_semester_name(now) == after


def test_a_name_that_is_not_season_and_year_has_no_next_semester():
    assert plan.next_semester_name("Autumn term") is None
    assert plan.next_semester_name(None) is None


# --- Cleaning what a VP typed -------------------------------------------------------


def test_blank_rows_are_dropped_and_good_rows_are_kept():
    cleaned, problems = plan.clean_lines(lines(
        ["Food & Drink", 400, "weekly meetings"],
        [None, None, None],
        ["Social Events", 200.004],
    ))
    assert problems == []
    assert cleaned == [
        {"purpose": "Food & Drink", "amount": 400.0, "note": "weekly meetings"},
        {"purpose": "Social Events", "amount": 200.0, "note": None},
    ]


@pytest.mark.parametrize("row, says", [
    ([None, 20], "pick a category"),
    (["x" * 41, 20], "under 40"),
    ([["Food & Drink", "Merch"], 20], "one category per line"),
    (["Food & Drink", 0], "above $0"),
    (["Food & Drink", None, "just a note"], "above $0"),
    (["Food & Drink", 1_000_000], "too large"),
])
def test_a_bad_row_is_named_and_nothing_is_saved_for_it(row, says):
    cleaned, problems = plan.clean_lines(lines(row))
    assert cleaned == []
    assert len(problems) == 1 and says in problems[0]


def test_a_typed_category_of_your_own_is_kept():
    cleaned, problems = plan.clean_lines(lines(["  Office   supplies ", 150, "pens and tape"]))
    assert problems == []
    assert cleaned == [{"purpose": "Office supplies", "amount": 150.0, "note": "pens and tape"}]


def test_the_pick_or_type_cell_gives_a_list_of_one_which_is_unwrapped():
    cleaned, problems = plan.clean_lines(lines([["Office supplies"], 150], [["Food & Drink"], 40]))
    assert problems == []
    assert [line["purpose"] for line in cleaned] == ["Office supplies", "Food & Drink"]


def test_an_empty_pick_with_an_amount_asks_for_a_category():
    cleaned, problems = plan.clean_lines(lines([[], 20]))
    assert cleaned == [] and "pick a category" in problems[0]


def test_a_name_saved_earlier_comes_back_as_a_choice():
    saved = pd.DataFrame({"purpose": ["Office supplies", "Food & Drink"], "amount": [150, 400]})
    assert plan.custom_categories(saved) == ["Office supplies"]


def test_a_typed_name_that_is_really_a_standard_one_becomes_the_standard_one():
    cleaned, problems = plan.clean_lines(lines(["food & drink", 90]))
    assert problems == [] and cleaned[0]["purpose"] == "Food & Drink"


def test_own_names_cant_repeat_either_whatever_the_capitals():
    cleaned, problems = plan.clean_lines(lines(["Office supplies", 50], ["office SUPPLIES", 20]))
    assert len(cleaned) == 1 and "listed twice" in problems[0]


def test_a_category_can_only_be_on_one_line():
    cleaned, problems = plan.clean_lines(lines(["Food & Drink", 100], ["Food & Drink", 50]))
    assert len(cleaned) == 1 and "listed twice" in problems[0]


def test_ticked_rows_are_removed_and_counted():
    edited = lines(["Food & Drink", 100], ["Misc.", 20], [None, None])
    edited["remove"] = [False, True, True]  # the blank ticked row isn't a real removal
    kept, removed = plan.drop_removed(edited)
    assert removed == 1
    assert list(kept["purpose"]) == ["Food & Drink"]
    assert "remove" not in kept.columns


def test_filling_in_from_history_updates_amounts_and_keeps_the_rest():
    existing = [
        {"purpose": "Food & Drink", "amount": 100.0, "note": "keep me"},
        {"purpose": "Social Events", "amount": 200.0, "note": None},
    ]
    incoming = [{"purpose": "Food & Drink", "amount": 325.0, "note": None},
                {"purpose": "Misc.", "amount": 60.0, "note": None}]
    merged = {line["purpose"]: line for line in plan.merge_lines(existing, incoming)}
    assert merged["Food & Drink"] == {"purpose": "Food & Drink", "amount": 325.0, "note": "keep me"}
    assert merged["Social Events"]["amount"] == 200.0
    assert merged["Misc."]["amount"] == 60.0


# --- The budget sheet ---------------------------------------------------------------


CHARGES = pd.DataFrame({
    "transaction_date": ["2026-09-03", "2026-09-10", "2026-09-28", "2026-12-30", "2026-09-15"],
    "amount": [-42.0, -18.5, -60.0, -99.0, 25.0],
    "purpose": ["Food & Drink", "Food & Drink", None, "Food & Drink", "Food & Drink"],
})
FALL = (pd.Timestamp("2026-08-15"), pd.Timestamp("2026-12-18"))


def test_spending_by_category_counts_money_out_in_the_term_only():
    spent = plan.spending_by_purpose(CHARGES, *FALL)
    # The charge after the term and the deposit are left out; no purpose -> Misc.
    assert spent.to_dict("records") == [
        {"Purpose": "Food & Drink", "Amount": 60.5},
        {"Purpose": "Misc.", "Amount": 60.0},
    ]


def test_the_sheet_shows_planned_spent_and_left_including_unplanned_spending():
    sheet = plan.budget_sheet(
        lines(["Food & Drink", 400], ["Social Events", 200]),
        plan.spending_by_purpose(CHARGES, *FALL),
    )
    assert sheet.to_dict("records") == [
        {"Category": "Food & Drink", "Planned": 400.0, "Spent": 60.5, "Left": 339.5},
        {"Category": "Social Events", "Planned": 200.0, "Spent": 0.0, "Left": 200.0},
        # Misc. has spending but no plan: it still shows, so money can't hide off the plan.
        {"Category": "Misc.", "Planned": 0.0, "Spent": 60.0, "Left": -60.0},
    ]


@pytest.mark.parametrize("planned, status", [(500, "fits"), (950, "tight"), (1100, "over")])
def test_the_headline_says_whether_the_plan_fits_the_budget(planned, status):
    sheet = plan.budget_sheet(lines(["Food & Drink", planned]), None)
    totals = plan.sheet_totals(sheet, 1000)
    assert totals["status"] == status
    assert totals["unplanned"] == 1000 - planned


def test_no_budget_is_its_own_status():
    assert plan.sheet_totals(plan.budget_sheet(None, None), 0)["status"] == "no budget"


def test_the_donut_folds_small_categories_into_other():
    sheet = plan.budget_sheet(lines(*[[name, 100 - i] for i, name in enumerate([
        "Food & Drink", "Social Events", "Misc.", "Merch", "Technology", "Marketing", "Road Trip",
    ])]), None)
    slices = plan.donut_slices(sheet, max_slices=6)
    assert list(slices["Category"]) == ["Food & Drink", "Social Events", "Misc.", "Merch", "Technology", "Other"]
    assert slices["Planned"].sum() == sheet["Planned"].sum()  # nothing lost


def test_history_becomes_one_line_per_category_rounded_up_to_5():
    assert plan.lines_from_history(plan.spending_by_purpose(CHARGES, *FALL)) == [
        {"purpose": "Food & Drink", "amount": 65.0, "note": None},
        {"purpose": "Misc.", "amount": 60.0, "note": None},
    ]


# --- Next semester ------------------------------------------------------------------


def test_next_spring_is_suggested_from_earlier_springs_only():
    semesters = ["Fall 2024", "Spring 2025", "Fall 2025", "Spring 2026", "Fall 2026"]
    spent = {"Fall 2024": 500, "Spring 2025": 1000, "Fall 2025": 700, "Spring 2026": 900, "Fall 2026": 600}
    budget = {name: 1200 for name in semesters}
    history = plan.season_history(semesters, spent, budget, "Spring 2027")
    assert list(history["Term"]) == ["Spring 2025", "Spring 2026"]
    assert plan.suggestion(history) == {"low": 900.0, "typical": 950.0, "high": 1000.0, "terms": 2}


def test_no_earlier_term_of_the_season_means_no_suggestion():
    history = plan.season_history(["Fall 2026"], {"Fall 2026": 600}, {}, "Spring 2027")
    assert history.empty and plan.suggestion(history) is None


def test_the_slider_scales_the_typical_term_and_the_request_rounds_up():
    assert plan.scaled_estimate(950, 120) == 1140.0
    assert plan.default_request(planned=1003, estimate=900) == 1010.0
    assert plan.default_request(0, 0) == 0.0


@pytest.mark.parametrize("amount, reason, problem", [
    (0, "Two socials and a speaker", "above $0"),
    (500, "more", "in a sentence"),
    (500, "x" * 501, "under 500"),
])
def test_a_request_needs_an_amount_and_a_reason(amount, reason, problem):
    value, text, why = plan.clean_request(amount, reason)
    assert value is None and problem in why


def test_a_good_request_is_cleaned():
    assert plan.clean_request("850", "  Two socials and a speaker  ") == (850.0, "Two socials and a speaker", None)


# --- The sandbox backend ------------------------------------------------------------


@pytest.fixture
def backend(seeded_db, use_db):  # noqa: F811
    use_db(seeded_db)
    return SqliteBackend()


def _clean(*rows):
    cleaned, problems = plan.clean_lines(lines(*rows))
    assert not problems, problems
    return cleaned


def test_a_plan_saves_and_replaces_only_that_committee_and_semester(backend):
    assert backend.replace_plan_lines(CONSULTING, "Fall 2026", _clean(["Food & Drink", 250], ["Misc.", 40]), (CONSULTING,), VP).ok
    assert backend.replace_plan_lines(MARKETING, "Fall 2026", _clean(["Marketing", 75]), (MARKETING,), VP).ok
    assert backend.replace_plan_lines(CONSULTING, "Spring 2027", _clean(["Social Events", 300]), (CONSULTING,), VP).ok

    assert backend.replace_plan_lines(CONSULTING, "Fall 2026", _clean(["Food & Drink", 300]), (CONSULTING,), VP).ok

    now = backend.fetch_plan_lines((CONSULTING,), "Fall 2026")
    assert list(now["purpose"]) == ["Food & Drink"] and list(now["amount"]) == [300.0]
    assert len(backend.fetch_plan_lines((CONSULTING,), "Spring 2027")) == 1  # untouched
    assert list(backend.fetch_plan_lines((MARKETING,))["purpose"]) == ["Marketing"]  # untouched


def test_removing_everything_leaves_an_empty_plan(backend):
    assert backend.replace_plan_lines(CONSULTING, "Fall 2026", _clean(["Food & Drink", 250]), (CONSULTING,), VP).ok
    assert backend.replace_plan_lines(CONSULTING, "Fall 2026", [], (CONSULTING,), VP).ok
    assert backend.fetch_plan_lines((CONSULTING,), "Fall 2026").empty


def test_the_backend_refuses_a_plan_for_another_committee(backend):
    result = backend.replace_plan_lines(MARKETING, "Fall 2026", _clean(["Misc.", 10]), (CONSULTING,), VP)
    assert result.error and "own committee" in result.error
    assert backend.fetch_plan_lines((MARKETING,)).empty


def test_one_request_per_committee_and_semester_and_sending_again_replaces_it(backend):
    assert backend.save_budget_request(CONSULTING, "Spring 2027", 800, "Two socials", (CONSULTING,), VP).ok
    assert backend.save_budget_request(CONSULTING, "Spring 2027", 950, "Two socials and a speaker", (CONSULTING,), VP).ok
    saved = backend.fetch_budget_requests((CONSULTING,), "Spring 2027")
    assert len(saved) == 1 and float(saved.iloc[0]["amount"]) == 950.0


def test_the_backend_refuses_a_request_for_another_committee(backend):
    result = backend.save_budget_request(MARKETING, "Spring 2027", 800, "Not mine to ask", (CONSULTING,), VP)
    assert result.error
    assert backend.fetch_budget_requests((MARKETING,)).empty


def test_saving_a_plan_and_a_request_is_in_the_audit_log(backend):
    backend.replace_plan_lines(CONSULTING, "Fall 2026", _clean(["Food & Drink", 250]), (CONSULTING,), VP)
    backend.save_budget_request(CONSULTING, "Spring 2027", 800, "Two socials", (CONSULTING,), VP)
    assert {"plan", "budget request"} <= set(backend.fetch_audit()["action"])


# --- Who gets the page --------------------------------------------------------------


def test_my_plan_is_in_a_vps_menu():
    vp = auth.Identity(email="vp@x.edu", role=OFFICER, committee_id=CONSULTING)
    assert "My Plan" in [page[1] for page in nav.visible_pages(vp, [])]


def test_my_plan_is_not_in_the_treasurers_menu():
    """For the committees only: the treasurer's full navigation does not list it."""
    from tests.test_views import ROOT

    assert "MyPlan.py" not in (ROOT / "app.py").read_text(encoding="utf-8")


# --- The page -----------------------------------------------------------------------


@pytest.mark.parametrize("committee_id", VP_COMMITTEE_IDS)
def test_every_vp_page_renders_and_only_ever_asks_for_its_own_plan(committee_id, backend, monkeypatch):
    """
    Every read the page makes is scoped to the signed-in VP's committee. Checked at
    the data boundary, because the editors' contents aren't visible to AppTest.
    """
    from ais_fmd.data import repositories as repo

    for cid in VP_COMMITTEE_IDS:
        assert backend.replace_plan_lines(cid, "Spring 2027", _clean(["Misc.", 25]), (cid,), VP).ok
        assert backend.save_budget_request(cid, "Spring 2027", 100 + cid, f"Request for {cid}", (cid,), VP).ok

    asked: list[tuple] = []
    for name in ("load_plan_lines", "load_budget_requests"):
        real = getattr(repo, name)

        def spy(committee_ids=None, semester=None, _real=real):
            asked.append(committee_ids)
            return _real(committee_ids, semester)

        monkeypatch.setattr(repo, name, spy)

    shown = ""
    for view in ("this", "next"):  # both halves of the page
        app = run_as(VIEWS / "MyPlan.py", OFFICER, committee_id, plan_view=view)
        assert_clean(app, f"My Plan ({view}) as {vp_committees.title_for(committee_id)}")
        shown += "\n".join(str(block.value) for block in [*app.markdown, *app.caption])
    assert asked, "the page never read a plan"
    assert all(scope == (committee_id,) for scope in asked), asked

    others = [cid for cid in VP_COMMITTEE_IDS if cid != committee_id]
    assert not [cid for cid in others if f"Request for {cid}" in shown]
    assert f"Request for {committee_id}" in shown  # their own request is shown back to them


def test_a_vp_with_no_committee_sees_nothing(backend):
    app = run_as(VIEWS / "MyPlan.py", OFFICER, None)
    assert_clean(app, "My Plan for an officer with no committee")
    assert not app.dataframe and not app.number_input


def test_the_request_form_sends_a_request(backend):
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view="next")
    app.number_input(key="plan_request_amount").set_value(850.0)
    app.text_area(key="plan_request_reason").set_value("Two socials and a guest speaker")
    app.button(key="plan_request_send").click().run()
    assert_clean(app, "sending a budget request")
    saved = SqliteBackend().fetch_budget_requests((CONSULTING,))
    assert len(saved) == 1 and float(saved.iloc[0]["amount"]) == 850.0


def test_a_short_reason_is_refused_on_the_page(backend):
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view="next")
    app.text_area(key="plan_request_reason").set_value("more")
    app.button(key="plan_request_send").click().run()
    assert any("in a sentence" in str(w.value) for w in app.warning)
    assert SqliteBackend().fetch_budget_requests((CONSULTING,)).empty


def test_the_page_stays_on_the_semester_you_were_on_after_saving(backend):
    """Tabs jumped back to "This semester" after every save; the switch doesn't."""
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view="next")
    app.number_input(key="plan_request_amount").set_value(500.0)
    app.text_area(key="plan_request_reason").set_value("Kickoff food and a case competition")
    app.button(key="plan_request_send").click().run()
    assert app.radio(key="plan_view").value == "next"
    assert any("sent to the treasurer" in str(s.value) for s in app.success)


@pytest.mark.parametrize("view, key, semester", [
    ("this", "fill_now", "Fall 2026"),
    ("next", "fill_next", "Spring 2027"),
])
def test_fill_in_from_past_spending_sets_amounts_and_keeps_the_rest(view, key, semester, backend):
    """The base seed's Consulting spending is in Spring 2026, which both views can copy."""
    assert backend.replace_plan_lines(CONSULTING, semester, _clean(["Road Trip", 10]), (CONSULTING,), VP).ok

    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view=view)
    assert_clean(app, f"My Plan ({view})")
    basis = app.radio(key=f"{key}_basis")
    basis.set_value(next(option for option in basis.options if "Spring 2026" in option)).run()
    app.button(key=f"{key}_use").click().run()
    assert_clean(app, "filling in from history")

    saved = SqliteBackend().fetch_plan_lines((CONSULTING,), semester)
    assert "Road Trip" in set(saved["purpose"])  # the existing line is kept
    assert len(saved) > 1  # and the copied categories were added
    assert saved["purpose"].is_unique


def test_fill_in_can_do_just_one_category(backend):
    """Pick one category and only that one is filled in; the plan's other lines stay."""
    assert backend.replace_plan_lines(CONSULTING, "Spring 2027", _clean(["Road Trip", 10]), (CONSULTING,), VP).ok
    # Two more Consulting charges in Spring 2026, so there is more than one category to choose from.
    receipt = backend.insert_transactions([
        {"transaction_date": "2026-03-10", "amount": -42.0, "details": "TEST PIZZA", "budget_category": CONSULTING,
         "purpose": "Food & Drink", "account": "Checking"},
        {"transaction_date": "2026-03-12", "amount": -18.0, "details": "TEST TAPE", "budget_category": CONSULTING,
         "purpose": "Misc.", "account": "Checking"},
    ], "test-fill-one-category.csv", VP)
    assert receipt.ok, receipt.error
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view="next")
    basis = app.radio(key="fill_next_basis")
    basis.set_value(next(option for option in basis.options if "Spring 2026" in option)).run()

    pick = app.multiselect(key="fill_next_pick")
    assert len(pick.options) > 1, "the seed should give more than one category to choose from"
    one = pick.options[0]
    pick.set_value([one]).run()
    assert app.button(key="fill_next_use").label.startswith(f"Use for {one} (")
    app.button(key="fill_next_use").click().run()
    assert_clean(app, "filling in one category")

    saved = SqliteBackend().fetch_plan_lines((CONSULTING,), "Spring 2027")
    assert sorted(saved["purpose"]) == sorted(["Road Trip", one])


def test_a_plan_with_a_category_of_your_own_renders_and_shows_zero_spent(backend):
    own_line = [{"purpose": "Office supplies", "amount": 150.0, "note": None}]
    assert backend.replace_plan_lines(CONSULTING, "Fall 2026", own_line, (CONSULTING,), VP).ok
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING, plan_view="this")
    assert_clean(app, "My Plan with a category of the VP's own")
    # The reason sits in the Spent column's "?" (2026-11-13), in the editor's column config.
    assert any("your own ones show $0 spent" in table.proto.columns for table in app.dataframe)


def test_the_page_says_so_when_plans_cannot_be_saved_yet(backend, monkeypatch):
    """Before migration 014 runs on live, the sheet is replaced by a notice."""
    from ais_fmd.data import repositories as repo

    def missing(*_args, **_kwargs):
        raise RuntimeError("relation committee_plan_lines does not exist")

    monkeypatch.setattr(repo, "load_plan_lines", missing)
    monkeypatch.setattr(repo, "load_budget_requests", missing)
    app = run_as(VIEWS / "MyPlan.py", OFFICER, CONSULTING)
    assert_clean(app, "My Plan without the tables")
    assert any("can't be saved" in str(i.value) for i in app.info)


def test_last_seasons_spending_is_never_marked_over_next_terms_plan():
    from ais_fmd.ui import plan_charts

    sheet = pd.DataFrame({"Category": ["Misc."], "Planned": [0.0], "Last Spring": [165.0]})
    figure = plan_charts.plan_vs_actual(sheet, actual="Last Spring", actual_label="Last Spring", flag_over=False)
    assert [trace.name for trace in figure.data] == ["Planned", "Last Spring"]  # no "Over plan" trace
    this_term = sheet.rename(columns={"Last Spring": "Spent"})
    assert "Over plan" in [trace.name for trace in plan_charts.plan_vs_actual(this_term).data]
