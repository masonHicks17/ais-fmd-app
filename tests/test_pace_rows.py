"""Committees to watch as progress rows (2026-11-09)."""

from __future__ import annotations

import pandas as pd

from ais_fmd.ui import shell, theme


def _table():
    return pd.DataFrame({
        "Committee_Name": ["Professional Development", "Consulting", "Marketing", "Membership"],
        "Budget": [1104.0, 4472.0, 973.0, 5371.0],
        "Spent": [717.0, 2800.0, 558.0, 2414.0],
        "Remaining": [387.0, 1672.0, 415.0, 2957.0],
        "Used": [65.0, 63.0, 57.0, 45.0],
        "Pace": [0.94, 0.91, 0.83, 0.65],
        "Runs out": [pd.Timestamp("2026-12-20"), pd.NaT, pd.NaT, pd.NaT],
        "Status": ["approaching", "on track", "on track", "on track"],
    })


def test_rows_show_name_share_bar_and_term_mark():
    html = shell.pace_rows(_table(), 0.69, rows=3)
    assert html.count('class="ais-pace-row"') == 3 and "Membership" not in html
    assert "Professional Development" in html and "<b>65%</b>" in html
    assert "width:65.0%" in html and "left:69.0%" in html
    assert "$717.00 of $1,104.00 · runs out Dec 20" in html
    assert theme.LIGHT.APPROACHING in html  # status colour on the fill


def test_an_overspent_committee_is_capped_at_the_track_and_says_so():
    table = _table().iloc[:1].assign(Used=[130.0], Status=["over"], Spent=[1435.0])
    html = shell.pace_rows(table, 0.5)
    assert "width:100.0%" in html and "over budget" in html and theme.LIGHT.OVER in html
