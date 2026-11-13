"""
UF AIS Financial Management System -- entry point.

Run:
    streamlit run app.py

Defaults to sandbox mode: a local SQLite database, no network calls, no real
money. See README.md for the safety guarantees.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth, nav, settings
from ais_fmd.ui import shell

shell.bootstrap()

# --- Sandbox bootstrap -------------------------------------------------------
# Seed on first run so the app has something to show immediately.

if settings.is_sandbox():
    from ais_fmd.data.sqlite_backend import SqliteBackend

    if "sandbox_ready" not in st.session_state:
        backend = SqliteBackend()
        if backend.fetch_transactions().empty:
            with st.spinner("Preparing the sandbox database…"):
                from ais_fmd.data.seed import seed

                seed(backend, reset=False)
        st.session_state.sandbox_ready = True

# --- Authentication ----------------------------------------------------------
# Sandbox is open and switches roles from the profile menu. Production requires the
# operator password before any page renders.

auth.login_gate()

# --- Navigation --------------------------------------------------------------
#
# PEBBLE CONSOLIDATION: sixteen entries -> eleven. Eight primary pages sit in
# the top pill bar; three admin pages sit in the profile menu (top right) since
# 2026-11-08, when it replaced the sidebar and the "More" section. Merged pages are hubs
# (views/*Hub.py) that run the original, unchanged view files one at a time:
#
#   Dues          = Dues + Roster
#   Transactions  = Transactions + Review Queue (Import moved to Treasury -> Reconcile)
#   Assistant     = Assistant + Runbook (as "Help")
#   Alerts & Data Quality = Alerts & Reports + Data Quality
#   Settings      = Officer Access + Audit Log
#
# MVP SCOPE. Reimbursements is commented out rather than deleted: the page, its
# domain module, its tests and its SQLite tables all still exist and still
# pass, and the Postgres side is waiting in migrations/002_deferred_features.sql.
#
# Officer Access, the VP portal's admin page, was deferred 2026-09-08 and turned
# back on 2026-10-26. It maps an email to a role and a committee, which is what
# Google sign-in looks up in `profiles`. In production that table comes from
# migration 013.
#
# `Officer.py` ("My Committee") is deliberately a primary page -- it is a
# read-only committee view that a treasurer outranks, and it stays available.

PROFILE_SECTION = "Profile"

PAGES = {
    "": [
        ("ais_fmd/views/Home.py", "Home", ":material/home:", auth.Role.MEMBER),
        # Dashboard merged into Home on 2026-11-08 ("This term in detail").
        ("ais_fmd/views/DuesHub.py", "Dues", ":material/groups:", auth.Role.MEMBER),
        ("ais_fmd/views/TransactionsHub.py", "Transactions", ":material/table_rows:", auth.Role.MEMBER),
        ("ais_fmd/views/Events.py", "Events", ":material/calendar_month:", auth.Role.MEMBER),
        ("ais_fmd/views/Planner.py", "Planner", ":material/insights:", auth.Role.TREASURER),
        ("ais_fmd/views/AssistantHub.py", "Assistant", ":material/smart_toy:", auth.Role.MEMBER),
        ("ais_fmd/views/Officer.py", "My Committee", ":material/badge:", auth.Role.OFFICER),
        # ("ais_fmd/views/Reimbursements.py", "Reimbursements", ":material/receipt_long:", auth.Role.MEMBER),  # MVP: deferred
        ("ais_fmd/views/Treasury.py", "Treasury", ":material/account_balance:", auth.Role.TREASURER),
    ],
    # Kept out of the top bar and opened from the profile menu (top right).
    # Still registered with st.navigation, as hidden pages, so links, alerts
    # and URLs reach them and each page's own role gate still applies.
    PROFILE_SECTION: [
        ("ais_fmd/views/AlertsHub.py", "Alerts & Data Quality", ":material/notifications:", auth.Role.MEMBER),
        ("ais_fmd/views/Reconciliation.py", "Reconciliation", ":material/balance:", auth.Role.MEMBER),
        ("ais_fmd/views/SettingsHub.py", "Settings", ":material/settings:", auth.Role.TREASURER),
    ],
}

identity = auth.current_user()

# A VP sees a short, committee-scoped navigation instead of the full list; see
# `ais_fmd/nav.py` for why hiding pages is what actually keeps other committees'
# data out of their reach.
visible = nav.visible_pages(identity, PAGES)
profile_pages = [
    st.Page(path, title=title, icon=icon, visibility="hidden")
    for path, title, icon, required in visible.get(PROFILE_SECTION, [])
]
pages = [
    st.Page(path, title=title, icon=icon, default=(title == "Home"))
    for section, entries in visible.items()
    if section != PROFILE_SECTION
    for path, title, icon, required in entries
]
navigation = st.navigation(pages + profile_pages, position="top")

# Light or dark follows the device unless chosen in the profile menu; the
# helper writes the choice for every page above (ui/appearance.py).
shell.appearance_sync(
    pages + profile_pages,
    auth.person_label(identity) if settings.is_sandbox() and not settings.sandbox_login_enabled() else None,
)

# --- Profile menu ------------------------------------------------------------
# Replaced the sidebar on 2026-11-08: who you are, the admin pages, and in the
# sandbox the role switcher and reseed button.


def _sandbox_controls() -> None:
    if st.button(
        "Reset & reseed data",
        icon=":material/restart_alt:",
        width="stretch",
        help="Wipes and regenerates the local SQLite file. Nothing outside this folder is touched.",
    ):
        from ais_fmd.data.seed import seed
        from ais_fmd.data import repositories as repo

        with st.spinner("Rebuilding the sandbox database…"):
            summary = seed()
            repo.invalidate()
        st.toast(f"Reseeded {summary['transactions']} transactions.")
        st.rerun()


shell.profile_menu(profile_pages, _sandbox_controls if settings.is_sandbox() else None)

navigation.run()
