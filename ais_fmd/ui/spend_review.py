"""
The spend review on the Review Queue: start a metered run, then work its flags.

A run only starts from the button, never from a page load, and the button says
how many charges it will send and the most it can cost before it is pressed.
Verdicts are suggestions. Nothing here changes a booking or sends a message:
the draft to the VP is shown for the treasurer to copy, and moving a charge is
still done on the Transactions page.
"""

from __future__ import annotations

import html

import pandas as pd
import streamlit as st

from ais_fmd import settings
from ais_fmd.config.categories import committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import model_runs
from ais_fmd.domain.money import format_currency
from ais_fmd.ui import shell
from ais_fmd.ui.flags import load_flags_or_none

_FLASH = "_spend_review_flash"
_LAST_RUN = "_spend_review_last_run"
_VERDICT_LABELS = {
    "question": "Ask the VP",
    "other_committee": "Looks like another committee's",
}


def treasurer_panel(transactions: pd.DataFrame, identity, *, inline: bool = False) -> None:
    """
    The run button, a table of what the last run checked, and the open flags.
    `inline` draws it open on the page (the import flow); otherwise it sits in
    an expander (the Review Queue).
    """
    try:
        reviews = repo.load_spend_reviews()
    except Exception:  # noqa: BLE001 - migration 013 not run: the feature stays hidden
        return

    terms = repo.load_terms()
    selection = model_runs.select_for_review(
        transactions, terms, reviews, load_flags_or_none(None)
    )
    rows, _baseline, plan = model_runs.spend_review_plan(selection, transactions, terms)
    spent, cap, _runs_today = repo.model_month_summary()

    open_reviews = reviews[reviews["status"] == "open"] if not reviews.empty else reviews
    frame = st.container() if inline else st.expander(
        f"Spend review ({len(open_reviews)} to look at)",
        expanded=len(open_reviews) > 0 or _FLASH in st.session_state,
    )
    with frame:
        _flash()
        shell.say(
            "Haiku checks each new charge against its committee's spending profile "
            "(config/spend_review_prompt.md) and flags the ones worth a question. "
            "It never changes a booking.",
            caption=True,
        )
        columns = st.columns(3)
        columns[0].metric("New charges to check", f"{selection.count:,}")
        # Four places below a dollar: a whole month of Haiku is fractions of a cent.
        spent_text = "$0.00" if not spent else (f"${spent:,.4f}" if spent < 1 else f"${spent:,.2f}")
        columns[1].metric("Model spend this month", spent_text, help=f"Capped at ${cap:,.2f}.")
        columns[2].metric("Already checked", f"{selection.skipped.get('already reviewed', 0):,}")

        _run_button(identity, rows, plan, selection.count)
        _last_run_table(reviews, transactions)
        if not open_reviews.empty:
            st.markdown(f"**{len(open_reviews)} flag(s) to look at**")
        _open_flags(open_reviews, transactions, identity)

        with st.expander("What is never sent"):
            for reason, count in selection.skipped.items():
                st.caption(f"{count:,} — {reason}")


def _run_button(identity, rows, plan, eligible: int) -> None:
    if not settings.llm_enabled():
        st.caption("The model is switched off here, so no review can run.")
        return
    if not rows:
        st.caption("Every eligible charge has been checked.")
        return

    ceiling = plan.reservation_usd(settings.llm_model(), settings.llm_max_output_tokens())
    later = eligible - len(rows)
    label = f"Check {len(rows)} charge(s) with Haiku · at most ${ceiling:.3f}"
    if later:
        st.caption(f"{later:,} more will be left for the next run (limit {len(rows)} per run).")
    if st.button(label, type="primary", key="spend_review_run", disabled=identity.read_only):
        with st.spinner(f"Haiku is checking {len(rows)} charge(s)…"):
            report = repo.run_spend_review(identity.email)
        st.session_state[_FLASH] = _summary(report)
        if report.run_id is not None:
            st.session_state[_LAST_RUN] = report.run_id
        st.rerun()


_VERDICT_TEXT = {"fits": "Fits", "question": "Ask the VP", "other_committee": "Another committee's"}


