"""
Import a statement, start to finish, on one page.

  1. Upload  -- parse, apply the rules (free), set aside duplicates.
  2. Ask Haiku about what the rules left -- one metered run, and a table of
     exactly what it answered, how sure it was, and what that did to each row.
  3. Review and import.
  4. Check the new charges against the committee profiles -- one click runs
     every charge not yet checked, and a table shows every verdict.

Moved here from the Treasury page (2026-10-20) so an import flows straight
through instead of across tabs. The fixes the Treasury page documented travel
with it:
  F3  deduplication keyed on date+amount+details+account with an occurrence
      ordinal, enforced by a unique index rather than a pandas filter
  F4  transactions and the uploaded-files marker commit together or not at all
  F5  statement shape is validated before parsing; unreadable rows are reported
      rather than imported as $0.00
  F7  the committee reference table is generated from config
"""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from ais_fmd import auth, settings
from ais_fmd.config.categories import (
    ACCOUNT_VENMO,
    ACCOUNT_WELLS_FARGO,
    committee_dropdown_options,
    committee_label,
    committee_name,
    committee_reference_rows,
    parse_committee_label,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues, model_runs
from ais_fmd.domain.categorize.pipeline import AUTO_APPLY_THRESHOLD, categorize_frame, categorize_records
from ais_fmd.domain.dedupe import split_new_and_duplicate
from ais_fmd.domain.parsers import venmo, wells_fargo
from ais_fmd.ui import shell
from ais_fmd.ui import spend_review as spend_review_ui

identity = auth.require(auth.Role.TREASURER)

SOURCES = {
    "Wells Fargo checking": {
        "parser": wells_fargo,
        "account": ACCOUNT_WELLS_FARGO,
        "hint": "Wells Fargo checking export (CSV or Excel).",
        "filename_hint": "checking",
    },
    "Venmo": {
        "parser": venmo,
        "account": ACCOUNT_VENMO,
        "hint": "Venmo statement export (CSV or Excel).",
        "filename_hint": "venmo",
    },
}
_CATEGORIZED_COLUMNS = ("budget_category", "purpose", "match_rule", "match_confidence", "match_source")
_UPLOADER_ROUND = "_import_uploader_round"
_IMPORTED = "_import_done"


def _step(number: int, title: str) -> None:
    st.markdown(f"#### {number} · {title}")


def _model_pass(new_rows: list[dict], schedule) -> None:
    """
    Offer one metered model pass over the rows of this upload that are NEW and
    that the rules could not settle -- never duplicates, never on a page load.

    What the model answered is kept for this upload (keyed on the rows), so the
    page's reruns show it instead of asking again. It is overlaid onto
    `new_rows` in place for the preview, and shown row by row in its own table.
    """
    keys = sorted(str(record.get("natural_key")) for record in new_rows)
    state_key = "_model_pass_" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]
    saved = st.session_state.get(state_key)

    if saved is None:
        unresolved = [r for r in new_rows if pd.isna(r.get("budget_category"))]
        if not unresolved:
            st.caption("The rules settled every new row. Nothing needs the model.")
            return
        if not settings.llm_enabled():
            st.caption(f"{len(unresolved)} row(s) need a person. The model is switched off here.")
            return
        clean = [{k: v for k, v in r.items() if k not in _CATEGORIZED_COLUMNS} for r in new_rows]
        plan = model_runs.categorizer_plan(categorize_records(clean, dues=schedule))
        if plan.rows == 0:
            return
        ceiling = plan.reservation_usd(settings.llm_model(), settings.llm_max_output_tokens())
        shell.say(
            f"The rules could not settle **{plan.rows}** new row(s). Haiku can suggest a "
            f"committee for each; anything it is under {AUTO_APPLY_THRESHOLD:.0%} sure of "
            "comes back to you with its suggestion attached.",
            caption=True,
        )
        if not st.button(
            f"Ask Haiku about {plan.rows} row(s) · at most ${ceiling:.3f}",
            key="model_pass_run",
            type="primary",
            disabled=identity.read_only,
        ):
            return
        with st.spinner("Asking Haiku…"):
            model_run, report = repo.run_categorizer(identity.email, clean, dues=schedule)
        if not report.started:
            shell.notify("warning", f"Nothing was sent. {report.refusal or ''}".strip())
            return
        st.session_state[state_key] = {
            "answers": {
                str(record.get("natural_key")): (item.committee_id, item.purpose, item.rule)
                for record, item in zip(clean, model_run.classifications)
            },
            "table": _what_haiku_did(clean, model_run),
            "note": (
                "warning" if report.batches_failed else "success",
                f"Haiku answered {report.saved} of {report.rows_sent} row(s) for "
                f"${report.cost_usd:.4f}."
                + (f" Problems: {'; '.join(report.errors[:2])}" if report.errors else ""),
            ),
        }
        # Redraw without the button, so it cannot be clicked into a second run.
        st.rerun()

    shell.notify(*saved["note"])
    st.markdown("**What Haiku did**")
    shell.dataframe(
        saved["table"],
        column_config={
            "Amount": st.column_config.NumberColumn(format="$%.2f"),
            "Confidence": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
            "Details": st.column_config.TextColumn(width="large"),
            "Haiku's reason": st.column_config.TextColumn(width="large"),
        },
    )
    for record in new_rows:
        found = saved["answers"].get(str(record.get("natural_key")))
        if found and pd.isna(record.get("budget_category")):
            record["budget_category"], record["purpose"], record["match_rule"] = found


