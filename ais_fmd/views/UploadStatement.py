"""
Upload statement: import a Venmo or Wells Fargo statement into the ledger.

Moved out of Treasury into the Transactions hub (2026-11-08) so statements are
imported next to the transactions they create. The flow itself is unchanged.

Fixes visible on this page:
  F3  deduplication keyed on date+amount+details+account with an occurrence
      ordinal, enforced by a unique index rather than a pandas filter
  F4  transactions and the uploaded-files marker commit together or not at all
  F5  statement shape is validated before parsing; unreadable rows are reported
      rather than imported as $0.00
  F7  the committee reference table is generated from config, so it cannot
      contradict the mapping the code applies
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    ACCOUNT_VENMO,
    ACCOUNT_WELLS_FARGO,
    committee_dropdown_options,
    committee_label,
    committee_reference_rows,
    parse_committee_label,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues
from ais_fmd.domain.categorize.pipeline import categorize_frame
from ais_fmd.domain.dedupe import split_new_and_duplicate
from ais_fmd.domain.parsers import venmo, wells_fargo
from ais_fmd.ui import shell

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Upload statement",
    "Import a Venmo or Wells Fargo statement. Rows are categorized, checked for "
    "duplicates and shown to you before anything is written.",
)

SOURCES = {
    "Venmo": {
        "parser": venmo,
        "account": ACCOUNT_VENMO,
        "hint": "Venmo statement export (CSV or Excel).",
        "filename_hint": "venmo",
    },
    "Wells Fargo checking": {
        "parser": wells_fargo,
        "account": ACCOUNT_WELLS_FARGO,
        "hint": "Wells Fargo checking export (CSV or Excel).",
        "filename_hint": "checking",
    },
}


with st.expander("Committee ID reference", expanded=False):
    reference = pd.DataFrame(committee_reference_rows(), columns=["ID", "Committee"])
    left, right = st.columns(2)
    midpoint = (len(reference) + 1) // 2
    with left:
        shell.dataframe(reference.iloc[:midpoint], height=340)
    with right:
        shell.dataframe(reference.iloc[midpoint:], height=340)
    st.caption(
        "Generated from config/categories.py — the same structure the "
        "categorizer uses, so this table cannot drift from the mapping."
    )

source_name = st.radio(
    "Statement source", list(SOURCES), horizontal=True, key="upload_source"
)
source = SOURCES[source_name]
st.caption(source["hint"])

uploaded = st.file_uploader(
    f"Upload {source_name} statement",
    type=["csv", "xlsx", "xls"],
    key=f"upload_{source_name}",
)

if uploaded is not None:
    file_name = uploaded.name

    if repo.is_file_uploaded(file_name):
        st.warning(
            f"**{file_name}** has already been uploaded. "
            f"Re-uploading is safe — duplicates are rejected by the database — "
            f"but nothing new will be added."
        )

    if source["filename_hint"] not in file_name.lower():
        st.info(
            f"Filename does not contain '{source['filename_hint']}'. "
            f"Proceeding anyway — the parser validates the file's actual shape, "
            f"not its name."
        )

    # --- Parse ----------------------------------------------------------
    try:
        if file_name.lower().endswith(".csv"):
            header = None if source_name == "Wells Fargo checking" else "infer"
            raw = pd.read_csv(uploaded, header=header)
        else:
            header = None if source_name == "Wells Fargo checking" else 0
            raw = pd.read_excel(uploaded, header=header)
    except Exception as exc:  # noqa: BLE001
        raw = None
        shell.error_state("Could not read the file", f"{type(exc).__name__}: {exc}")

    if raw is not None:
        parsed = source["parser"].parse(raw, source_file=file_name)

        for warning in parsed.warnings:
            st.warning(warning)

        if not parsed.ok:
            shell.error_state(
                "This file was not imported",
                "\n\n".join(parsed.errors) or "No usable rows found.",
            )
        else:
            st.success(f"Parsed {parsed.row_count:,} transactions from {file_name}.")

            # --- Categorize -------------------------------------------
            # Per-term rates, so a statement from a term with its own dues
            # rate is categorized against that rate and not a stale default.
            schedule = dues.schedule_from_terms(repo.load_terms())
            categorized, run = categorize_frame(parsed.rows, dues=schedule)

            counts = run.counts_by_source
            summary_columns = st.columns(4)
            summary_columns[0].metric("By card or rule", counts["rule"])
            summary_columns[1].metric("By scoring", counts["scored"])
            summary_columns[2].metric("By model", counts["llm"])
            summary_columns[3].metric("Need review", counts["none"])

            st.caption(
                f"{run.rows_resolved_locally} of {run.total} resolved without any "
                f"model call. Only {run.rows_sent_to_model} would ever be sent."
            )
            if run.llm.skipped_reason:
                st.caption(f"Model pass: {run.llm.skipped_reason}")
            elif run.llm.fully_failed:
                shell.error_state(
                    "Every model batch failed",
                    "\n".join(run.llm.errors[:3]),
                )

            # --- Dedupe -----------------------------------------------
            existing = repo.load_transactions()
            existing_records = existing.to_dict("records") if not existing.empty else []
            staged = categorized.to_dict("records")
            for record in staged:
                if isinstance(record.get("transaction_date"), pd.Timestamp):
                    record["transaction_date"] = record["transaction_date"].strftime("%Y-%m-%d")

            dedupe = split_new_and_duplicate(staged, existing_records)

            if dedupe.duplicates:
                st.info(
                    f"{len(dedupe.duplicates)} row(s) already exist and will be skipped. "
                    f"Genuine same-day repeat purchases are *not* treated as duplicates."
                )
                with st.expander("Show skipped duplicates"):
                    shell.dataframe(
                        pd.DataFrame(dedupe.duplicates)[
                            ["transaction_date", "amount", "details"]
                        ].rename(
                            columns={
                                "transaction_date": "Date",
                                "amount": "Amount",
                                "details": "Details",
                            }
                        ),
                        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
                    )

            if not dedupe.new:
                st.warning("Every row in this file already exists. Nothing to import.")
            else:
                st.markdown(f"#### Review {len(dedupe.new)} new transaction(s)")

                preview = pd.DataFrame(dedupe.new)
                preview_display = pd.DataFrame(
                    {
                        "Date": preview["transaction_date"],
                        "Amount": preview["amount"],
                        "Details": preview["details"],
                        "Committee": [
                            committee_label(value) for value in preview["budget_category"]
                        ],
                        "Purpose": preview["purpose"].fillna(""),
                        "Matched by": preview["match_rule"].fillna(""),
                    }
                )

                edited = st.data_editor(
                    preview_display,
                    key="upload_preview_editor",
                    width="stretch",
                    hide_index=True,
                    disabled=["Date", "Amount", "Details", "Matched by"],
                    column_config={
                        "Amount": st.column_config.NumberColumn(format="$%.2f"),
                        "Details": st.column_config.TextColumn(width="large"),
                        "Committee": st.column_config.SelectboxColumn(
                            options=committee_dropdown_options(), required=False
                        ),
                        "Purpose": st.column_config.SelectboxColumn(
                            options=purpose_dropdown_options(), required=False
                        ),
                        "Matched by": st.column_config.TextColumn(width="medium"),
                    },
                )

                if st.button(
                    f"Import {len(dedupe.new)} transaction(s)", type="primary", key="confirm_import",
                    disabled=identity.read_only,
                ):
                    final = []
                    for position, record in enumerate(dedupe.new):
                        row = edited.iloc[position]
                        final.append(
                            {
                                "transaction_date": record["transaction_date"],
                                "amount": record["amount"],
                                "details": record["details"],
                                "account": record["account"],
                                "budget_category": parse_committee_label(row["Committee"]),
                                "purpose": (row["Purpose"] or "").strip() or None,
                                "natural_key": record["natural_key"],
                            }
                        )

                    receipt = repo.insert_transactions(final, file_name, identity.email)
                    if not receipt.ok:
                        shell.error_state("Import failed — nothing was written", receipt.error or "")
                    else:
                        st.success(
                            f"Imported {receipt.inserted} transaction(s) from {file_name}."
                            + (
                                f" {receipt.duplicates_skipped} rejected as duplicates by the database."
                                if receipt.duplicates_skipped
                                else ""
                            )
                        )
                        st.balloons()
                        st.rerun()

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Previously uploaded files")
files = repo.load_uploaded_files()
if files.empty:
    shell.empty_state("Nothing uploaded yet")
else:
    shell.dataframe(files.head(25))
