"""
The data-freshness line, the spend-vs-pace chart, the CSV download and the
printable committee report.

Two properties matter most. A report or export must contain only the VP's own
committee, and nothing in either may run as code when opened: a bank description
is free text, so it is escaped in the HTML and neutralised in the CSV.
"""

from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
import pytest

from ais_fmd.config import vp_committees
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import committee_report, exports, vp_metrics
from ais_fmd.domain.money import format_currency
from ais_fmd.ui import committee_charts

# Fixtures and helpers shared with the other VP tests.
from tests.test_views import seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import OUTSIDE, membership_db, two_line_committee  # noqa: F401

GENERATED = datetime(2026, 10, 1, 9, 30)


# --- Data freshness ------------------------------------------------------------


def _ledger(*dates):
    return pd.DataFrame({"transaction_date": pd.to_datetime(list(dates)), "amount": [-1.0] * len(dates)})


def test_freshness_names_the_latest_charge_and_the_last_upload():
    uploads = pd.DataFrame({"uploaded_at": ["2026-09-20 10:00:00", "2026-10-01 12:00:00"]})
    text = vp_metrics.freshness_text(_ledger("2026-09-01", "2026-10-22"), uploads)
    assert text == "Includes charges through Oct 22, 2026. Statements last uploaded Oct 1, 2026."


def test_freshness_without_upload_records_still_says_how_far_the_data_reaches():
    text = vp_metrics.freshness_text(_ledger("2026-10-22"), pd.DataFrame())
    assert text == "Includes charges through Oct 22, 2026."


def test_an_empty_ledger_says_there_is_nothing_to_show():
    text = vp_metrics.freshness_text(pd.DataFrame(), pd.DataFrame())
    assert "No statements" in text
    assert vp_metrics.data_freshness(None, None) == {"through": None, "uploaded": None}


# --- Cumulative spend ----------------------------------------------------------

START, END = pd.Timestamp("2026-08-01"), pd.Timestamp("2026-12-18")


def _spend_frame():
    return pd.DataFrame(
        {
            "transaction_date": pd.to_datetime(
                ["2026-08-02", "2026-08-02", "2026-08-04", "2026-08-03", "2026-07-31", "2026-08-10"]
            ),
            "amount": [-10.0, -5.0, -20.0, 100.0, -999.0, -50.0],
        }
    )


def test_cumulative_spend_adds_expenses_up_day_by_day_and_stops_where_the_data_stops():
    out = vp_metrics.cumulative_spend(_spend_frame(), START, END, pd.Timestamp("2026-08-05"))
    assert list(out.columns) == ["date", "spent"]
    assert out["date"].iloc[0] == START and out["date"].iloc[-1] == pd.Timestamp("2026-08-05")
    # Income, the charge before the term and the charge after the data stops are all ignored.
    assert out["spent"].tolist() == [0.0, 15.0, 15.0, 35.0, 35.0]


def test_cumulative_spend_never_runs_past_the_end_of_the_term():
    out = vp_metrics.cumulative_spend(_spend_frame(), START, END, pd.Timestamp("2027-03-01"))
    assert out["date"].iloc[-1] == END
    assert out["spent"].iloc[-1] == 85.0


def test_cumulative_spend_with_nothing_to_plot_is_an_empty_frame_with_the_right_columns():
    assert vp_metrics.cumulative_spend(_spend_frame(), START, END, None).empty
    assert vp_metrics.cumulative_spend(_spend_frame(), START, END, pd.Timestamp("2026-07-01")).empty
    flat = vp_metrics.cumulative_spend(pd.DataFrame(), START, END, pd.Timestamp("2026-08-03"))
    assert flat["spent"].tolist() == [0.0, 0.0, 0.0]


# --- The pace chart ------------------------------------------------------------


def test_the_pace_chart_draws_the_on_pace_line_and_the_spend_so_far():
    spend = vp_metrics.cumulative_spend(_spend_frame(), START, END, pd.Timestamp("2026-08-05"))
    figure = committee_charts.pace_chart(spend, 1000.0, START, END, today=pd.Timestamp("2026-08-05"))
    assert [trace.name for trace in figure.data] == ["On pace", "Spent"]
    assert list(figure.data[0].y) == [0.0, 1000.0]


