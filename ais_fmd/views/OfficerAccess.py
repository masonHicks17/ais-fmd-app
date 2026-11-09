"""Module M22 -- who can sign in as what, and which committee they see."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_label
from ais_fmd.data import repositories as repo
from ais_fmd.ui import shell

identity = auth.require(auth.Role.TREASURER)
auth.refuse_read_only(identity)

shell.environment_banner()
shell.page_header(
    "Officer Access",
    "Who can sign in with Google, and what they see once they do. Doesn't "
    "touch the treasury password — that still works for anyone who has it, "
    "unchanged.",
)

if not auth.google_login_available():
    shell.notify(
        "info",
        "Google sign-in isn't configured on this deployment yet, so nothing "
        "added here can be used until it is. You can still set up committee "
        "assignments in advance — see HANDOFF.md for the two things a deployment "
        "needs (a Google Cloud OAuth app, and the `[auth]` section in secrets).",
    )

# Only committee heads sign in: a VP per committee, the Treasurer (who heads
# Treasury) and the President. "member" and "admin" are still valid stored
# roles, but no one is added with them any more (2026-11-09).
ROLE_OPTIONS = ["officer", "treasurer", "president"]
ROLE_HELP = {
    "member": "Read-only dashboards.",
    "officer": "Adds their own committee's detail and reimbursement requests.",
    "treasurer": "Full write access — uploads, categorisation, budgets.",
    "president": "Sees everything the treasurer sees; cannot change anything.",
    "admin": "Everything, including this page.",
}

committee_options = ["— none —"] + [committee_label(c) for c in BUDGETED_COMMITTEE_IDS]

# --- Add or edit one -----------------------------------------------------

with st.expander("Add or update someone", expanded=True):
    with st.form("officer_access_form", clear_on_submit=True):
        cols = st.columns([2, 1.4, 1.4, 1.4])
        email = cols[0].text_input(
            "Email", placeholder="vp@ufl.edu", help="Must match their Google account exactly."
        )
        display_name = cols[1].text_input("Name", placeholder="optional")
        role = cols[2].selectbox(
            "Role", ROLE_OPTIONS, index=0, help=ROLE_HELP["officer"],
            format_func=lambda r: {"officer": "VP"}.get(r, r.title()),
        )
        committee_choice = cols[3].selectbox("Committee", committee_options)
        submitted = st.form_submit_button("Save", type="primary")

    if submitted:
        cleaned_email = email.strip()
        if not cleaned_email or "@" not in cleaned_email:
            shell.error_state("Not saved", "Enter a valid email address.")
        elif role == "officer" and committee_choice == "— none —":
            # An officer is defined by their committee. Saving one without it would
            # let them sign in and then see nothing, which looks like a bug.
            shell.error_state("Not saved", "An officer needs a committee. Pick one.")
        else:
            committee_id = (
                None
                if committee_choice == "— none —"
                else next(
                    c for c in BUDGETED_COMMITTEE_IDS if committee_label(c) == committee_choice
                )
            )
            result = repo.upsert_profile(
                cleaned_email, role, committee_id, display_name.strip(), identity.email
            )
            if result.ok:
                repo.invalidate()
                shell.notify("success", f"Saved access for {cleaned_email}.")
                st.rerun()
            else:
                shell.error_state("Could not save", result.error or "")

# --- Current list ----------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Current access")

profiles = repo.load_profiles()
if profiles.empty:
    shell.empty_state(
        "Nobody has been given individual access yet",
        "Everyone still reaches the app only through the shared treasury password.",
    )
    st.stop()

display = profiles.copy()
display["Committee"] = display["committee_id"].map(
    lambda c: committee_label(int(c)) if pd.notna(c) else "—"
)
shell.dataframe(
    pd.DataFrame(
        {
            "Email": display["email"],
            "Name": display["display_name"].fillna("—"),
            "Role": display["role"],
            "Committee": display["Committee"],
        }
    ),
    hide_index=True,
)

st.markdown("###### Remove someone's access")
remove_cols = st.columns([3, 1])
target = remove_cols[0].selectbox(
    "Email to remove", [""] + list(profiles["email"]), label_visibility="collapsed"
)
if remove_cols[1].button("Remove", disabled=not target):
    result = repo.remove_profile(target)
    if result.ok:
        repo.invalidate()
        shell.notify("success", f"Removed {target}.")
        st.rerun()
    else:
        shell.error_state("Could not remove", result.error or "")
