"""
The "same point in past terms" section on My Committee.

The comparison itself is tested in test_vp_history.py. These render the real page
with the clock pinned, because the section depends on today's date and a test that
passes in October must also pass in March.
"""

from __future__ import annotations

import pandas as pd

from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import vp_history
from ais_fmd.domain.dedupe import assign_natural_keys

from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import OFFICER, run_as  # noqa: F401

PINNED_TODAY = pd.Timestamp("2026-10-01")  # about 38% of the way through Fall 2026


def _pin_the_clock(monkeypatch) -> None:
    original = vp_history.same_point_in_past_terms

    def pinned(*args, **kwargs):
        kwargs["today"] = PINNED_TODAY
        return original(*args, **kwargs)

    monkeypatch.setattr(vp_history, "same_point_in_past_terms", pinned)


def _past_spending_for_consulting() -> None:
    """Make sure Consulting (line 7) has spending in two earlier terms to compare with."""
    records = [
        {"transaction_date": date, "amount": amount, "details": f"TEST PAST CHARGE {i}",
         "budget_category": 7, "purpose": "Misc.", "account": "Wells Fargo"}
        for i, (date, amount) in enumerate(
            [("2025-09-10", -120.0), ("2025-11-01", -200.0), ("2026-02-10", -150.0), ("2026-04-01", -90.0)]
        )
    ]
    receipt = SqliteBackend().insert_transactions(assign_natural_keys(records), "test_past.csv", "tests")
    assert receipt.ok, receipt.error


def test_my_committee_compares_this_term_with_the_same_point_in_past_terms(seeded_db, use_db, monkeypatch):
    use_db(seeded_db)
    _past_spending_for_consulting()
    _pin_the_clock(monkeypatch)

    app = run_as(VIEWS / "Officer.py", OFFICER, 7)
    assert_clean(app, "My Committee as the Consulting VP")

    assert any("This term vs. past terms" in block.value for block in app.markdown)
    table = next((block.value for block in app.markdown if "ais-compare" in block.value), None)
    assert table is not None, "the comparison table was not drawn"
    # This term is the first row, marked Now, ahead of the past terms.
    assert table.index("Fall 2026") < table.index("Spring 2026")
    assert "ais-chip-now" in table
    # What the bars mean is in the section title's "?" (2026-11-13); how far through the term stays visible.
    assert any("by the same point" in block.value for block in app.markdown)
    assert any("of the way through the term" in caption.value for caption in app.caption)


def test_the_section_stays_quiet_when_it_is_too_early_in_the_term(seeded_db, use_db, monkeypatch):
    use_db(seeded_db)
    _past_spending_for_consulting()
    original = vp_history.same_point_in_past_terms
    monkeypatch.setattr(
        vp_history,
        "same_point_in_past_terms",
        lambda *a, **k: original(*a, **{**k, "today": pd.Timestamp("2026-08-18")}),  # days into the term
    )
    app = run_as(VIEWS / "Officer.py", OFFICER, 7)
    assert_clean(app, "My Committee early in the term")
    assert not any("This term vs. past terms" in block.value for block in app.markdown)