def test_spend_past_the_budget_turns_the_budget_line_orange_and_says_so():
    under = vp_metrics.cumulative_spend(_spend_frame(), START, END, pd.Timestamp("2026-08-05"))
    fine = committee_charts.pace_chart(under, 1000.0, START, END)
    over = committee_charts.pace_chart(under, 20.0, START, END)
    assert fine.layout.shapes[0].line.color != over.layout.shapes[0].line.color
    assert fine.layout.annotations[0].text.startswith("Budget")
    assert over.layout.annotations[0].text.startswith("Over budget")


def test_the_pace_chart_copes_with_no_spend_and_with_no_budget_at_all():
    nothing = pd.DataFrame(columns=["date", "spent"])
    assert [t.name for t in committee_charts.pace_chart(nothing, 500.0, START, END).data] == ["On pace"]
    blank = committee_charts.pace_chart(nothing, 0.0, START, END)
    assert "No budget or spending" in blank.layout.annotations[0].text


# --- CSV -----------------------------------------------------------------------


@pytest.mark.parametrize("cell", ["=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\t=1"])
def test_text_a_spreadsheet_would_run_as_a_formula_is_made_inert(cell):
    assert exports.csv_safe(cell) == "'" + cell


def test_ordinary_text_and_numbers_are_left_alone():
    assert exports.csv_safe("PURCHASE AUTHORIZED ON 10/13") == "PURCHASE AUTHORIZED ON 10/13"
    assert exports.csv_safe(-12.5) == -12.5
    assert exports.csv_safe(None) is None


def test_the_csv_keeps_negative_amounts_as_numbers_and_defuses_hostile_text():
    table = pd.DataFrame(
        {"Amount": [-12.5, 40.0], "Details": ["=HYPERLINK(\"http://evil\")", "CAFÃ‰ COFFEE"]}
    )
    raw = exports.transactions_csv(table)
    assert raw.startswith(b"\xef\xbb\xbf")  # byte-order mark, so Excel reads accents correctly
    text = raw.decode("utf-8-sig")
    assert "-12.5" in text and "'-12.5" not in text  # a number, not neutralised text
    assert "'=HYPERLINK" in text and ",=HYPERLINK" not in text
    assert "CAFÃ‰ COFFEE" in text


def test_text_stored_with_pandas_own_string_type_is_still_neutralised():
    table = pd.DataFrame({"Details": pd.array(["=1+1", "fine"], dtype="string")})
    assert "'=1+1" in exports.transactions_csv(table).decode("utf-8-sig")


def test_file_names_are_safe():
    assert exports.slug("Professional Development") == "professional-development"
    assert exports.slug("../../etc/passwd") == "etc-passwd"
    assert exports.slug("") == "committee"


# --- The printable report ------------------------------------------------------


def _ledger_frames():
    backend = SqliteBackend()
    return backend.fetch_transactions(), backend.fetch_budgets(), backend.fetch_terms()


def test_the_report_holds_only_the_committees_own_lines(membership_db):
    transactions, budgets, terms = _ledger_frames()
    owned = vp_committees.budget_ids_for(5)
    own = transactions[transactions["budget_category"].isin(owned)]
    other = transactions[~transactions["budget_category"].isin(owned).fillna(False)]
    foreign_details = set(other["details"].dropna()) - set(own["details"].dropna())
    assert len(foreign_details) > 20, "the check needs plenty of other committees' charges to mean anything"

    report = committee_report.build_for_committee(
        transactions, budgets, terms, 5, "Fall 2026", generated=GENERATED
    )
    assert "Membership" in report
    leaked = [detail for detail in foreign_details if detail in report]
    assert not leaked, f"another committee's charge appeared in the report: {leaked[:2]}"


