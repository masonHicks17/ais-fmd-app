"""
Import a statement, start to finish, on one page, as four stages:

  1. Import      -- upload; parse; set aside rows already in the books.
  2. Categorize  -- the rules (free), then one optional, metered Haiku run.
  3. Review      -- settle what is left, check Haiku's picks, save to the books.
  4. AI check    -- Haiku compares the new charges with each committee's profile.

A status bar across the top shows where the statement is.

PRESENTATION ONLY (2026-11-12 overhaul). Parsing, the rules, the Haiku run,
deduplication, what gets saved and the spend review are exactly the functions
this page called before; only the layout and the way choices are collected
changed. Rows nobody chose a committee for still go to Needs attention (the
Review Queue) on save, as before.

The fixes the old Treasury page documented travel with it:
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

from ais_fmd import auth, settings
from ais_fmd.config.categories import (
    ACCOUNT_WELLS_FARGO,
    committee_name,
    committee_reference_rows,
    purpose_dropdown_options,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues, model_runs
from ais_fmd.domain.categorize.pipeline import AUTO_APPLY_THRESHOLD, categorize_frame, categorize_records
from ais_fmd.domain.categorize.predicates import COMMITTEE_PURPOSE
from ais_fmd.domain.dedupe import split_new_and_duplicate
from ais_fmd.domain.parsers import wells_fargo
from ais_fmd.ui import inbox, shell
from ais_fmd.ui import spend_review as spend_review_ui
from ais_fmd.ui import txn_display as show

identity = auth.require(auth.Role.TREASURER)

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
_UPLOADER_ROUND = "_import_uploader_round"
_IMPORTED = "_import_done"
_HUB_KEY = "transactions_hub"
NEEDS_ATTENTION = "Needs attention"

NAMES = [name for _, name in committee_reference_rows()]
ID_BY_NAME = {name: cid for cid, name in committee_reference_rows()}


def _id(name: object) -> int | None:
    return ID_BY_NAME.get(str(name)) if name else None


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or value == ""


def _go_to_needs_attention() -> None:
    st.session_state[_HUB_KEY] = NEEDS_ATTENTION


def _upload_another() -> None:
    st.session_state.pop(_IMPORTED, None)
    st.session_state[_UPLOADER_ROUND] = st.session_state.get(_UPLOADER_ROUND, 0) + 1


# --- Stage 1: your statements -------------------------------------------------------


def _statement_rows(transactions: pd.DataFrame) -> list[dict]:
    """Progress of each statement already in the books, read from what is stored."""
    files = repo.load_uploaded_files()
    if files.empty or "file_name" not in files.columns or "source_file" not in transactions.columns:
        return []
    try:
        reviews = repo.load_spend_reviews()
    except Exception:  # noqa: BLE001 - migration 013 not run: no AI check column to show
        reviews = None
    rows = []
    for _, file in files.head(6).iterrows():
        mine = transactions[transactions["source_file"] == file["file_name"]]
        total = len(mine)
        blank = int(mine["budget_category"].isna().sum()) if total else 0
        checked = flags_open = 0
        if reviews is not None and not reviews.empty and total:
            ids = set(pd.to_numeric(mine["transactionid"], errors="coerce").dropna().astype(int))
            theirs = reviews[pd.to_numeric(reviews["transaction_id"], errors="coerce").isin(ids)]
            checked = len(theirs)
            flags_open = int((theirs["status"] == "open").sum())
        rows.append(
            {
                "name": str(file["file_name"]),
                "when": show.day(file.get("uploaded_at")),
                "total": total,
                "blank": blank,
                "checked": checked,
                "flags_open": flags_open,
                "ai": reviews is not None,
            }
        )
    return rows


def _statements_table(rows: list[dict]) -> None:
    def cell(fill: float, text: str, *, done: bool, needs: bool = False) -> str:
        bar_colour = "#111111" if done else "#D6400E"
        inner = f'<i style="display:block;height:6px;width:{fill * 100:.0f}%;background:{bar_colour}"></i>'
        style = "font-weight:700;color:#A8320B" if needs else ("" if done else "color:#55565A")
        return (
            '<span style="display:flex;flex-direction:column;gap:4px">'
            '<span style="display:block;height:6px;border-radius:999px;background:#ECECE9;overflow:hidden">'
            f"{inner}</span><span style=\"{style}\">{text}</span></span>"
        )

    head = (
        '<span></span><span style="font-size:12px;font-weight:600;color:#55565A">Imported &amp; categorized</span>'
        '<span style="font-size:12px;font-weight:600;color:#55565A">Review</span>'
        '<span style="font-size:12px;font-weight:600;color:#55565A">AI check</span>'
    )
    body = []
    for row in rows:
        total = max(row["total"], 1)
        review = (
            cell(1, "Done", done=True)
            if row["blank"] == 0
            else cell(1 - row["blank"] / total, f"{row['blank']} still need you", done=False, needs=True)
        )
        if not row["ai"]:
            ai = cell(0, "Not available yet", done=False)
        elif row["checked"] == 0:
            ai = cell(0, "Not run", done=False)
        elif row["flags_open"]:
            ai = cell(0.5, f"{row['flags_open']} flag(s) open", done=False, needs=True)
        else:
            ai = cell(1, f"{row['checked']} checked · no flags open", done=True)
        body.append(
            f'<span style="display:flex;flex-direction:column"><b>{row["name"]}</b>'
            f'<span style="font-size:13px;color:#55565A">{row["when"]} · {row["total"]} rows</span></span>'
            f"{cell(1, 'Done', done=True)}{review}{ai}"
        )
    shell.html(
        '<div style="overflow-x:auto"><div style="min-width:620px;display:grid;'
        "grid-template-columns:minmax(180px,1.2fr) repeat(3,minmax(0,1fr));gap:10px 16px;"
        f'align-items:center;font-size:14px">{head}{"".join(body)}</div></div>'
    )


# --- Stage 2: Haiku -----------------------------------------------------------------


def _haiku_state_key(new_rows: list[dict]) -> str:
    keys = sorted(str(record.get("natural_key")) for record in new_rows)
    return "_model_pass_" + hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]


def _run_haiku(new_rows: list[dict], schedule, state_key: str, plan_rows: int) -> None:
    """One metered run over the rows the rules left. Unchanged from the old step 2."""
    clean = [{k: v for k, v in r.items() if k not in _CATEGORIZED_COLUMNS} for r in new_rows]
    with st.spinner(f"Asking Haiku about {plan_rows} row(s)…"):
        model_run, report = repo.run_categorizer(identity.email, clean, dues=schedule)
    if not report.started:
        shell.notify("warning", f"Nothing was sent. {report.refusal or ''}".strip())
        return
    suggestions = {}
    for index, _ in model_run.residual:
        answer = model_run.llm.classifications.get(index)
        if answer is not None:
            reason = answer.rule.removeprefix("Model: ").removeprefix("Model")
            suggestions[str(clean[index].get("natural_key"))] = (
                answer.committee_id, round(answer.confidence * 100), reason,
            )
    st.session_state[state_key] = {
        "answers": {
            str(record.get("natural_key")): (item.committee_id, item.purpose, item.rule)
            for record, item in zip(clean, model_run.classifications)
        },
        "table": _what_haiku_did(clean, model_run),
        "suggestions": suggestions,
        "rows": report.rows_sent,
        "cost": report.cost_usd,
        "note": (
            "warning" if report.batches_failed else "success",
            f"Haiku answered {report.saved} of {report.rows_sent} row(s) for "
            f"${report.cost_usd:.4f}."
            + (f" Problems: {'; '.join(report.errors[:2])}" if report.errors else ""),
        ),
    }
    st.rerun()


def _what_haiku_did(records: list[dict], run) -> pd.DataFrame:
    """One line per row Haiku was asked about: its pick, how sure, and the result."""
    rows = []
    for index, _ in run.residual:
        record = records[index]
        answer = run.llm.classifications.get(index)
        final = run.classifications[index]
        if answer is None:
            pick, confidence, reason = "Couldn't tell", None, ""
        else:
            pick = committee_name(answer.committee_id)
            confidence = round(answer.confidence * 100)
            reason = answer.rule.removeprefix("Model: ").removeprefix("Model")
        booked = final.is_assigned and final.source == "llm"
        rows.append(
            {
                "Date": show.day(record.get("transaction_date")),
                "Amount": record.get("amount"),
                "Merchant": show.merchant(record.get("details")),
                "Card": show.card_line(record.get("details"), record.get("transaction_date"), record.get("amount")),
                "Haiku's pick": pick,
                "Confidence": confidence,
                # Editable: starts as what was booked, blank when Haiku was unsure.
                "Book as": committee_name(final.committee_id) if booked else "",
                "Haiku's reason": reason,
                "natural_key": str(record.get("natural_key")),
            }
        )
    return pd.DataFrame(rows)


# --- Stage 3: review -------------------------------------------------------------------


def _decisions(ukey: str) -> dict:
    return st.session_state.setdefault(f"_import_decisions_{ukey}", {})


def _purposes(ukey: str) -> dict:
    return st.session_state.setdefault(f"_import_purposes_{ukey}", {})


def _choose_group(ukey: str, natural_keys: list[str], widget_key: str) -> None:
    committee = _id(st.session_state.pop(widget_key, None))
    if committee is not None:
        _choose_many(ukey, natural_keys, committee)


def _choose(ukey: str, natural_key: str, widget_key: str) -> None:
    committee = _id(st.session_state.get(widget_key))
    if committee is not None:
        _decisions(ukey)[natural_key] = committee
        _purposes(ukey).pop(natural_key, None)


def _choose_many(ukey: str, natural_keys: list[str], committee: int) -> None:
    for natural_key in natural_keys:
        _decisions(ukey)[natural_key] = committee
        _purposes(ukey).pop(natural_key, None)


def _undo(ukey: str, natural_key: str, widget_keys: list[str]) -> None:
    _decisions(ukey).pop(natural_key, None)
    _purposes(ukey).pop(natural_key, None)
    for key in widget_keys:
        st.session_state.pop(key, None)


def _set_purpose(ukey: str, natural_key: str, widget_key: str) -> None:
    _purposes(ukey)[natural_key] = st.session_state.get(widget_key) or ""


def _suggestions(record: dict, haiku: dict, proposals: dict) -> tuple[list[int], str, str]:
    """
    Up to three committees to offer, best first, with a tag and the reason.

    Only what the categorizer already produced: Haiku's pick, then the rules'
    scored proposal and its runners-up. Nothing is invented to fill the row.
    """
    key = str(record.get("natural_key"))
    options: list[int] = []
    tag, why = "No suggestion", "No rule or rostered card matched this one, and nothing else pointed anywhere."
    if key in haiku and haiku[key][0] is not None:
        committee, confidence, reason = haiku[key]
        options.append(int(committee))
        tag, why = f"Haiku {confidence}%", reason or "Haiku's best guess."
    proposal = proposals.get(key)
    if proposal is not None and proposal.winner is not None:
        if not options:
            badge = "contested" if proposal.conflicting() else "weak evidence"
            tag, why = f"{proposal.confidence:.0%} · {badge}", proposal.explain()
        for committee, _score in [(proposal.winner, 0), *proposal.conflicting()]:
            if committee not in options:
                options.append(int(committee))
    return options[:3], tag, why


def _needs_row(record: dict, ukey: str, options: list[int], tag: str, why: str, group: list[str]) -> None:
    natural_key = str(record.get("natural_key"))
    digest = hashlib.sha256(natural_key.encode()).hexdigest()[:10]
    pick_key, other_key, purpose_key = f"imp_pick_{digest}", f"imp_other_{digest}", f"imp_purpose_{digest}"
    decided = _decisions(ukey).get(natural_key)

    if decided is not None:
        with st.container(border=True):
            left, middle, right = st.columns([5, 3, 1], vertical_alignment="center")
            with left:
                inbox.booked_line(record, committee_name(decided))
            purposes = purpose_dropdown_options()
            current = _purposes(ukey).get(natural_key, COMMITTEE_PURPOSE.get(decided, ""))
            middle.selectbox(
                "Purpose",
                purposes,
                index=purposes.index(current) if current in purposes else 0,
                key=purpose_key, format_func=lambda p: p or "None",
                on_change=_set_purpose,
                args=(ukey, natural_key, purpose_key),
                disabled=identity.read_only,
            )
            right.button(
                "Undo", key=f"imp_undo_{digest}", on_click=_undo,
                args=(ukey, natural_key, [pick_key, other_key, purpose_key]), disabled=identity.read_only,
            )
        return

    with st.container(border=True):
        if len(group) > 1:
            strip_left, strip_right = st.columns([3, 2], vertical_alignment="center")
            strip_left.caption(
                f"{len(group)} {show.merchant(record.get('details'))} charges on the same card in this "
                "upload. Booking them together remembers nothing for next time."
            )
            if options:
                strip_right.button(
                    f"Book all {len(group)} as {committee_name(options[0])}",
                    key=f"imp_group_{digest}", on_click=_choose_many,
                    args=(ukey, group, options[0]), disabled=identity.read_only,
                )
            else:
                group_key = f"imp_groupsel_{digest}"
                strip_right.selectbox(
                    f"Book all {len(group)} as", NAMES, index=None, placeholder="Choose a committee",
                    key=group_key, on_change=_choose_group, args=(ukey, group, group_key),
                    disabled=identity.read_only,
                )
        detail, choice = st.columns([3, 2])
        with detail:
            inbox.row_header(record, tag=tag, why=why)
            inbox.bank_line(record.get("details"))
        with choice:
            if options:
                st.pills(
                    "Book as",
                    [committee_name(option) for option in options],
                    key=pick_key,
                    on_change=_choose,
                    args=(ukey, natural_key, pick_key),
                    disabled=identity.read_only,
                )
            st.selectbox(
                "Other committee" if options else "Book as",
                NAMES,
                index=None,
                placeholder="Choose a committee",
                key=other_key,
                on_change=_choose,
                args=(ukey, natural_key, other_key),
                disabled=identity.read_only,
            )
            st.caption("Purpose fills in from the committee you pick; you can change it after.")


def _final_rows(new_rows: list[dict], ukey: str) -> list[dict]:
    """What would be saved now: each row's committee and purpose after every choice."""
    decisions, purposes = _decisions(ukey), _purposes(ukey)
    final = []
    for record in new_rows:
        key = str(record.get("natural_key"))
        committee, purpose = record.get("budget_category"), record.get("purpose")
        if key in decisions:
            committee = decisions[key]
            purpose = purposes.get(key, COMMITTEE_PURPOSE.get(committee, ""))
        committee = None if _blank(committee) else int(committee)
        purpose = None if _blank(purpose) else str(purpose).strip() or None
        final.append(
            {
                "transaction_date": record["transaction_date"],
                "amount": record["amount"],
                "details": record["details"],
                "account": record["account"],
                "budget_category": committee,
                "purpose": purpose,
                "natural_key": record["natural_key"],
            }
        )
    return final