def _last_run_table(reviews: pd.DataFrame, transactions: pd.DataFrame) -> None:
    """Every charge the last run checked, flags first -- not just the flags."""
    run_id = st.session_state.get(_LAST_RUN)
    if run_id is None or reviews.empty or "run_id" not in reviews.columns:
        return
    checked = reviews[pd.to_numeric(reviews["run_id"], errors="coerce") == int(run_id)]
    if checked.empty:
        return
    merged = checked.merge(
        transactions[["transactionid", "transaction_date", "amount", "details"]],
        left_on="transaction_id", right_on="transactionid", how="left",
    )
    verdicts = []
    for _, row in merged.iterrows():
        text = _VERDICT_TEXT.get(row["verdict"], row["verdict"])
        if row["verdict"] == "question" and int(row["booked_committee_id"]) == 1:
            text = "Question for you"
        if pd.notna(row.get("suggested_committee_id")):
            text += f": {committee_name(row['suggested_committee_id'])}"
        verdicts.append(text)
    table = pd.DataFrame(
        {
            "Date": pd.to_datetime(merged["transaction_date"]).dt.date,
            "Amount": merged["amount"],
            "Details": merged["details"].astype(str).str.split().str.join(" ").str[:110],
            "Booked to": merged["booked_committee_id"].map(committee_name),
            "Haiku's verdict": verdicts,
            "Confidence": (pd.to_numeric(merged["confidence"], errors="coerce") * 100).round(),
            "Haiku's reason": merged["reason"].fillna(""),
            "_flag": (merged["verdict"] != "fits").astype(int),
        }
    ).sort_values(["_flag", "Date"], ascending=[False, True]).drop(columns="_flag")
    flagged = int((merged["verdict"] != "fits").sum())
    st.markdown(f"**What Haiku checked** — {len(table)} charge(s), {flagged} flagged")
    shell.dataframe(
        table,
        column_config={
            "Amount": st.column_config.NumberColumn(format="dollar"),
            "Confidence": st.column_config.ProgressColumn(format="%.0f%%", min_value=0, max_value=100),
            "Details": st.column_config.TextColumn(width="large"),
            "Haiku's reason": st.column_config.TextColumn(width="large"),
        },
    )


def _summary(report: model_runs.RunReport) -> tuple[str, str]:
    if not report.started:
        return "warning", f"Nothing was sent. {report.refusal or ''}".strip()
    text = (
        f"Checked {report.saved} charge(s) for ${report.cost_usd:.4f}; "
        f"{report.flagged} flagged."
    )
    if report.batches_failed:
        text += (
            f" {report.batches_failed} batch(es) failed and stay unchecked "
            f"({'; '.join(report.errors[:2])})."
        )
        return "warning", text
    return "success", text


def _flash() -> None:
    message = st.session_state.pop(_FLASH, None)
    if message:
        kind, text = message
        shell.notify(kind, text)


def _open_flags(open_reviews: pd.DataFrame, transactions: pd.DataFrame, identity) -> None:
    if open_reviews.empty:
        return
    charges = transactions.set_index("transactionid")
    for _, review in open_reviews.iterrows():
        review_id = int(review["review_id"])
        tid = int(review["transaction_id"])
        if tid not in charges.index:
            continue
        charge = charges.loc[tid]
        day = pd.Timestamp(charge["transaction_date"]).date()
        booked = committee_name(review["booked_committee_id"]) or "no committee"
        with st.container(border=True):
            shell.say(f"**{day}** · {format_currency(charge['amount'])} · booked to **{booked}**")
            st.caption(str(charge["details"] or "")[:160])
            verdict = _VERDICT_LABELS.get(review["verdict"], review["verdict"])
            suggested = review.get("suggested_committee_id")
            if pd.notna(suggested):
                verdict += f": {committee_name(suggested)}"
            confidence = review.get("confidence")
            confidence_text = f" ({float(confidence):.0%})" if pd.notna(confidence) else ""
            # The reason is model output: escaped before it goes inside HTML.
            st.markdown(
                shell.money_safe(
                    f"{shell.pill(html.escape(verdict + confidence_text), 'approaching')} &nbsp; "
                    f"{html.escape(str(review['reason'] or ''))}"
                ),
                unsafe_allow_html=True,
            )
            if review.get("message_to_vp"):
                st.caption("Draft to the VP (copy it; nothing is sent from here):")
                st.code(str(review["message_to_vp"]), language=None, wrap_lines=True)
            else:
                st.caption("For you, not a VP: check it against the bank record or the roster.")

            left, right = st.columns(2)
            if left.button("Done", key=f"spend_done_{review_id}", disabled=identity.read_only):
                _close(review_id, "resolved", identity)
            if right.button("Fine as booked", key=f"spend_dismiss_{review_id}", disabled=identity.read_only):
                _close(review_id, "dismissed", identity)


def _close(review_id: int, status: str, identity) -> None:
    result = repo.resolve_spend_review(review_id, status, identity.email)
    if result.error:
        shell.error_state("Could not close it", result.error)
        return
    st.session_state[_FLASH] = ("success", "Closed.")
    st.rerun()