def test_the_membership_report_never_includes_the_presidents_line(membership_db):
    """The President's line is outside the VP committees: its name and charges stay out of a VP's report."""
    transactions, budgets, terms = _ledger_frames()
    president_charges = set(transactions[transactions["budget_category"] == OUTSIDE]["details"].dropna())
    assert president_charges, "the fixture should have charges on the President's line"
    report = committee_report.build_for_committee(
        transactions, budgets, terms, 5, "Fall 2026", generated=GENERATED
    )
    assert "President" not in report
    assert not [d for d in president_charges if html.escape(d[:90]) in report]


def test_the_report_figures_match_the_page(membership_db):
    transactions, budgets, terms = _ledger_frames()
    summary = budget_domain.budget_vs_actual(transactions, budgets, terms, "Fall 2026")
    position = vp_metrics.rollup(summary, ["Membership"])
    report = committee_report.build_for_committee(
        transactions, budgets, terms, 5, "Fall 2026", generated=GENERATED
    )
    assert format_currency(position["budget"]) in report
    assert format_currency(position["spent"]) in report


def test_a_two_line_committee_report_adds_its_lines_together(membership_db, two_line_committee):
    """The roll-up still works for a committee that owns several lines."""
    transactions, budgets, terms = _ledger_frames()
    summary = budget_domain.budget_vs_actual(transactions, budgets, terms, "Fall 2026")
    position = vp_metrics.rollup(summary, ["Membership", "President"])
    report = committee_report.build_for_committee(
        transactions, budgets, terms, OUTSIDE, "Fall 2026", generated=GENERATED  # via the second line
    )
    assert "Membership" in report and "President" in report
    assert format_currency(position["budget"]) in report
    assert format_currency(position["spent"]) in report


def test_a_single_line_committee_report_does_not_mention_another_committees_line(membership_db):
    transactions, budgets, terms = _ledger_frames()
    report = committee_report.build_for_committee(
        transactions, budgets, terms, 7, "Fall 2026", generated=GENERATED
    )
    assert "Consulting" in report
    assert "President" not in report


def test_the_report_is_self_contained_and_flags_sandbox_data(membership_db):
    transactions, budgets, terms = _ledger_frames()
    args = (transactions, budgets, terms, 5, "Fall 2026")
    sandbox = committee_report.build_for_committee(*args, generated=GENERATED, sandbox=True)
    real = committee_report.build_for_committee(*args, generated=GENERATED, sandbox=False)
    assert "SANDBOX DATA" in sandbox and "SANDBOX DATA" not in real
    for forbidden in ("http://", "https://", "<script", "<link", "src="):
        assert forbidden not in real, f"the report must not contain {forbidden!r}"


def test_hostile_text_in_a_description_cannot_become_markup():
    terms = pd.DataFrame(
        {"TermID": ["T1"], "Semester": ["Fall 2026"], "start_date": ["2026-08-01"], "end_date": ["2026-12-18"]}
    )
    budgets = pd.DataFrame({"termid": ["T1"], "committeeid": [7], "budget_amount": [1000.0]})
    transactions = pd.DataFrame(
        {
            "transactionid": [1],
            "transaction_date": pd.to_datetime(["2026-09-01"]),
            "amount": [-50.0],
            "details": ["<script>alert(1)</script> & \"quoted\""],
            "budget_category": pd.array([7], dtype="Int64"),
            "purpose": ["<b>Misc.</b>"],
        }
    )
    report = committee_report.build_for_committee(
        transactions, budgets, terms, 7, "Fall 2026", generated=GENERATED
    )
    assert "<script" not in report and "<b>Misc." not in report
    assert "&lt;script&gt;" in report


def test_a_report_needs_a_committee_and_survives_an_empty_ledger():
    terms = pd.DataFrame(
        {"TermID": ["T1"], "Semester": ["Fall 2026"], "start_date": ["2026-08-01"], "end_date": ["2026-12-18"]}
    )
    with pytest.raises(ValueError):
        committee_report.build_for_committee(
            pd.DataFrame(), pd.DataFrame(), terms, None, "Fall 2026", generated=GENERATED
        )
    report = committee_report.build_for_committee(
        pd.DataFrame(), pd.DataFrame(), terms, 7, "Fall 2026", generated=GENERATED
    )
    assert "Consulting" in report and "Nothing to report" in report