def _glance(final: list[dict]) -> None:
    money_in = sum(r["amount"] for r in final if r["amount"] > 0)
    money_out = sum(r["amount"] for r in final if r["amount"] < 0)
    unbooked = sum(r["amount"] for r in final if r["budget_category"] is None)
    totals: dict[str, float] = {}
    for row in final:
        if row["budget_category"] is not None:
            name = committee_name(row["budget_category"])
            totals[name] = totals.get(name, 0.0) + float(row["amount"])
    with shell.card("glance", order=3):
        shell.heading(
            "This statement at a glance",
            '<span style="font-size:13px;color:#55565A;font-weight:400">'
            "A total that looks wrong usually means a row is booked to the wrong committee.</span>",
        )
        columns = st.columns(3)
        columns[0].metric("Money in", show.signed_money(money_in))
        columns[1].metric("Money out", show.signed_money(money_out))
        columns[2].metric("Not booked yet", show.signed_money(unbooked) if unbooked else "$0.00")
        inbox.glance(list(totals.items()))


# ----------------------------------------------------------------------------------------

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Import & review",
    "Upload a statement, settle what the rules and Haiku couldn't, save it, then let the AI check look it over.",
)
inbox.styles()
status_slot = st.container()

done = st.session_state.get(_IMPORTED)

