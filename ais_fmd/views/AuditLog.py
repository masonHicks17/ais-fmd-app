"""
Module M2 -- audit trail viewer.

Because every write went through a shared service-role key behind a shared
password, the original could not answer "who recategorized this transaction".
Every mutation now records actor, field, old value and new value.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.ui import shell

auth.require(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Audit Log",
    "Who changed what, and when. Written by the data layer — there is no write "
    "path that skips it.",
)

audit = repo.load_audit(2000)

if audit.empty:
    shell.empty_state("No audit entries yet")
    st.stop()

metrics = st.columns(4)
metrics[0].metric("Entries", f"{len(audit):,}")
metrics[1].metric("Actors", f"{audit['actor'].nunique():,}")
metrics[2].metric("Edits", f"{int((audit['action'] == 'update').sum()):,}")
metrics[3].metric("Imports", f"{int((audit['action'] == 'insert').sum()):,}")

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

filters = st.columns(3)
with filters[0]:
    action = st.selectbox("Action", ["All"] + sorted(audit["action"].dropna().unique().tolist()))
with filters[1]:
    actor = st.selectbox("Actor", ["All"] + sorted(audit["actor"].dropna().unique().tolist()))
with filters[2]:
    search = st.text_input("Search transaction ID", placeholder="e.g. 412")

view = audit.copy()
if action != "All":
    view = view[view["action"] == action]
if actor != "All":
    view = view[view["actor"] == actor]
if search.strip():
    view = view[view["transaction_id"].astype("string").str.contains(search.strip(), na=False)]

st.caption(f"{len(view):,} of {len(audit):,} entries.")

shell.dataframe(
    view[["changed_at", "actor", "action", "transaction_id", "field", "old_value", "new_value"]]
    .rename(
        columns={
            "changed_at": "When",
            "actor": "Actor",
            "action": "Action",
            "transaction_id": "Txn",
            "field": "Field",
            "old_value": "From",
            "new_value": "To",
        }
    )
    .head(500),
    height=520,
)

with st.expander("Why this matters more than it looks"):
    st.markdown(
        """
Every write in the original went through the service-role key, which bypasses
row-level security entirely, gated only by a password shared across the whole
treasury committee. That combination means a change could not be attributed to
anyone, and there was no way to reconstruct what a figure looked like before an
edit.

The audit table also makes undo possible: each row carries the previous value.
"""
    )
