"""
Dashboard: the term analysis as a page of its own.

Merged into Home on 2026-11-08 and taken out of the navigation; the content
lives in ui/term_analysis.py and renders under Home's "today" cards. This file
stays so the analysis can still be opened alone and tested on its own.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain.terms import ordered_semesters
from ais_fmd.ui import shell, term_analysis

auth.require(auth.Role.MEMBER)

shell.environment_banner()

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.page_header("Dashboard")
    shell.empty_state("No terms defined", "Add an academic term before using the dashboard.")
    st.stop()

term_analysis.render(bundle, semesters, as_page=True)