# --- Stage 4: saved; the AI check --------------------------------------------------------
if isinstance(done, dict):
    transactions = repo.load_transactions()
    if "source_file" in transactions.columns:
        mine = transactions[transactions["source_file"] == done["file"]]
    else:
        mine = transactions.iloc[0:0]
    left_over = int(mine["budget_category"].isna().sum()) if not mine.empty else done.get("left", 0)
    try:
        reviews = repo.load_spend_reviews()
    except Exception:  # noqa: BLE001 - migration 013 not run
        reviews = None
    checked = flags_open = 0
    if reviews is not None and not reviews.empty and not mine.empty:
        ids = set(pd.to_numeric(mine["transactionid"], errors="coerce").dropna().astype(int))
        theirs = reviews[pd.to_numeric(reviews["transaction_id"], errors="coerce").isin(ids)]
        checked, flags_open = len(theirs), int((theirs["status"] == "open").sum())
    if reviews is None:
        ai_stage = inbox.Stage("AI check", "upcoming", "Not available here yet")
    elif checked == 0:
        ai_stage = inbox.Stage(
            "AI check", "active", "Ready to run" if settings.llm_enabled() else "Haiku is switched off here"
        )
    elif flags_open:
        ai_stage = inbox.Stage("AI check", "active", f"{flags_open} flag(s) to look at", 0.5, needs_you=True)
    else:
        ai_stage = inbox.Stage("AI check", "done", f"{checked} checked · all clear")
    with status_slot:
        inbox.status_bar(
            [
                inbox.Stage("Import", "done", f"{done['inserted']} new · {done['duplicates']} already in"),
                inbox.Stage(
                    "Categorize", "done",
                    f"{done['by_rules']} by rules · "
                    + (f"{done['by_haiku']} by Haiku" if done.get("haiku_used") else "Haiku not used"),
                ),
                inbox.Stage(
                    "Review", "done",
                    f"Saved · {left_over} in Needs attention" if left_over else "Saved · nothing left",
                ),
                ai_stage,
            ],
            title=done["file"],
            right="Finished" if ai_stage.state == "done" else "Stage 4 of 4",
        )
    shell.notify("success", done["message"])

    with shell.card("aicheck", order=2):
        shell.heading("AI check")
        spend_review_ui.treasurer_panel(transactions, identity, inline=True)

    actions = st.columns([2, 2, 3])
    if left_over:
        actions[0].button(
            f"{left_over} waiting in Needs attention", key="import_to_queue", on_click=_go_to_needs_attention
        )
    actions[1].button("Upload another statement", key="import_another", on_click=_upload_another)
    st.stop()

