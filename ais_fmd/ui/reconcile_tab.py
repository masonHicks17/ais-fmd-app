"""
Treasury -> Reconcile: upload a file, see how it differs from the app, apply
the ticked differences.

The books, the roster and the calendar arrive by an automatic sync from Google
Drive and Supabase (2026-11-13), so this is the only upload spot left. It
replaced Transactions -> Import, the Roster page's "Upload a membership list"
and the Events page's "Sync from a file".

Three groups per file (domain/file_compare.py): in the file only, in both but
different, in the app only (inside the file's own scope). Each row has an Apply
tick box. New rows start ticked; changes and removals start unticked, because
the synced data is usually the more recent copy. One button applies only what
is ticked.

Other pages open this tab with `open_treasury("Reconcile", source)`.
"""

from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st

from ais_fmd.config.categories import (
    committee_dropdown_options,
    committee_label,
    committee_name,
    committee_reference_rows,
    parse_committee_label,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues
from ais_fmd.domain import events as events_domain
from ais_fmd.domain import file_compare
from ais_fmd.domain import roster as roster_domain
from ais_fmd.domain.terms import default_semester_index, ordered_semesters, term_id_for_semester
from ais_fmd.ui import shell
from ais_fmd.ui import spend_review as spend_review_ui
from ais_fmd.ui import statement_import

TAB_KEY = "treasury_tab"
TAB_LABEL = "Reconcile"
SOURCE_KEY = "reconcile_source"
BANK, ROSTER, SCHEDULE = "Bank statement", "Membership roster", "Event schedule"
SOURCES = (BANK, ROSTER, SCHEDULE)
ROSTER_SEMESTER_KEY = "reconcile_roster_semester"

_FLASH = "_reconcile_flash"
# Bumped after every apply, so the tick tables start fresh against the new
# comparison instead of carrying ticks over by row position.
_ROUND = "_reconcile_round"
APPLY = "Apply"


def open_treasury(tab: str, source: str | None = None, *, semester: str | None = None) -> None:
    """Make the next Treasury run open on `tab` (and, for Reconcile, `source`)."""
    st.session_state[TAB_KEY] = tab
    if source in SOURCES:
        st.session_state[SOURCE_KEY] = source
    if semester:
        st.session_state[ROSTER_SEMESTER_KEY] = semester


# --- Shared pieces -----------------------------------------------------------------

def _editor_key(name: str, data: bytes) -> str:
    digest = hashlib.sha1(data).hexdigest()[:10]
    return f"reconcile_{name}_{digest}_{st.session_state.get(_ROUND, 0)}"


def _metrics(items: list[tuple[str, int, str]]) -> None:
    columns = st.columns(len(items))
    for column, (label, value, help_text) in zip(columns, items):
        shell.metric(column, label, f"{value:,}", currency=False, help=help_text)


def _tick_table(
    frame: pd.DataFrame,
    keys: list[str],
    *,
    name: str,
    data: bytes,
    ticked: bool,
    identity,
    editable: dict | None = None,
    column_config: dict | None = None,
) -> pd.DataFrame:
    """A table with an Apply tick box per row. Returns the edited table (see `_ticked`)."""
    table = frame.copy()
    table.insert(0, APPLY, [ticked] * len(table))
    table["_key"] = keys
    editable = editable or {}
    config = {
        APPLY: st.column_config.CheckboxColumn(APPLY, width="small"),
        "_key": None,
        **(column_config or {}),
        **editable,
    }
    edited = st.data_editor(
        table,
        key=_editor_key(name, data),
        width="stretch",
        hide_index=True,
        disabled=True if identity.read_only else [c for c in table.columns if c != APPLY and c not in editable],
        column_config=config,
    )
    return edited


def _ticked(edited: pd.DataFrame) -> set[str]:
    return {key for key, tick in zip(edited["_key"], edited[APPLY]) if bool(tick)}


def _group_heading(title: str, count: int, help_text: str) -> None:
    shell.heading(title, shell.pill(f"{count:,}", "warn" if count else "muted"), help=help_text)


def _apply_button(count: int, identity, name: str) -> bool:
    return st.button(
        f"Apply {count} ticked change{'s' if count != 1 else ''}",
        type="primary",
        key=f"reconcile_apply_{name}",
        disabled=identity.read_only or count == 0,
        help="Only the ticked rows are applied." if not identity.read_only else "Read-only: you can compare but not apply.",
    )


def _finish(messages: list[tuple[str, str]]) -> None:
    st.session_state[_FLASH] = messages
    st.session_state[_ROUND] = st.session_state.get(_ROUND, 0) + 1
    st.rerun()


def _no_differences(comparison: file_compare.Comparison) -> None:
    if not comparison.differences:
        shell.notify("success", "The file matches the app. Nothing to apply.")


def _committee(value: object) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else committee_name(value)


# --- Bank statement ------------------------------------------------------------------

def _bank(identity) -> None:
    source = statement_import.SOURCE
    uploaded = st.file_uploader(
        f"{statement_import.SOURCE_NAME} statement",
        type=["csv", "xlsx", "xls"],
        help=source["hint"],
        key="reconcile_upload_statement",
    )
    if uploaded is None:
        return
    data = uploaded.getvalue()
    parsed = statement_import.read_statement(uploaded)
    if parsed is None:
        return

    schedule = dues.schedule_from_terms(repo.load_terms())
    records, _run = statement_import.categorized_records(parsed, schedule)
    stored = repo.load_transactions()
    comparison = file_compare.compare_statement(
        records, stored.to_dict("records") if not stored.empty else [], source["account"]
    )
    _metrics([
        ("In the file only", len(comparison.new), "Rows the app does not have yet. Applying imports them."),
        ("In the app only", len(comparison.missing),
         f"Transactions on this account inside the statement's dates ({comparison.scope}) that the file does not contain."),
        ("In both", comparison.matching, "Rows already in the app. Nothing to do."),
    ])
    _no_differences(comparison)

    ticked: set[str] = set()
    edited = None
    new_rows = [difference.file_row for difference in comparison.new]
    if new_rows:
        statement_import.model_pass(new_rows, schedule, identity)
        _group_heading(
            "In the file, not in the app", len(new_rows),
            "Ticked rows are imported with the Committee and Purpose shown; change them here first if "
            "needed. Rows left without a committee go to the Review Queue.",
        )
        edited = _tick_table(
            pd.DataFrame({
                "Date": [r["transaction_date"] for r in new_rows],
                "Amount": [r["amount"] for r in new_rows],
                "Details": [r["details"] for r in new_rows],
                "Committee": [committee_label(r.get("budget_category")) for r in new_rows],
                "Purpose": [r.get("purpose") if isinstance(r.get("purpose"), str) else "" for r in new_rows],
                "Matched by": [r.get("match_rule") if isinstance(r.get("match_rule"), str) else "" for r in new_rows],
            }),
            [difference.key for difference in comparison.new],
            name="statement_new", data=data, ticked=True, identity=identity,
            editable={
                "Committee": st.column_config.SelectboxColumn(options=committee_dropdown_options(), required=False),
                "Purpose": st.column_config.SelectboxColumn(options=purpose_dropdown_options(), required=False),
            },
            column_config={
                "Amount": st.column_config.NumberColumn(format="dollar"),
                "Details": st.column_config.TextColumn(width="large"),
            },
        )
        ticked = _ticked(edited)

    if comparison.missing:
        _group_heading(
            "In the app, not in the file", len(comparison.missing),
            "For checking only: transactions cannot be deleted here. Usually a charge the bank "
            "reversed, or a row edited at the bank after it was imported.",
        )
        missing = [difference.app_row for difference in comparison.missing]
        shell.dataframe(
            pd.DataFrame({
                "Date": [pd.to_datetime(r.get("transaction_date")).strftime("%Y-%m-%d") for r in missing],
                "Amount": [r.get("amount") for r in missing],
                "Details": [r.get("details") for r in missing],
                "Committee": [_committee(r.get("budget_category")) for r in missing],
            }),
            column_config={
                "Amount": st.column_config.NumberColumn(format="dollar"),
                "Details": st.column_config.TextColumn(width="large"),
            },
        )

    if new_rows and _apply_button(len(ticked), identity, "statement"):
        by_key = {difference.key: difference.file_row for difference in comparison.new}
        final = []
        for position, key in enumerate(edited["_key"]):
            if key not in ticked:
                continue
            record, row = by_key[key], edited.iloc[position]
            final.append({
                "transaction_date": record["transaction_date"],
                "amount": record["amount"],
                "details": record["details"],
                "account": record["account"],
                "budget_category": parse_committee_label(row["Committee"]),
                "purpose": (row["Purpose"] or "").strip() or None,
                "natural_key": record["natural_key"],
            })
        receipt = repo.insert_transactions(final, uploaded.name, identity.email)
        if not receipt.ok:
            shell.error_state("Import failed — nothing was written", receipt.error or "")
        else:
            note = f"Imported {receipt.inserted} transaction(s) from {uploaded.name}."
            if receipt.duplicates_skipped:
                note += f" {receipt.duplicates_skipped} were already there."
            _finish([("success", note)])

    spend_review_ui.treasurer_panel(repo.load_transactions(), identity)
    with st.expander("Committee ID reference"):
        shell.dataframe(pd.DataFrame(committee_reference_rows(), columns=["ID", "Committee"]), height=340)
    with st.expander("Previously uploaded files"):
        files = repo.load_uploaded_files()
        if files.empty:
            shell.empty_state("Nothing uploaded yet")
        else:
            shell.dataframe(files.head(25))


# --- Membership roster ----------------------------------------------------------------

def _roster(identity) -> None:
    terms = repo.load_terms()
    semesters = ordered_semesters(terms)
    if not semesters:
        shell.empty_state("No terms defined", "Add one on the Terms tab first.")
        return
    if st.session_state.get(ROSTER_SEMESTER_KEY) not in semesters:
        st.session_state[ROSTER_SEMESTER_KEY] = semesters[
            default_semester_index(repo.load_transactions(), terms)
        ]
    selected = st.selectbox("Semester", semesters, key=ROSTER_SEMESTER_KEY)
    term_id = term_id_for_semester(terms, selected) or ""
    uploaded = st.file_uploader(
        "Membership list",
        type=["csv", "xlsx", "xls"],
        key="reconcile_upload_roster",
        help="A CSV or Excel export of the membership sheet, with a name column (or first and last "
        "name columns). Only the name, preferred name, UFID, UF email and the \"I certify I paid\" "
        "answer are read; phone, gender and survey columns are ignored.",
    )
    if uploaded is None:
        return
    data = uploaded.getvalue()
    try:
        # As text, blanks as "": read as numbers, a UFID came back as "1111.0"
        # and an empty cell as the string "nan".
        options = {"dtype": str, "keep_default_na": False}
        raw = (pd.read_csv(uploaded, **options) if uploaded.name.lower().endswith(".csv")
               else pd.read_excel(uploaded, **options))
    except Exception as exc:  # noqa: BLE001 - a bad file must not crash the page
        shell.error_state("Could not read that file", f"{type(exc).__name__}: {exc}")
        return
    parsed = roster_domain.parse_roster(raw)
    for warning in parsed.warnings:
        shell.notify("warning", warning)
    if not parsed.ok:
        shell.error_state("Could not read this roster", "\n\n".join(parsed.errors))
        return

    stored = repo.load_members(term_id)
    stored_rows = stored.to_dict("records") if stored is not None and not stored.empty else []
    comparison = file_compare.compare_roster(
        parsed.members, stored_rows, selected,
        compare_claims=file_compare.roster_has_claim_column(raw.columns),
    )
    _metrics([
        ("In the file only", len(comparison.new), f"People in the file who are not on the {selected} roster."),
        ("Different", len(comparison.changed), "On both, but a preferred name, email, UFID or paid answer differs."),
        ("In the app only", len(comparison.missing), f"On the {selected} roster but not in the file."),
        ("Same", comparison.matching, "On both, nothing differs."),
    ])
    _no_differences(comparison)

    ticked: set[str] = set()
    if comparison.new:
        _group_heading("In the file, not in the app", len(comparison.new), "Ticked people are added to the roster.")
        rows = [difference.file_row for difference in comparison.new]
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Name": [r["full_name"] for r in rows],
                "Preferred name": [r["preferred_name"] for r in rows],
                "Email": [r["email"] for r in rows],
                "UFID": [r["ufid"] for r in rows],
                "Says they paid": [r["claims_paid"] for r in rows],
            }),
            [difference.key for difference in comparison.new],
            name="roster_new", data=data, ticked=True, identity=identity,
        ))
    if comparison.changed:
        _group_heading("In both, but different", len(comparison.changed),
                       "Ticked rows take the file's value. A blank in the file never clears a value.")
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Name": [d.app_row.get("full_name") for d in comparison.changed],
                "Changes (app → file)": [d.change_text for d in comparison.changed],
            }),
            [difference.key for difference in comparison.changed],
            name="roster_changed", data=data, ticked=False, identity=identity,
            column_config={"Changes (app → file)": st.column_config.TextColumn(width="large")},
        ))
    if comparison.missing:
        _group_heading("In the app, not in the file", len(comparison.missing),
                       f"Ticked people are taken off the {selected} roster.")
        rows = [difference.app_row for difference in comparison.missing]
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Name": [r.get("full_name") for r in rows],
                "Email": [file_compare.as_text(r.get("email")) for r in rows],
                "UFID": [file_compare.as_text(r.get("ufid")) for r in rows],
            }),
            [difference.key for difference in comparison.missing],
            name="roster_missing", data=data, ticked=False, identity=identity,
        ))

    if comparison.differences and _apply_button(len(ticked), identity, "roster"):
        merged = file_compare.merged_roster(stored_rows, comparison, ticked)
        result = repo.replace_members(term_id, merged, uploaded.name, identity.email)
        if not result.ok:
            shell.error_state("Could not save the roster", result.error or "")
        else:
            repo.invalidate()
            _finish([("success", f"Applied {len(ticked)} change(s). The {selected} roster has {len(merged)} people.")])