def _what_haiku_did(records: list[dict], run) -> pd.DataFrame:
    """One line per row Haiku was asked about: its pick, how sure, and the result."""
    rows = []
    for index, _ in run.residual:
        record = records[index]
        answer = run.llm.classifications.get(index)
        final = run.classifications[index]
        if answer is None:
            pick, confidence, reason = "Couldn't tell", None, ""
            outcome = "Left for you — no suggestion"
        else:
            pick = committee_name(answer.committee_id)
            confidence = round(answer.confidence * 100)
            reason = answer.rule.removeprefix("Model: ").removeprefix("Model")
            outcome = (
                f"Booked to {pick}"
                if final.is_assigned and final.source == "llm"
                else "Left for you — suggestion attached"
            )
        rows.append(
            {
                "Date": record.get("transaction_date"),
                "Amount": record.get("amount"),
                "Details": " ".join(str(record.get("details") or "").split())[:120],
                "Haiku's pick": pick,
                "Confidence": confidence,
                "What happened": outcome,
                "Haiku's reason": reason,
            }
        )
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Import a statement",
    "Upload, let the rules and Haiku sort it, import it, then check it against "
    "each committee's spending profile, all on this page.",
)

done = st.session_state.pop(_IMPORTED, None)
if done:
    shell.notify("success", done)

_step(1, "Upload")
source_name = st.radio("Statement source", list(SOURCES), horizontal=True, key="upload_source")
source = SOURCES[source_name]
st.caption(source["hint"])
uploaded = st.file_uploader(
    f"Upload {source_name} statement",
    type=["csv", "xlsx", "xls"],
    # A new key after each import empties the uploader, so the imported file is
    # not re-read as a wall of duplicates.
    key=f"upload_{source_name}_{st.session_state.get(_UPLOADER_ROUND, 0)}",
)

if uploaded is not None:
    file_name = uploaded.name
    if repo.is_file_uploaded(file_name):
        st.warning(
            f"**{file_name}** has already been uploaded. Re-uploading is safe — "
            "duplicates are rejected by the database — but nothing new will be added."
        )
    if source["filename_hint"] not in file_name.lower():
        st.info(
            f"Filename does not contain '{source['filename_hint']}'. Proceeding anyway — "
            "the parser validates the file's actual shape, not its name."
        )

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

    parsed = source["parser"].parse(raw, source_file=file_name) if raw is not None else None
    if parsed is not None:
        for warning in parsed.warnings:
            st.warning(warning)
    if parsed is not None and not parsed.ok:
        shell.error_state(
            "This file was not imported", "\n\n".join(parsed.errors) or "No usable rows found."
        )
    elif parsed is not None:
        # Rules only. This page reruns on every click, so this must never reach
        # the model: the model pass is the explicit, metered button in step 2.
        schedule = dues.schedule_from_terms(repo.load_terms())
        categorized, run = categorize_frame(parsed.rows, dues=schedule)

        existing = repo.load_transactions()
        staged = categorized.to_dict("records")
        for record in staged:
            if isinstance(record.get("transaction_date"), pd.Timestamp):
                record["transaction_date"] = record["transaction_date"].strftime("%Y-%m-%d")
        dedupe = split_new_and_duplicate(staged, existing.to_dict("records") if not existing.empty else [])

        counts = run.counts_by_source
        columns = st.columns(4)
        columns[0].metric("Rows in the file", f"{run.total:,}")
        columns[1].metric("Already imported", f"{len(dedupe.duplicates):,}")
        columns[2].metric("Settled by the rules", f"{counts['rule'] + counts['scored']:,}")
        columns[3].metric("Left unsettled", f"{counts['none']:,}")

        if dedupe.duplicates:
            with st.expander(f"{len(dedupe.duplicates)} row(s) already imported — skipped"):
                shell.dataframe(
                    pd.DataFrame(dedupe.duplicates)[["transaction_date", "amount", "details"]].rename(
                        columns={"transaction_date": "Date", "amount": "Amount", "details": "Details"}
                    ),
                    column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
                )

        if not dedupe.new:
            st.warning("Every row in this file is already imported. Nothing to do.")
        else:
            _step(2, "Ask Haiku about what the rules left")
            _model_pass(dedupe.new, schedule)

            _step(3, f"Review and import {len(dedupe.new)} new transaction(s)")
            preview = pd.DataFrame(dedupe.new)
            edited = st.data_editor(
                pd.DataFrame(
                    {
                        "Date": preview["transaction_date"],
                        "Amount": preview["amount"],
                        "Details": preview["details"],
                        "Committee": [committee_label(value) for value in preview["budget_category"]],
                        "Purpose": preview["purpose"].fillna(""),
                        "Matched by": preview["match_rule"].fillna(""),
                    }
                ),
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
                f"Import {len(dedupe.new)} transaction(s)",
                type="primary",
                key="confirm_import",
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
                    st.session_state[_IMPORTED] = (
                        f"Imported {receipt.inserted} transaction(s) from {file_name}."
                        + (
                            f" {receipt.duplicates_skipped} rejected as duplicates by the database."
                            if receipt.duplicates_skipped
                            else ""
                        )
                        + " Next: check them against the committee profiles below."
                    )
                    st.session_state[_UPLOADER_ROUND] = st.session_state.get(_UPLOADER_ROUND, 0) + 1
                    st.rerun()

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
_step(4, "Check new charges against the committee profiles")
spend_review_ui.treasurer_panel(repo.load_transactions(), identity, inline=True)

with st.expander("Committee ID reference"):
    reference = pd.DataFrame(committee_reference_rows(), columns=["ID", "Committee"])
    shell.dataframe(reference, height=340)
with st.expander("Previously uploaded files"):
    files = repo.load_uploaded_files()
    if files.empty:
        shell.empty_state("Nothing uploaded yet")
    else:
        shell.dataframe(files.head(25))