# --- Stage 1: import ------------------------------------------------------------------------
with shell.card("upload", order=1):
    uploaded = st.file_uploader(
        f"Drop a {SOURCE_NAME} export here",
        type=["csv", "xlsx", "xls"],
        help=f"{SOURCE['hint']} Rows already in the books are skipped automatically.",
        # A new key after each import empties the uploader, so the imported file is
        # not re-read as a wall of duplicates.
        key=f"upload_{SOURCE_NAME}_{st.session_state.get(_UPLOADER_ROUND, 0)}",
    )

if uploaded is None:
    with status_slot:
        inbox.status_bar(
            [
                inbox.Stage("Import", "active", "Choose a file", 0.15),
                inbox.Stage("Categorize", "upcoming", "Rules, then Haiku"),
                inbox.Stage("Review", "upcoming", "Anything left for a person"),
                inbox.Stage("AI check", "upcoming", "After saving"),
            ],
            title="New statement",
            right="Stage 1 of 4",
        )
    statements = _statement_rows(repo.load_transactions())
    with shell.card("statements", order=2):
        shell.heading("Your statements")
        if statements:
            _statements_table(statements)
            st.caption(
                "Read from what's already in the books: rows still without a committee, "
                "and which charges the AI check has looked at."
            )
        else:
            shell.empty_state("Nothing uploaded yet")
    st.stop()