# --- Event schedule ------------------------------------------------------------------

def _time(row: dict) -> str:
    start, end = file_compare.as_text(row.get("start_time")), file_compare.as_text(row.get("end_time"))
    return f"{start}–{end}" if start and end else start


def _event_changes(difference: file_compare.Difference) -> str:
    """The change text, with committee ids shown as names."""
    parts = []
    for change in difference.changes:
        if change.field == "committee_id":
            change = file_compare.FieldChange(
                change.field, change.label, _committee(change.old) or None, _committee(change.new) or None
            )
        parts.append(change.describe())
    return "; ".join(parts)


def _events(identity) -> None:
    uploaded = st.file_uploader(
        "Event schedule",
        type=["xlsx", "ics"],
        key="reconcile_upload_events",
        help="The AIS Event Schedule workbook (.xlsx, one sheet per semester) or a Google Calendar "
        "export (.ics). Events are matched on their day and title.",
    )
    if uploaded is None:
        return
    data = uploaded.getvalue()
    try:
        parsed = events_domain.parse_upload(data, uploaded.name, repo.load_terms())
    except Exception as exc:  # noqa: BLE001 - a bad file must not crash the page
        shell.error_state("Could not read that file", f"{type(exc).__name__}: {exc}")
        return
    for note in parsed.notes:
        shell.notify("warning", note)
    skipped = sum(parsed.skipped_outside_term.values())
    if skipped:
        st.caption(f"{skipped} row(s) dated outside their sheet's semester were skipped.")
    if not parsed.events:
        shell.empty_state("No events found in this file", "A row needs a date and an event title.")
        return

    stored = repo.load_events()
    stored_rows = stored.to_dict("records") if stored is not None and not stored.empty else []
    comparison = file_compare.compare_events([event.as_row() for event in parsed.events], stored_rows)
    _metrics([
        ("In the file only", len(comparison.new), "Events the app does not have yet."),
        ("Different", len(comparison.changed), "Same day and title, but the time, place, owner or another detail differs."),
        ("In the app only", len(comparison.missing),
         f"Events between {comparison.scope} that the file does not have. Events added in the app are not counted."),
        ("Same", comparison.matching, "In both, nothing differs."),
    ])
    _no_differences(comparison)

    ticked: set[str] = set()
    if comparison.new:
        _group_heading("In the file, not in the app", len(comparison.new), "Ticked events are added to the calendar.")
        rows = [difference.file_row for difference in comparison.new]
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Date": [r["event_date"] for r in rows],
                "Title": [r["title"] for r in rows],
                "Time": [_time(r) for r in rows],
                "Location": [r.get("location") or "" for r in rows],
                "Committee": [_committee(r.get("committee_id")) for r in rows],
            }),
            [difference.key for difference in comparison.new],
            name="events_new", data=data, ticked=True, identity=identity,
        ))
    if comparison.changed:
        _group_heading("In both, but different", len(comparison.changed),
                       "Ticked events take the file's details. Linked purchases and attendance are kept.")
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Date": [d.file_row["event_date"] for d in comparison.changed],
                "Title": [d.app_row.get("title") for d in comparison.changed],
                "Changes (app → file)": [_event_changes(d) for d in comparison.changed],
                "Note": [d.note for d in comparison.changed],
            }),
            [difference.key for difference in comparison.changed],
            name="events_changed", data=data, ticked=False, identity=identity,
            column_config={"Changes (app → file)": st.column_config.TextColumn(width="large")},
        ))
    if comparison.missing:
        _group_heading("In the app, not in the file", len(comparison.missing),
                       "Ticked events are removed. An event with purchases linked to it is refused; "
                       "move or unlink them on Events first.")
        rows = [difference.app_row for difference in comparison.missing]
        ticked |= _ticked(_tick_table(
            pd.DataFrame({
                "Date": [str(r.get("event_date"))[:10] for r in rows],
                "Title": [r.get("title") for r in rows],
                "From": [file_compare.as_text(r.get("source")).split(": ", 1)[-1] for r in rows],
                "Note": [d.note for d in comparison.missing],
            }),
            [difference.key for difference in comparison.missing],
            name="events_missing", data=data, ticked=False, identity=identity,
        ))
    if comparison.kept:
        with st.expander(f"{len(comparison.kept)} event(s) added in the app in these dates — never removed here"):
            shell.dataframe(pd.DataFrame({
                "Date": [str(d.app_row.get("event_date"))[:10] for d in comparison.kept],
                "Title": [d.app_row.get("title") for d in comparison.kept],
            }))

    if comparison.differences and _apply_button(len(ticked), identity, "events"):
        messages: list[tuple[str, str]] = []
        # A blank in the file keeps what the app holds (file_compare.filled_from_app).
        upserts = [file_compare.filled_from_app(d.file_row, d.app_row)
                   for d in (*comparison.new, *comparison.changed) if d.key in ticked]
        if upserts:
            result = repo.sync_events(upserts, identity.email)
            if result.error:
                messages.append(("warning", f"Could not save the events: {result.error}"))
            else:
                messages.append(("success", f"Added {result.updated} and updated {result.unchanged} event(s)."))
        removed = 0
        for difference in comparison.missing:
            if difference.key not in ticked:
                continue
            result = repo.delete_event(int(difference.app_row["event_id"]), identity.email)
            if result.error:
                messages.append(("warning", f"{difference.app_row.get('title')} was not removed: {result.error}"))
            else:
                removed += 1
        if removed:
            messages.append(("success", f"Removed {removed} event(s)."))
        _finish(messages)


# --- The tab ------------------------------------------------------------------------

_RENDER = {BANK: _bank, ROSTER: _roster, SCHEDULE: _events}


def render(identity) -> None:
    for kind, text in st.session_state.pop(_FLASH, None) or []:
        shell.notify(kind, text)
    if st.session_state.get(SOURCE_KEY) not in SOURCES:
        st.session_state[SOURCE_KEY] = BANK
    shell.heading(
        "Check a file against the app",
        help="Data arrives by the automatic sync. Upload a file here to check it: the app lists what "
        "is only in the file, what differs, and what is only in the app, and applies only the rows "
        "you tick. New rows start ticked; changes and removals start unticked.",
    )
    source = st.segmented_control(
        "Kind of file", SOURCES, key=SOURCE_KEY, required=True, label_visibility="collapsed"
    )
    _RENDER.get(source, _bank)(identity)
