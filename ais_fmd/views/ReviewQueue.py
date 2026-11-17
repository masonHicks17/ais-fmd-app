"""
Needs attention (was the Review Queue): everything waiting on the treasurer,
from every statement, under three tabs:

  Uncategorized    -- rows with no committee, with what the categorizer proposes
  Disputed by VPs  -- "This isn't ours" flags
  AI check flags   -- the Haiku spend review's open flags

PRESENTATION ONLY (2026-11-12 overhaul). Which rows are listed, the period
filter, the suggestions (re-run through the current rules, never the model),
how a decision is written and logged as a label (M19), and the dispute and
spend-review actions are unchanged. What changed is the layout: one card per
row with the suggestions as buttons, filters folded into one line, and the
three kinds of work as tabs instead of stacked expanders.

Stored categorizations are the record (treasury, 2026-10-02, restated
2026-10-16): this page only ever lists rows with NO committee. Correcting a
stored committee is a deliberate, one-row edit on All transactions.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import committee_name, committee_reference_rows, purpose_dropdown_options
from ais_fmd.data import repositories as repo
from ais_fmd.data.backend import TransactionChange
from ais_fmd.domain import dues
from ais_fmd.domain.categorize.pipeline import categorize_records
from ais_fmd.domain.categorize.predicates import COMMITTEE_PURPOSE
from ais_fmd.domain.categorize.scoring import current_era
from ais_fmd.ui import charts, inbox, shell
from ais_fmd.ui import flags as flag_ui
from ais_fmd.ui import spend_review as spend_review_ui
from ais_fmd.ui import txn_display as show

identity = auth.require(auth.Role.TREASURER)

NAMES = [name for _, name in committee_reference_rows()]
ID_BY_NAME = {name: cid for cid, name in committee_reference_rows()}
_DECISIONS = "_queue_decisions"
_PURPOSES = "_queue_purposes"
_FLASH = "_queue_flash"

PERIOD_CHOICES = {"Past year": 365, "Past 6 months": 182, "Past 90 days": 90, "Everything": None}
FILTER_ALL = "Everything"
FILTER_PROPOSED = "Flagged: scored but uncertain"
FILTER_RESOLVABLE = "Resolvable now"
FILTER_NO_SIGNAL = "No signal at all"
SORTS = ["Largest amount first", "Most confident suggestion first", "Oldest first"]


def _decisions() -> dict:
    return st.session_state.setdefault(_DECISIONS, {})


def _purposes() -> dict:
    return st.session_state.setdefault(_PURPOSES, {})


def _choose(snapshot: dict, widget_key: str) -> None:
    committee = ID_BY_NAME.get(str(st.session_state.get(widget_key) or ""))
    if committee is not None:
        _decisions()[snapshot["transaction_id"]] = {**snapshot, "committee_id": committee}
        _purposes().pop(snapshot["transaction_id"], None)


def _choose_many(snapshots: list[dict], committee: int) -> None:
    for snapshot in snapshots:
        _decisions()[snapshot["transaction_id"]] = {**snapshot, "committee_id": committee}
        _purposes().pop(snapshot["transaction_id"], None)


def _choose_group(snapshots: list[dict], widget_key: str) -> None:
    committee = ID_BY_NAME.get(str(st.session_state.pop(widget_key, None) or ""))
    if committee is not None:
        _choose_many(snapshots, committee)


def _undo(transaction_id: int, widget_keys: list[str]) -> None:
    _decisions().pop(transaction_id, None)
    _purposes().pop(transaction_id, None)
    for key in widget_keys:
        st.session_state.pop(key, None)


def _set_purpose(transaction_id: int, widget_key: str) -> None:
    _purposes()[transaction_id] = st.session_state.get(widget_key) or ""


shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Needs attention",
    "Everything waiting on the treasurer, from every statement: rows with no committee, "
    "charges a VP disputed, and the AI check's flags.",
)
inbox.styles()

transactions = repo.load_transactions()
if transactions.empty:
    shell.empty_state("No transactions yet", "Upload a statement from Import & review.")
    st.stop()

flash = st.session_state.pop(_FLASH, None)
if flash:
    shell.notify(*flash)

# --- Counts across the top ----------------------------------------------------------
needs_committee = transactions["budget_category"].isna()
if "source_file" in transactions.columns:
    from_original_app = transactions["source_file"].isna()
else:
    from_original_app = pd.Series(False, index=transactions.index)
flags = flag_ui.load_flags_or_none(None)
open_disputes = 0 if flags is None or flags.empty else int((flags["status"] == "open").sum())
try:
    reviews = repo.load_spend_reviews()
    open_ai = 0 if reviews.empty else int((reviews["status"] == "open").sum())
except Exception:  # noqa: BLE001 - migration 013 not run
    open_ai = 0
app_blanks = int((needs_committee & ~from_original_app).sum())


def _counter(count: int, label: str, sub: str) -> str:
    return (
        '<div style="background:#fff;border-radius:20px;padding:14px 18px;box-shadow:0 1px 2px rgba(17,17,17,.04),'
        '0 8px 24px rgba(17,17,17,.05);display:flex;flex-direction:column">'
        f'<span style="font-size:26px;font-weight:700">{count:,}</span>'
        f'<span style="font-weight:600">{label}</span><span style="font-size:13px;color:#55565A">{sub}</span></div>'
    )


shell.html(
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:4px 0 8px">'
    + _counter(app_blanks, "Uncategorized", "No committee yet, every period. The list follows your filters.")
    + _counter(open_disputes, "Disputed by VPs", "“This isn't ours” from a committee")
    + _counter(open_ai, "AI check flags", "Unusual for the committee it's booked to")
    + "</div>"
)

# Static labels: a count in a tab label would reset the open tab whenever it changed.
tab_uncat, tab_disputes, tab_ai = st.tabs(["Uncategorized", "Disputed by VPs", "AI check flags"])

with tab_disputes:
    # Charges a VP has disputed. The panel lives in ui/flags.py.
    flag_ui.treasurer_panel(transactions, identity.email, inline=True)

with tab_ai:
    # The Haiku spend review: a metered run the treasurer starts, and its flags.
    spend_review_ui.treasurer_panel(transactions, identity, inline=True)


def uncategorized() -> None:
    history_blanks = int((needs_committee & from_original_app).sum())

    # --- Filters, folded into one line -------------------------------------------------
    summary_column, change_column = st.columns([4, 1], vertical_alignment="center")
    with change_column.popover("Change filters", width="stretch"):
        period_choice = st.selectbox(
            "Period", list(PERIOD_CHOICES), key="queue_period",
            help="Limits the queue to recently dated transactions.",
        )
        filter_choice = st.radio(
            "Show",
            [FILTER_ALL, FILTER_PROPOSED, FILTER_RESOLVABLE, FILTER_NO_SIGNAL],
            key="queue_filter",
            help=(
                "Flagged rows were scored but fell below the auto-apply threshold — "
                "each carries a proposed committee and the reason it is uncertain."
            ),
        )
        sort_choice = st.radio("Order", SORTS, key="queue_sort")
        batch_size = st.slider("How many to show", 5, 50, 12, step=1, key="queue_batch")
        include_history = st.toggle(
            "Include blank rows from before this app",
            value=False,
            key="queue_include_history",
            disabled=not history_blanks,
            help=(
                f"{history_blanks:,} transaction(s) carried over from the original app have "
                "no committee. They are left exactly as stored unless you choose to work "
                "them here. Rows that already have a committee are never shown."
            ),
        )

    pending = transactions[needs_committee & (include_history | ~from_original_app)].copy()
    # Coverage is a property of the whole ledger, not of the period being worked.
    total_pending = int(needs_committee.sum())
    total_resolved = len(transactions) - total_pending

    hidden_note = ""
    window_days = PERIOD_CHOICES[period_choice]
    if window_days is not None and not pending.empty:
        dated = pd.to_datetime(pending["transaction_date"], errors="coerce")
        # Anchor on the newest transaction rather than today: a statement imported
        # after a gap would otherwise show an empty queue and read as "all clear".
        latest = dated.max()
        if pd.notna(latest):
            before = len(pending)
            pending = pending[dated >= latest - pd.Timedelta(days=window_days)]
            hidden = before - len(pending)
            if hidden:
                hidden_note = f" · {hidden:,} older hidden"

    summary_column.markdown(
        f"Showing **{period_choice.lower()}** · **{filter_choice.lower()}** · **{sort_choice.lower()}**"
        + (" · including rows from before this app" if include_history else "")
        + hidden_note
    )

    if pending.empty:
        shell.empty_state("Nothing unresolved in this period", "Widen the period under Change filters.")
        return

    metrics = st.columns(3)
    metrics[0].metric(
        "Awaiting review", f"{len(pending):,}",
        help=f"In the selected period. {total_pending:,} unresolved across the whole ledger.",
    )
    metrics[1].metric("Already categorized", f"{total_resolved:,}")
    metrics[2].metric(
        "Coverage",
        f"{(total_resolved / len(transactions) * 100) if len(transactions) else 0:.1f}%",
        help="Across every imported statement, not just the selected period.",
    )

    # --- Suggestions ------------------------------------------------------------------
    # The current card roster and rules, re-run over the pending rows. Rules only:
    # this page reruns on every click, so it never calls the model.
    records = pending.to_dict("records")
    run = categorize_records(records, dues=dues.schedule_from_terms(repo.load_terms()))
    held = run.held_for_review

    rows = []
    for position, item in enumerate(run.classifications):
        record = records[position]
        proposal = held.get(position)
        if item.is_assigned or proposal is None:
            committee, rule, source, confidence = item.committee_id, item.rule, item.source, item.confidence
            rivals = []
            contested = False
        else:
            suggestion = proposal.as_classification()
            committee, rule, source = suggestion.committee_id, proposal.explain(), "proposed"
            confidence = proposal.confidence
            rivals = [cid for cid, _ in proposal.conflicting()]
            contested = bool(rivals)
        options = [int(committee)] if committee is not None and not pd.isna(committee) else []
        options += [int(cid) for cid in rivals if int(cid) not in options]
        rows.append(
            {
                **record,
                "suggested_committee": committee,
                "match_rule": rule,
                "match_source": source,
                "confidence": confidence,
                "contested": contested,
                "options": options[:3],
                "magnitude": abs(float(record["amount"])),
            }
        )
    pending = pd.DataFrame(rows)

    resolvable = int(pending["match_source"].isin({"rule", "scored", "llm"}).sum())
    proposed = int((pending["match_source"] == "proposed").sum())
    notes = []
    if resolvable:
        notes.append(f"The current card roster and rules can now settle **{resolvable}** of these without Haiku.")
    if proposed:
        notes.append(f"**{proposed}** were scored but held back as too uncertain; each shows its best guess.")
    if notes:
        st.caption(" ".join(notes))

    if filter_choice == FILTER_PROPOSED:
        pending = pending[pending["match_source"] == "proposed"]
    elif filter_choice == FILTER_RESOLVABLE:
        pending = pending[pending["match_source"].isin({"rule", "scored", "llm"})]
    elif filter_choice == FILTER_NO_SIGNAL:
        pending = pending[pending["match_source"] == "none"]
    if pending.empty:
        shell.empty_state("Nothing in this view", "Choose a different Show option under Change filters.")
        return

    if sort_choice == SORTS[0]:
        pending = pending.sort_values("magnitude", ascending=False)
    elif sort_choice == SORTS[1]:
        pending = pending.sort_values(["confidence", "magnitude"], ascending=[False, False])
    else:
        pending = pending.sort_values("transaction_date")
    batch = pending.head(batch_size)

    # Look-alike charges (same merchant, same card) sit together, in the order
    # their largest member comes up.
    batch = batch.assign(_group=[show.group_key(d) for d in batch["details"]])
    first_seen = {key: index for index, key in reversed(list(enumerate(batch["_group"])))}
    batch = batch.assign(_order=batch["_group"].map(first_seen)).sort_values("_order", kind="stable")

    decisions = _decisions()
    for group_name, members in batch.groupby("_group", sort=False):
        open_members = [r for _, r in members.iterrows() if int(r["transactionid"]) not in decisions]
        for _, row in members.iterrows():
            _row_card(row, open_members if open_members and int(row["transactionid"]) == int(open_members[0]["transactionid"]) else [])

    _save_bar()

    with st.expander("Suggestion confidence across the queue"):
        shell.chart(
            charts.confidence_histogram([value for value in pending["confidence"].tolist() if value > 0]),
            key="queue_confidence",
        )
        st.caption(
            "Cards on the roster and exact rules score 1.0, heuristic rules 0.85–0.9, "
            "model suggestions carry the model's own confidence. Zero means nothing matched."
        )


def _snapshot(row: pd.Series) -> dict:
    """What the categorizer proposed, captured before the human answers (M19)."""
    suggestion = row["suggested_committee"]
    return {
        "transaction_id": int(row["transactionid"]),
        "details": row["details"],
        "transaction_date": row["transaction_date"],
        "amount": row["amount"],
        "account": row["account"],
        "model_committee": int(suggestion) if pd.notna(suggestion) else None,
        "model_confidence": float(row["confidence"]),
        "model_source": str(row["match_source"]),
    }


def _row_card(row: pd.Series, group: list[pd.Series]) -> None:
    transaction_id = int(row["transactionid"])
    pick_key, other_key = f"queue_pick_{transaction_id}", f"queue_committee_{transaction_id}"
    purpose_key = f"queue_purpose_{transaction_id}"
    decided = _decisions().get(transaction_id)

    if decided is not None:
        committee = decided["committee_id"]
        with st.container(border=True):
            left, middle, right = st.columns([5, 3, 1], vertical_alignment="center")
            with left:
                inbox.booked_line(row.to_dict(), committee_name(committee))
            purposes = purpose_dropdown_options()
            current = _purposes().get(transaction_id, COMMITTEE_PURPOSE.get(committee, ""))
            middle.selectbox(
                "Purpose", purposes, index=purposes.index(current) if current in purposes else 0,
                key=purpose_key, format_func=lambda p: p or "None", on_change=_set_purpose, args=(transaction_id, purpose_key),
                disabled=identity.read_only,
            )
            right.button(
                "Undo", key=f"queue_undo_{transaction_id}", on_click=_undo,
                args=(transaction_id, [pick_key, other_key, purpose_key]), disabled=identity.read_only,
            )
        return

    source = str(row["match_source"])
    if source == "proposed":
        tag = f"{row['confidence']:.0%} · {'contested' if row['contested'] else 'weak evidence'}"
        why = str(row["match_rule"])
    elif row["match_rule"] and source in {"rule", "scored", "llm"}:
        tag = f"{show.how_booked(source, row['match_rule'])} · {row['confidence']:.0%}"
        why = str(row["match_rule"])
    else:
        tag, why = "No suggestion", "No card on the roster or rule matched this one."
    origin = row.get("source_file")
    when_from = f" · from {origin}" if isinstance(origin, str) and origin else ""

    options = list(row["options"])
    with st.container(border=True):
        if len(group) > 1:
            strip_left, strip_right = st.columns([3, 2], vertical_alignment="center")
            strip_left.caption(
                f"{len(group)} {show.merchant(row['details'])} charges on the same card here. "
                "Booking them together remembers nothing for next time."
            )
            snapshots = [_snapshot(member) for member in group]
            if options:
                strip_right.button(
                    f"Book all {len(group)} as {committee_name(options[0])}",
                    key=f"queue_group_{transaction_id}", on_click=_choose_many,
                    args=(snapshots, options[0]), disabled=identity.read_only,
                )
            else:
                group_key = f"queue_groupsel_{transaction_id}"
                strip_right.selectbox(
                    f"Book all {len(group)} as", NAMES, index=None, placeholder="Choose a committee",
                    key=group_key, on_change=_choose_group, args=(snapshots, group_key),
                    disabled=identity.read_only,
                )
        detail, choice = st.columns([3, 2])
        with detail:
            inbox.row_header(row.to_dict(), tag=tag, why=why + when_from)
            inbox.bank_line(row["details"])
        with choice:
            snapshot = _snapshot(row)
            if options:
                st.pills(
                    "Book as", [committee_name(option) for option in options], key=pick_key,
                    on_change=_choose, args=(snapshot, pick_key), disabled=identity.read_only,
                )
            st.selectbox(
                "Other committee" if options else "Book as", NAMES, index=None, placeholder="Choose a committee", key=other_key,
                on_change=_choose, args=(snapshot, other_key), disabled=identity.read_only,
            )
            st.caption("Purpose fills in from the committee you pick; you can change it after.")


def _save_bar() -> None:
    decisions = _decisions()
    with st.container(key="ais-savebar"):
        text_column, button_column = st.columns([3, 2], vertical_alignment="center")
        text_column.markdown(
            f"**{len(decisions)}** chosen, not saved yet." if decisions else "Pick a committee on any row above."
        )
        submitted = button_column.button(
            f"Save {len(decisions)} choice(s)" if decisions else "Save choices",
            type="primary", key="queue_apply", disabled=identity.read_only or not decisions,
        )
    if submitted:
        _apply(decisions)


def _apply(decisions: dict) -> None:
    changes: list[TransactionChange] = []
    labels: list[dict] = []
    era = current_era()
    for transaction_id, decision in decisions.items():
        committee_id = decision["committee_id"]
        purpose = (_purposes().get(transaction_id, COMMITTEE_PURPOSE.get(committee_id, "")) or "").strip() or None
        changes.append(TransactionChange(transaction_id=transaction_id, purpose=purpose, budget_category=committee_id))
        labels.append(
            {
                "source": "review",
                "source_ref": "review-queue",
                "era": era,
                "transaction_id": transaction_id,
                "transaction_date": str(decision["transaction_date"]),
                "amount": float(decision["amount"]),
                "details": str(decision["details"]),
                "account": None if pd.isna(decision["account"]) else str(decision["account"]),
                "committee_id": committee_id,
                "purpose": purpose,
                "model_committee": decision["model_committee"],
                "model_confidence": decision["model_confidence"],
                "model_source": decision["model_source"],
                "labeled_by": identity.email,
                # Keyed on the transaction, so re-deciding a row updates nothing
                # rather than logging the same example twice.
                "natural_key": f"review:{transaction_id}",
            }
        )

    result = repo.update_transactions(changes, identity.email)
    if result.error:
        shell.error_state("Could not save those choices", result.error)
        return
    message = f"Categorized {result.updated} transaction(s)."
    # Record the labels whether or not the human agreed with the suggestion.
    # Overrides are the most informative rows in the set, so a failure here is
    # surfaced rather than swallowed.
    label_result = repo.record_labels(labels, identity.email)
    kind = "success"
    if label_result.error:
        kind = "warning"
        message += f" Saved, but the training log failed: {label_result.error}"
    else:
        overrides = sum(
            1 for item in labels
            if item["model_committee"] is not None and item["model_committee"] != item["committee_id"]
        )
        message += f" Logged {label_result.updated} for training"
        message += f" ({overrides} overrode the suggestion)." if overrides else "."
    st.session_state[_DECISIONS] = {}
    st.session_state[_PURPOSES] = {}
    st.session_state[_FLASH] = (kind, message)
    st.rerun()


with tab_uncat:
    uncategorized()