file_name = uploaded.name
notes = []
if repo.is_file_uploaded(file_name):
    notes.append(f"{file_name} was uploaded before. Re-uploading is safe: rows already in the books are skipped.")
if SOURCE["filename_hint"] not in file_name.lower():
    notes.append(f"The file name doesn't contain '{SOURCE['filename_hint']}'; the contents are checked instead.")

try:
    if file_name.lower().endswith(".csv"):
        raw = pd.read_csv(uploaded, header=None)
    else:
        raw = pd.read_excel(uploaded, header=None)
except Exception as exc:  # noqa: BLE001
    raw = None
    shell.error_state("Could not read the file", f"{type(exc).__name__}: {exc}")

parsed = SOURCE["parser"].parse(raw, source_file=file_name) if raw is not None else None
if parsed is not None:
    for warning in parsed.warnings:
        st.warning(warning)
if parsed is None or not parsed.ok:
    if parsed is not None:
        shell.error_state("This file was not imported", "\n\n".join(parsed.errors) or "No usable rows found.")
    with status_slot:
        inbox.status_bar(
            [
                inbox.Stage("Import", "active", "This file couldn't be read", 0.15, needs_you=True),
                inbox.Stage("Categorize", "upcoming"),
                inbox.Stage("Review", "upcoming"),
                inbox.Stage("AI check", "upcoming"),
            ],
            title=file_name,
            right="Stage 1 of 4",
        )
    st.stop()

