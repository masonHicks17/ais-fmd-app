"""
"Same point in past terms": where a committee stood at this moment in earlier terms.

Built from hand-made terms and ledgers where every expected number can be worked
out on paper. All terms are exactly 120 days long, so "50% through" lands on a
whole day, and another committee's huge charges sit alongside to prove they are
ignored.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ais_fmd.domain import vp_history

TERMS = pd.DataFrame(
    {
        "TermID": ["T0", "T1", "T2", "T3"],
        "Semester": ["Fall 2024", "Fall 2025", "Spring 2026", "Fall 2026"],
        "start_date": ["2024-08-01", "2025-08-01", "2026-01-01", "2026-08-01"],
        "end_date": ["2024-11-29", "2025-11-29", "2026-05-01", "2026-11-29"],
    }
)

# Line 7 is the committee under test; line 9 is "somebody else" and must never count.
BUDGETS = pd.DataFrame(
    {
        # Fall 2024 (T0) has a budget but not a single charge: no spending to compare.
        "termid": ["T0", "T1", "T2", "T3", "T1", "T2", "T3"],
        "committeeid": [7, 7, 7, 7, 9, 9, 9],
        "budget_amount": [1000.0, 1000.0, 500.0, 800.0, 5000.0, 5000.0, 5000.0],
    }
)


def _transactions() -> pd.DataFrame:
    rows = [
        # Fall 2025, line 7: by day 60 (Sep 30) = 100 + 50; the Oct 1 and Nov 20 charges come later.
        ("2025-08-10", -100.0, 7), ("2025-09-30", -50.0, 7),
        ("2025-10-01", -200.0, 7), ("2025-11-20", -300.0, 7),
        # Spring 2026, line 7: day 60 is Mar 2, so the Mar 3 charge is after the point.
        ("2026-02-01", -400.0, 7), ("2026-03-03", -100.0, 7),
        # Fall 2026 (now), line 7: 300 + 100 by Sep 30; the Oct 15 charge is in the future.
        ("2026-08-20", -300.0, 7), ("2026-09-30", -100.0, 7), ("2026-10-15", -700.0, 7),
        # Income on the committee's own line never counts as spending.
        ("2026-09-01", 1000.0, 7),
        # Another committee, enormous, in every term.
        ("2025-09-01", -9999.0, 9), ("2026-02-01", -9999.0, 9), ("2026-09-01", -9999.0, 9),
    ]
    return pd.DataFrame(
        {
            "transaction_date": pd.to_datetime([r[0] for r in rows]),
            "amount": [r[1] for r in rows],
            "budget_category": pd.array([r[2] for r in rows], dtype="Int64"),
        }
    )


TODAY = pd.Timestamp("2026-09-30")  # exactly 50% of the way through Fall 2026
THROUGH = pd.Timestamp("2026-10-20")


def compare(**overrides):
    args = dict(
        df_transactions=_transactions(),
        df_budgets=BUDGETS,
        df_terms=TERMS,
        line_ids=(7,),
        semester="Fall 2026",
        through=THROUGH,
        today=TODAY,
    )
    args.update(overrides)
    return vp_history.same_point_in_past_terms(**args)


# --- The comparison ------------------------------------------------------------


def test_each_past_term_is_measured_at_the_same_fraction_of_that_term():
    result = compare()
    assert result.elapsed_percent == pytest.approx(50.0)
    table = result.table
    assert table["Semester"].tolist() == ["Spring 2026", "Fall 2025"]  # most recent first
    assert table["Spent by this point"].tolist() == [400.0, 150.0]
    assert table["Of its budget"].tolist() == pytest.approx([80.0, 15.0])
    assert table["Ended the term at"].tolist() == pytest.approx([100.0, 65.0])


def test_this_terms_own_figure_is_measured_the_same_way():
    result = compare()
    assert result.current_spent == 400.0  # the Oct 15 charge is after the point
    assert result.current_percent == pytest.approx(50.0)


def test_another_committees_charges_never_enter_the_numbers():
    table = compare().table
    assert table["Spent by this point"].max() < 1000


def test_a_term_with_a_budget_but_no_spending_is_left_out():
    """It cannot tell you about pace, and would drag 'what is usual' toward zero."""
    assert "Fall 2024" not in compare().table["Semester"].tolist()


def test_the_same_season_is_marked():
    table = compare().table.set_index("Semester")
    assert bool(table.loc["Fall 2025", "Same season"]) is True
    assert bool(table.loc["Spring 2026", "Same season"]) is False


def test_only_the_most_recent_terms_are_kept():
    assert compare(max_terms=1).table["Semester"].tolist() == ["Spring 2026"]


def test_the_data_not_the_calendar_sets_how_far_through_the_term_it_is():
    """Statements last uploaded Sep 30; today is a month later. 'Now' is Sep 30."""
    result = compare(through=pd.Timestamp("2026-09-30"), today=pd.Timestamp("2026-11-01"))
    assert result.as_of == pd.Timestamp("2026-09-30")
    assert result.elapsed_percent == pytest.approx(50.0)
    assert result.current_spent == 400.0


def test_nothing_is_offered_when_there_is_nothing_meaningful_to_say():
    assert compare(through=None) is None  # no statements at all
    assert compare(through=pd.Timestamp("2026-07-01")) is None  # data has not reached the term
    assert compare(today=pd.Timestamp("2026-08-05")) is None  # too early in the term
    # Fall 2025's only earlier term (Fall 2024) is empty, so there is nothing to compare with.
    assert compare(semester="Fall 2025", today=pd.Timestamp("2025-09-30"),
                   through=pd.Timestamp("2025-10-30")) is None
    assert compare(line_ids=()) is None


def test_a_committee_that_owns_two_lines_is_added_together():
    budgets = pd.DataFrame(
        {
            "termid": ["T1", "T1", "T3", "T3"],
            "committeeid": [5, 4, 5, 4],
            "budget_amount": [600.0, 400.0, 600.0, 400.0],
        }
    )
    transactions = pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(["2025-09-01", "2025-09-02", "2026-09-01"]),
            "amount": [-60.0, -40.0, -250.0],
            "budget_category": pd.array([5, 4, 4], dtype="Int64"),
        }
    )
    result = compare(df_transactions=transactions, df_budgets=budgets, line_ids=(5, 4))
    assert result.table["Spent by this point"].tolist() == [100.0]
    assert result.table["Of its budget"].tolist() == pytest.approx([10.0])
    assert result.current_percent == pytest.approx(25.0)


def test_an_empty_ledger_does_not_crash():
    assert compare(df_transactions=pd.DataFrame(), df_budgets=pd.DataFrame()) is None


# --- The sentence --------------------------------------------------------------


def _comparison(current, rows, semester="Fall 2026"):
    table = pd.DataFrame(
        [
            {"Semester": name, "Same season": same, "Spent by this point": 0.0,
             "Of its budget": pct, "Ended the term at": 100.0}
            for name, same, pct in rows
        ],
        columns=vp_history.TABLE_COLUMNS,
    )
    return vp_history.PastComparison(semester, 50.0, TODAY, 0.0, current, table)


def test_the_sentence_prefers_terms_from_the_same_season():
    comparison = _comparison(95.0, [("Spring 2026", False, 80.0), ("Fall 2025", True, 15.0), ("Fall 2024", True, 25.0)])
    text = vp_history.comparison_sentence(comparison)
    assert "past Fall terms" in text
    assert "about 20%" in text  # the middle of 15 and 25, not dragged up by Spring's 80
    assert "You are at 95%" in text and "more than usual" in text


def test_with_no_earlier_term_in_the_season_it_falls_back_to_all_past_terms():
    comparison = _comparison(60.0, [("Spring 2026", False, 80.0)])
    text = vp_history.comparison_sentence(comparison)
    assert "past terms" in text and "Fall terms" not in text
    assert "less than usual" in text


@pytest.mark.parametrize(
    ("current", "expected"),
    [(85.0, "about the same as usual"), (90.0, "about the same as usual"), (91.0, "more than usual"),
     (70.0, "about the same as usual"), (69.0, "less than usual")],
)
def test_within_ten_points_counts_as_the_same(current, expected):
    comparison = _comparison(current, [("Fall 2025", True, 80.0)])
    assert expected in vp_history.comparison_sentence(comparison)


def test_no_sentence_without_something_to_compare():
    assert vp_history.comparison_sentence(None) is None
    assert vp_history.comparison_sentence(_comparison(None, [("Fall 2025", True, 80.0)])) is None
    assert vp_history.comparison_sentence(_comparison(50.0, [])) is None


# --- The table a VP sees -------------------------------------------------------


def test_the_display_table_leads_with_this_term_and_tags_the_same_season():
    shown = vp_history.display_frame(compare())
    assert shown["Term"].tolist() == ["Fall 2026 (now)", "Spring 2026", "Fall 2025 · same season"]
    assert shown["Of its budget"].tolist() == pytest.approx([50.0, 80.0, 15.0])
    assert shown["Ended the term at"].tolist() == ["—", "100%", "65%"]  # this term has not ended


# --- The simpler headline and chart --------------------------------------------


def test_the_verdict_and_short_sentence_agree_with_the_long_one():
    comparison = _comparison(95.0, [("Spring 2026", False, 80.0), ("Fall 2025", True, 15.0), ("Fall 2024", True, 25.0)])
    usual = vp_history.compare_to_usual(comparison)
    assert usual == {"typical": 20.0, "current": 95.0, "label": "past Fall terms", "verdict": "more"}
    assert vp_history.VERDICT_TAGS[usual["verdict"]] == "More than usual"
    assert vp_history.short_sentence(comparison) == (
        "95% of your budget is used. By this point, past Fall terms had used about 20%."
    )


def test_no_short_sentence_without_something_to_compare():
    assert vp_history.compare_to_usual(None) is None
    assert vp_history.short_sentence(_comparison(None, [("Fall 2025", True, 80.0)])) is None


def test_the_chart_leads_with_this_term_and_skips_terms_with_no_budget():
    comparison = _comparison(60.0, [("Spring 2026", False, 37.0), ("Fall 2025", True, None)])
    frame = vp_history.chart_frame(comparison)
    assert frame.to_dict("records") == [
        {"Term": "Fall 2026", "Used": 60.0, "Now": True},
        {"Term": "Spring 2026", "Used": 37.0, "Now": False},
    ]
