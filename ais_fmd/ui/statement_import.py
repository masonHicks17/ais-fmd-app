"""
Reading a bank statement and the optional Haiku pass over it.

Moved out of views/ImportStatement.py (2026-11-13) when the Import view was
folded into Treasury -> Reconcile: data now arrives by an automatic sync, and an
uploaded statement is compared with the ledger rather than imported wholesale.
The fixes the Import page documented travel with it:

  F3  deduplication keyed on date+amount+details+account with an occurrence
      ordinal, enforced by a unique index rather than a pandas filter
  F4  transactions and the uploaded-files marker commit together or not at all
  F5  statement shape is validated before parsing; unreadable rows are reported
      rather than imported as $0.00
"""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from ais_fmd import settings
from ais_fmd.config.categories import (
    ACCOUNT_WELLS_FARGO,
    committee_dropdown_options,
    committee_label,
    committee_name,
    parse_committee_label,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import model_runs
from ais_fmd.domain.categorize.pipeline import AUTO_APPLY_THRESHOLD, categorize_frame, categorize_records
from ais_fmd.domain.parsers import wells_fargo
from ais_fmd.ui import shell

# Wells Fargo checking only. Venmo was retired in April 2026 and treasury will
# not use it again (2026-11-03), so it is no longer offered; its parser stays
# for the history already imported from it.
SOURCE_NAME = "Wells Fargo checking"
SOURCE = {
    "parser": wells_fargo,
    "account": ACCOUNT_WELLS_FARGO,
    "hint": "Wells Fargo checking export (CSV or Excel).",
    "filename_hint": "checking",
}
_CATEGORIZED_COLUMNS = ("budget_category", "purpose", "match_rule", "match_confidence", "match_source")


def read_statement(uploaded):
    """The parsed statement, or None after showing why it could not be read."""
    try:
        if uploaded.name.lower().endswith(".csv"):
            raw = pd.read_csv(uploaded, header=None)
        else:
            raw = pd.read_excel(uploaded, header=None)
    except Exception as exc:  # noqa: BLE001 - a bad file must not crash the page
        shell.error_state("Could not read the file", f"{type(exc).__name__}: {exc}")
        return None
    parsed = SOURCE["parser"].parse(raw, source_file=uploaded.name)
    for warning in parsed.warnings:
        shell.notify("warning", warning)
    if not parsed.ok:
        shell.error_state("Could not read this statement", "\n\n".join(parsed.errors) or "No usable rows found.")
        return None
    return parsed


def categorized_records(parsed, schedule) -> tuple[list[dict], object]:
    """
    The rules only (free). The page reruns on every click, so this must never
    reach the model: the model pass is the explicit, metered button below.
    """
    categorized, run = categorize_frame(parsed.rows, dues=schedule)
    staged = categorized.to_dict("records")
    for record in staged:
        if isinstance(record.get("transaction_date"), pd.Timestamp):
            record["transaction_date"] = record["transaction_date"].strftime("%Y-%m-%d")
    return staged, run


def model_pass(new_rows: list[dict], schedule, identity) -> None:
    """
    Offer one metered model pass over the rows of this upload that are NEW and
    that the rules could not settle -- never duplicates, never on a page load.

    What the model answered is kept for this upload (keyed on the rows), so the
    page's reruns show it instead of asking again. It is overlaid onto
    `new_rows` in place for the table below, and shown row by row in its own table.
    """
    keys = sorted(str(record.get("natural_key")) for record in new_rows)
    state_key = "_model_pass_" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]
    saved = st.session_state.get(state_key)

    if saved is None:
        unresolved = [r for r in new_rows if pd.isna(r.get("budget_category"))]
        if not unresolved:
            return
        if not settings.llm_enabled():
            st.caption(f"{len(unresolved)} new row(s) need a committee. The model is switched off here.")
            return
        clean = [{k: v for k, v in r.items() if k not in _CATEGORIZED_COLUMNS} for r in new_rows]
        plan = model_runs.categorizer_plan(categorize_records(clean, dues=schedule))
        if plan.rows == 0:
            return
        ceiling = plan.reservation_usd(settings.llm_model(), settings.llm_max_output_tokens())
        if not st.button(
            f"Ask Haiku about {plan.rows} row(s) · at most ${ceiling:.3f}",
            key="model_pass_run",
            disabled=identity.read_only,
            help=(
                f"The rules could not settle {plan.rows} new row(s). Haiku suggests a committee for "
                f"each; anything it is under {AUTO_APPLY_THRESHOLD:.0%} sure of comes back to you "
                "with its suggestion attached."
            ),
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
            "table": what_haiku_did(clean, model_run),
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
    shell.heading(
        "What Haiku did",
        help="Change Book as to correct or accept Haiku's pick; it carries into the table "
        "below. Leave it blank to send the row to the Review Queue.",
    )
    table = saved["table"]
    edited = st.data_editor(
        table,
        key=f"{state_key}_book_as",
        width="stretch",
        hide_index=True,
        column_order=[column for column in table.columns if column != "natural_key"],
        disabled=True if identity.read_only else [c for c in table.columns if c != "Book as"],
        column_config={
            "Amount": st.column_config.NumberColumn(format="dollar"),
            "Confidence": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
            "Details": st.column_config.TextColumn(width="large"),
            "Book as": st.column_config.SelectboxColumn(
                options=committee_dropdown_options(), required=False
            ),
            "Haiku's reason": st.column_config.TextColumn(width="large"),
        },
    )
    model_runs.apply_categorizer_choices(
        new_rows,
        {key: parse_committee_label(choice) for key, choice in zip(table["natural_key"], edited["Book as"])},
        saved["answers"],
    )


def what_haiku_did(records: list[dict], run) -> pd.DataFrame:
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
                # Editable: starts as what was booked, blank when Haiku was unsure.
                "Book as": committee_label(final.committee_id) if final.is_assigned else "",
                "What happened": outcome,
                "Haiku's reason": reason,
                "natural_key": str(record.get("natural_key")),
            }
        )
    return pd.DataFrame(rows)