# Rules only. This page reruns on every click, so this must never reach the
# model: the model pass is the explicit, metered button below.
schedule = dues.schedule_from_terms(repo.load_terms())
categorized, run = categorize_frame(parsed.rows, dues=schedule)

existing = repo.load_transactions()
staged = categorized.to_dict("records")
for record in staged:
    if isinstance(record.get("transaction_date"), pd.Timestamp):
        record["transaction_date"] = record["transaction_date"].strftime("%Y-%m-%d")
dedupe = split_new_and_duplicate(staged, existing.to_dict("records") if not existing.empty else [])
proposals = {
    str(staged[index].get("natural_key")): proposal for index, proposal in run.proposals.items()
}
new_rows = dedupe.new
date_span = ""
if staged:
    dates = pd.to_datetime([r["transaction_date"] for r in staged], errors="coerce")
    date_span = f"{show.day(dates.min())} – {show.day(dates.max(), year=True)}"

if not new_rows:
    with status_slot:
        inbox.status_bar(
            [
                inbox.Stage("Import", "done", f"0 new · {len(dedupe.duplicates)} already in"),
                inbox.Stage("Categorize", "done", "Nothing new to sort"),
                inbox.Stage("Review", "done", "Nothing to review"),
                inbox.Stage("AI check", "upcoming", "Nothing new to check"),
            ],
            title=file_name,
            right=date_span,
        )
    st.warning("Every row in this file is already in the books. Nothing to do.")
    st.stop()

rules_booked = sum(1 for r in new_rows if not _blank(r.get("budget_category")))
unresolved = [r for r in new_rows if _blank(r.get("budget_category"))]
state_key = _haiku_state_key(new_rows)
ukey = state_key.removeprefix("_model_pass_")
skip_key = f"_import_skip_haiku_{ukey}"
saved = st.session_state.get(state_key)
model_on = settings.llm_enabled()

plan = None
if unresolved and model_on and saved is None:
    clean = [{k: v for k, v in r.items() if k not in _CATEGORIZED_COLUMNS} for r in new_rows]
    plan = model_runs.categorizer_plan(categorize_records(clean, dues=schedule))
    if plan.rows == 0:
        plan = None

in_categorize = plan is not None and not st.session_state.get(skip_key)

if notes or date_span:
    st.caption(" · ".join([f"{file_name}: {date_span}" if date_span else file_name, *notes]))

# --- Stage 2: categorize ------------------------------------------------------------------
if in_categorize:
    with status_slot:
        inbox.status_bar(
            [
                inbox.Stage("Import", "done", f"{len(new_rows)} new · {len(dedupe.duplicates)} already in"),
                inbox.Stage("Categorize", "active", f"{len(unresolved)} left after the rules", 0.9),
                inbox.Stage("Review", "upcoming", "Anything left for a person"),
                inbox.Stage("AI check", "upcoming", "After saving"),
            ],
            title=file_name,
            right="Stage 2 of 4",
        )
    columns = st.columns(4)
    columns[0].metric("Rows in the file", f"{run.total:,}")
    columns[1].metric("Already in the books, skipped", f"{len(dedupe.duplicates):,}")
    columns[2].metric("New, booked by the rules", f"{rules_booked:,} of {len(new_rows):,}")
    columns[3].metric("Rules couldn't settle", f"{len(unresolved):,}")

    ceiling = plan.reservation_usd(settings.llm_model(), settings.llm_max_output_tokens())
    with shell.card("haiku", order=2):
        shell.heading(f"Ask Haiku about the {plan.rows} row(s) the rules left?")
        shell.say(
            "Haiku sees how the rest of this upload was booked. Anything it's under "
            f"{AUTO_APPLY_THRESHOLD:.0%} sure of comes back to you with its suggestion attached. "
            "One metered run.",
            caption=True,
        )
        skip_column, ask_column, _ = st.columns([2, 3, 3])
        if skip_column.button("Skip, I'll do them", key="model_pass_skip"):
            st.session_state[skip_key] = True
            st.rerun()
        if ask_column.button(
            f"Ask Haiku about {plan.rows} row(s) · at most ${ceiling:.3f}",
            key="model_pass_run",
            type="primary",
            disabled=identity.read_only,
        ):
            _run_haiku(new_rows, schedule, state_key, plan.rows)
        spent, cap, _runs = repo.model_month_summary()
        inbox.haiku_meter(spent, cap)

    if dedupe.duplicates:
        with st.expander(f"{len(dedupe.duplicates)} row(s) already in the books — skipped"):
            shell.dataframe(
                pd.DataFrame(
                    {
                        "Date": [show.day(r["transaction_date"]) for r in dedupe.duplicates],
                        "Amount": [r["amount"] for r in dedupe.duplicates],
                        "Merchant": [show.merchant(r["details"]) for r in dedupe.duplicates],
                    }
                ),
                column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
            )
    st.stop()

