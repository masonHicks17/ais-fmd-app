"""
Module M13 -- data quality.

The original had two integrity checks printed as warnings at the bottom of the
Database Tools page. This is the same idea, promoted to its own page, with the
checks returning structured issues so they can be ranked and drilled into.
"""

from __future__ import annotations

import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import quality
from ais_fmd.ui import shell

auth.require(auth.Role.TREASURER)

shell.environment_banner()
shell.page_header(
    "Data Quality",
    "Everything the integrity checks found, worst first.",
)

bundle = repo.load_bundle()
issues = repo.run_quality_checks()

if not issues:
    st.success("Every check passes. Nothing to report.")
    st.stop()

severity_style = {
    "critical": "over",
    "high": "over",
    "medium": "approaching",
    "low": "muted",
    "info": "muted",
}

counts = {level: sum(1 for issue in issues if issue.severity == level) for level in
          ("critical", "high", "medium", "low")}
metrics = st.columns(4)
metrics[0].metric("Critical", counts["critical"])
metrics[1].metric("High", counts["high"])
metrics[2].metric("Medium", counts["medium"])
metrics[3].metric("Low", counts["low"])

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

for issue in issues:
    with st.container(border=True):
        st.markdown(
            f"{shell.pill(issue.severity, severity_style.get(issue.severity, 'muted'))} "
            f"&nbsp; **{issue.title}** &nbsp; · &nbsp; {issue.count:,} row(s)",
            unsafe_allow_html=True,
        )
        st.write(issue.detail)
        if issue.rows is not None and not issue.rows.empty:
            with st.expander(f"Show affected rows ({min(len(issue.rows), 200)} shown)"):
                shell.dataframe(issue.rows)

# --- F7 decision support -----------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Disputed purpose mappings")
st.caption(
    "The original code mapped these purposes to committees that contradict the "
    "reference table shown to treasurers. The original behaviour was preserved so "
    "historical figures did not shift — here is what changing it would affect."
)

report = quality.disputed_mapping_report(bundle.transactions)
if report.empty:
    shell.empty_state("No transactions carry a disputed purpose")
else:
    shell.dataframe(
        report,
        column_config={"Total amount": st.column_config.NumberColumn(format="dollar")},
    )
    st.caption(
        "To change a mapping, edit `PURPOSE_TO_COMMITTEE` in "
        "`ais_fmd/config/categories.py` and remove the entry from "
        "`DISPUTED_PURPOSE_MAPPINGS`."
    )