# --- Stage 3: review --------------------------------------------------------------------------
if saved:
    shell.notify(*saved["note"])

haiku_suggestions = saved.get("suggestions", {}) if saved else {}
haiku_booked_keys: set[str] = set()

tab_needs, tab_haiku, tab_rules = st.tabs(["Needs you", "Booked by Haiku", "Booked by card roster or rules"])

# Haiku's picks first (in code, not on screen): a blanked pick sends its row to
# "Needs you", so these choices must be applied before that list is drawn.
with tab_haiku:
    if not saved:
        st.caption(
            "Haiku didn't run for this upload."
            if model_on else "Haiku is switched off here, so the rules' results are all there is."
        )
        if plan is not None and st.button("Ask Haiku after all", key="model_pass_unskip"):
            st.session_state.pop(skip_key, None)
            st.rerun()
    else:
        table = saved["table"]
        booked_table = table[table["Book as"] != ""].reset_index(drop=True)
        haiku_booked_keys = set(booked_table["natural_key"])
        if booked_table.empty:
            st.caption("Haiku wasn't at least 75% sure of any row, so every one it saw is under Needs you.")
        else:
            st.caption(
                f"Haiku was at least {AUTO_APPLY_THRESHOLD:.0%} sure of these {len(booked_table)} and booked them. "
                "Change **Book as** to correct one; clear it to send the row to Needs you."
            )
            edited = st.data_editor(
                booked_table,
                key=f"{state_key}_book_as",
                width="stretch",
                hide_index=True,
                column_order=[c for c in booked_table.columns if c != "natural_key"],
                disabled=True if identity.read_only else [c for c in booked_table.columns if c != "Book as"],
                column_config={
                    "Amount": st.column_config.NumberColumn(format="$%.2f"),
                    "Confidence": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
                    "Book as": st.column_config.SelectboxColumn(options=NAMES, required=False),
                    "Haiku's reason": st.column_config.TextColumn(width="large"),
                },
            )
            model_runs.apply_categorizer_choices(
                new_rows,
                {key: _id(choice) for key, choice in zip(booked_table["natural_key"], edited["Book as"])},
                saved["answers"],
            )

# The rules' bookings, editable as they always were before saving.
with tab_rules:
    rule_rows = [
        r for r in new_rows
        if str(r.get("natural_key")) not in haiku_booked_keys and not _blank(r.get("budget_category"))
        and str(r.get("match_source")) in {"rule", "scored"}
    ]
    if not rule_rows:
        st.caption("The rules didn't book any new row.")
    else:
        st.caption(
            f"{len(rule_rows)} booked by the card roster or one of the treasurer's rules. "
            "Correct any of them here before saving."
        )
        rules_table = pd.DataFrame(
            {
                "Date": [show.day(r["transaction_date"]) for r in rule_rows],
                "Amount": [r["amount"] for r in rule_rows],
                "Merchant": [show.merchant(r["details"]) for r in rule_rows],
                "Card": [show.card_line(r["details"], r["transaction_date"], r["amount"]) for r in rule_rows],
                "Committee": [committee_name(r["budget_category"]) for r in rule_rows],
                "Purpose": [r.get("purpose") if not _blank(r.get("purpose")) else "" for r in rule_rows],
                "How": [show.how_booked(r.get("match_source"), r.get("match_rule")) for r in rule_rows],
                "Why": [str(r.get("match_rule") or "") for r in rule_rows],
            }
        )
        edited_rules = st.data_editor(
            rules_table,
            key="upload_preview_editor",
            width="stretch",
            hide_index=True,
            disabled=True if identity.read_only else ["Date", "Amount", "Merchant", "Card", "How", "Why"],
            column_config={
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
                "Committee": st.column_config.SelectboxColumn(options=NAMES, required=False),
                "Purpose": st.column_config.SelectboxColumn(options=purpose_dropdown_options(), required=False),
                "Why": st.column_config.TextColumn(width="large"),
            },
        )
        for record, (_, row) in zip(rule_rows, edited_rules.iterrows()):
            record["budget_category"] = _id(row["Committee"])
            record["purpose"] = (row["Purpose"] or "").strip() or None

with tab_needs:
    needs = [r for r in new_rows if _blank(r.get("budget_category"))]
    if not needs:
        st.success("Every new row has a committee. Save when you're ready.")
    else:
        groups: dict[str, list[dict]] = {}
        for record in needs:
            groups.setdefault(show.group_key(record.get("details")), []).append(record)
        ordered = sorted(groups.values(), key=lambda rows: -max(abs(float(r["amount"])) for r in rows))
        decisions = _decisions(ukey)
        open_count = sum(1 for r in needs if str(r.get("natural_key")) not in decisions)
        st.caption(
            f"{open_count} of {len(needs)} still need a committee. Choices aren't saved until you press "
            "Save below; anything left blank waits in Needs attention."
        )
        for rows in ordered:
            open_keys = [str(r["natural_key"]) for r in rows if str(r["natural_key"]) not in decisions]
            for record in sorted(rows, key=lambda r: str(r["transaction_date"])):
                options, tag, why = _suggestions(record, haiku_suggestions, proposals)
                key = str(record["natural_key"])
                group = open_keys if (open_keys and key == open_keys[0]) else []
                _needs_row(record, ukey, options, tag, why, group)

final = _final_rows(new_rows, ukey)
left = sum(1 for r in final if r["budget_category"] is None)
by_you = sum(1 for r in new_rows if str(r.get("natural_key")) in _decisions(ukey))
decided_keys = set(_decisions(ukey))
by_haiku = sum(
    1 for record, row in zip(new_rows, final)
    if str(record.get("natural_key")) in haiku_booked_keys
    and str(record.get("natural_key")) not in decided_keys
    and row["budget_category"] is not None
)
by_rules = len(new_rows) - left - by_you - by_haiku

with status_slot:
    inbox.status_bar(
        [
            inbox.Stage("Import", "done", f"{len(new_rows)} new · {len(dedupe.duplicates)} already in"),
            inbox.Stage(
                "Categorize", "done",
                f"{rules_booked} by rules · " + (f"{by_haiku} by Haiku" if saved else "Haiku not used"),
            ),
            inbox.Stage(
                "Review", "active",
                f"{left} need you" if left else "Ready to save",
                (len(new_rows) - left) / len(new_rows), needs_you=bool(left),
            ),
            inbox.Stage("AI check", "upcoming", "After saving"),
        ],
        title=file_name,
        right="Stage 3 of 4",
    )

_glance(final)

with st.container(key="ais-savebar"):
    text_column, button_column = st.columns([3, 2], vertical_alignment="center")
    text_column.markdown(
        "Every row has a committee."
        if not left
        else f"**{left}** row(s) still need you. Save now and they wait in Needs attention."
    )
    label = f"Save {len(final)} · {left} go to Needs attention" if left else f"Save {len(final)} to the books"
    if button_column.button(label, type="primary", key="confirm_import", disabled=identity.read_only):
        receipt = repo.insert_transactions(final, file_name, identity.email)
        if not receipt.ok:
            shell.error_state("Saving failed — nothing was written", receipt.error or "")
        else:
            message = f"Saved {receipt.inserted} transaction(s) from {file_name}." + (
                f" {receipt.duplicates_skipped} rejected as duplicates by the database."
                if receipt.duplicates_skipped else ""
            )
            st.session_state[_IMPORTED] = {
                "file": file_name,
                "message": message,
                "inserted": receipt.inserted,
                "duplicates": len(dedupe.duplicates),
                "by_rules": max(by_rules, 0),
                "by_haiku": by_haiku,
                "haiku_used": bool(saved),
                "left": left,
            }
            st.session_state[_UPLOADER_ROUND] = st.session_state.get(_UPLOADER_ROUND, 0) + 1
            st.rerun()
