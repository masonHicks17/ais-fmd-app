"""
The two sides of "This isn't ours".

`vp_panel` is what a VP sees under their transactions: a way to flag a charge they
think belongs to another committee, and the list of what they have flagged and
how the treasurer answered. `treasurer_panel` is what lands in the Review Queue.

A flag by itself changes nothing: it is a note. Only the treasurer decides where
money is booked. Their verdict on a flag is one of three things -- it is theirs and
stays put, it is not theirs and moves to the committee they pick (the booking and the
flag close together, through the normal edit path so closed terms and the audit trail
still apply), or it was already fixed elsewhere and the flag just closes.

Both are written to be added to an existing page with one call, because the pages
they sit on are shared with other teams.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import (
    COMMITTEES,
    committee_label,
    committee_name,
    parse_committee_label,
)
from ais_fmd.data import repositories as repo
from ais_fmd.domain import flags as flags_domain
from ais_fmd.domain.details import tidy
from ais_fmd.domain.money import format_currency
from ais_fmd.ui import shell

_NONCE = "_flag_form_nonce"
_FLASH = "_flag_flash"

# A VP picks from the most recent charges in their current view; the filters above
# the table narrow it. Bounded so the list stays usable for a busy committee.
MAX_CHOICES = 200

# The treasurer's three possible verdicts on a flag.
VERDICT_KEEP = "It's theirs: keep it where it is"
VERDICT_MOVE = "It isn't theirs: move it to another committee"
VERDICT_FIXED = "I already fixed it elsewhere: just close the flag"


def load_flags_or_none(committee_ids: tuple[int, ...] | None) -> pd.DataFrame | None:
    """
    The flags, or None when this deployment cannot store them yet (for example
    the table has not been created). Pages hide the feature on None instead of
    erroring -- the same way My Committee treats reimbursements.
    """
    try:
        return repo.load_flags(committee_ids)
    except Exception:  # noqa: BLE001 - a missing table is a deployment state, not an error
        return None


def _flash() -> None:
    message = st.session_state.pop(_FLASH, None)
    if message:
        shell.notify("success", message)


def _charge_label(row: pd.Series) -> str:
    day = pd.Timestamp(row["transaction_date"]).date()
    details = tidy(row["details"])[:48]
    return f"{day}  ·  {format_currency(row['amount'])}  ·  {details}"


# --- The VP's side: updates on My Committee -------------------------------------

# How many updates to list at once. The rest are on My Transactions.
MAX_UPDATES = 5

_LEVEL = {
    flags_domain.PENDING: "info",
    flags_domain.MOVED_OUT: "success",
    flags_domain.KEPT: "info",
    flags_domain.CLOSED: "info",
    flags_domain.ARRIVED: "success",
}


def load_arrivals_or_none(committee_ids: tuple[int, ...]) -> pd.DataFrame | None:
    """Charges moved into these lines after a flag, or None if flags cannot be stored here."""
    try:
        return repo.load_flags_moved_into(committee_ids)
    except Exception:  # noqa: BLE001 - a missing table is a deployment state, not an error
        return None


def updates_panel(
    own_flags: pd.DataFrame | None,
    arrivals: pd.DataFrame | None,
    transactions: pd.DataFrame,
    line_ids: tuple[int, ...],
) -> None:
    """
    Tell a committee what happened to the charges it flagged, and which charges
    were moved in. Draws nothing when there is nothing to say, so a committee with
    no flag activity sees no empty box.
    """
    if own_flags is None and arrivals is None:
        return  # this deployment cannot store flags yet
    updates = flags_domain.notifications(
        own_flags, arrivals, transactions, line_ids, today=pd.Timestamp.today().normalize()
    )
    if updates.empty:
        return

    st.subheader("Updates")
    for _, update in updates.head(MAX_UPDATES).iterrows():
        day = pd.Timestamp(update["transaction_date"]).strftime("%b %d, %Y")
        charge = f"{day}  ·  {format_currency(update['amount'])}  ·  {str(update['details'])[:48]}"
        lines = [f"**{update['headline']}**", charge]
        if update["reply"]:
            lines.append(f"Treasurer: {update['reply']}")
        text = "  \n".join(lines)  # two spaces + newline = a line break in markdown
        shell.notify(_LEVEL[update["kind"]], text)
    if len(updates) > MAX_UPDATES:
        st.caption(
            f"Showing the latest {MAX_UPDATES} of {len(updates)}. "
            "Everything is on My Transactions."
        )
    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)


# --- The VP's side --------------------------------------------------------------


def vp_panel(
    charges: pd.DataFrame,
    all_transactions: pd.DataFrame,
    flags: pd.DataFrame,
    line_ids: tuple[int, ...],
    actor: str,
) -> None:
    """
    `charges` are the rows the VP is looking at, newest first (each needs
    `transactionid`, `transaction_date`, `amount`, `details`). `all_transactions`
    is only used to show the charge behind each of their existing flags.
    """
    nonce = st.session_state.setdefault(_NONCE, 0)
    open_ids = {
        tid for tid, status in flags_domain.status_by_transaction(flags).items()
        if status == flags_domain.OPEN
    }
    candidates = charges[~charges["transactionid"].isin(open_ids)].head(MAX_CHOICES)
    labels = {int(row["transactionid"]): _charge_label(row) for _, row in candidates.iterrows()}

    # Open straight after a submit so the confirmation is actually seen.
    with st.expander("This isn't ours — flag a charge", expanded=_FLASH in st.session_state):
        _flash()
        shell.say(
            "If a charge here belongs to a different committee, flag it and say why. "
            "The treasurer reviews it. You can't change the booking yourself, so "
            "nothing moves until they decide.",
        )

        if not labels:
            st.caption("There are no unflagged charges in this view.")
        else:
            charge_id = st.selectbox(
                "Which charge?",
                list(labels),
                format_func=lambda tid: labels[tid],
                key=f"flag_charge_{nonce}",
            )
            note = st.text_area(
                "Why isn't it yours?",
                max_chars=flags_domain.MAX_NOTE_LENGTH,
                placeholder="For example: this was bought for a different committee's event.",
                key=f"flag_note_{nonce}",
            )
            if st.button("Flag for the treasurer", type="primary", key=f"flag_submit_{nonce}"):
                result = repo.create_flag(int(charge_id), line_ids, note, actor)
                if result.error:
                    shell.error_state("Not flagged", result.error)
                elif result.unchanged:
                    shell.notify("info", "That charge is already flagged and waiting for the treasurer.")
                else:
                    st.session_state[_NONCE] = nonce + 1  # fresh, empty form next run
                    st.session_state[_FLASH] = "Flagged. The treasurer will see it in their review queue."
                    st.rerun()

        mine = flags_domain.with_charges(flags, all_transactions)
        if not mine.empty:
            st.markdown("**Your flagged charges**")
            table = pd.DataFrame(
                {
                    "Flagged": pd.to_datetime(mine["flagged_at"]).dt.date,
                    "Charge date": pd.to_datetime(mine["transaction_date"]).dt.date,
                    "Amount": mine["amount"],
                    "Details": mine["details"].map(tidy).str.slice(0, 60),
                    "Your note": mine["note"],
                    "Status": mine["status"].map(flags_domain.vp_status_label),
                    "Treasurer's reply": mine["resolution_note"].fillna(""),
                }
            )
            shell.dataframe(
                table, column_config={"Amount": st.column_config.NumberColumn(format="dollar")}
            )


# --- The treasurer's side -------------------------------------------------------


def treasurer_panel(transactions: pd.DataFrame, actor: str) -> None:
    flags = load_flags_or_none(None)
    if flags is None:
        return  # this deployment cannot store flags yet; nothing to show

    open_flags = flags[flags["status"] == flags_domain.OPEN] if not flags.empty else flags
    count = len(open_flags)

    with st.expander(
        f"Charges disputed by VPs ({count} open)",
        expanded=count > 0 or _FLASH in st.session_state,
    ):
        _flash()
        if count == 0:
            shell.say("No VP has an open flag. Nothing to review here.", caption=True)
        else:
            shell.say(
                "A VP says these charges belong to a different committee. Nothing has "
                "changed yet. Decide each one: keep it where it is, or move it -- and "
                "the booking changes right here, with the flag closed to match.",
                caption=True,
            )
            detailed = flags_domain.with_charges(open_flags, transactions)
            for _, row in detailed.iterrows():
                _treasurer_decision(row, actor)

        closed = flags[flags["status"] != flags_domain.OPEN] if not flags.empty else flags
        if not closed.empty:
            st.markdown("**Recently closed**")
            recent = flags_domain.with_charges(closed.head(10), transactions)
            shell.dataframe(
                pd.DataFrame(
                    {
                        "Closed": pd.to_datetime(recent["resolved_at"]).dt.date,
                        "Charge date": pd.to_datetime(recent["transaction_date"]).dt.date,
                        "Amount": recent["amount"],
                        "Outcome": recent["status"].map(flags_domain.vp_status_label),
                        "VP's note": recent["note"],
                        "Your reply": recent["resolution_note"].fillna(""),
                    }
                ),
                column_config={"Amount": st.column_config.NumberColumn(format="dollar")},
            )


def _treasurer_decision(row: pd.Series, actor: str) -> None:
    """One open flag: show the evidence, take the treasurer's verdict, and apply it."""
    flag_id = int(row["flag_id"])
    day = pd.Timestamp(row["transaction_date"]).date() if pd.notna(row["transaction_date"]) else "?"
    current_id = None if pd.isna(row["budget_category"]) else int(row["budget_category"])
    flagged_id = None if pd.isna(row["booked_to_committee_id"]) else int(row["booked_to_committee_id"])
    current_name = committee_name(current_id) or "no committee"
    moved_since = current_id != flagged_id

    with st.container(border=True):
        shell.say(f"**{day}** · {format_currency(row['amount'])} · booked to **{current_name}**")
        st.caption(str(row["details"] or "")[:140])
        shell.say(f"> {row['note']}")
        st.caption(f"Flagged by {row['flagged_by']}")

        if moved_since:
            shell.notify(
                "warning",
                f"This charge has moved since it was flagged: it was in "
                f"{committee_name(flagged_id) or 'no committee'} and is now in {current_name}. "
                "It cannot be moved again from here; close the flag once you are satisfied.",
            )
        options = [VERDICT_KEEP, VERDICT_FIXED] if moved_since else [VERDICT_KEEP, VERDICT_MOVE, VERDICT_FIXED]
        # No default: a verdict that moves money should be chosen, not left selected.
        verdict = st.radio("Your decision", options, index=None, key=f"flag_verdict_{flag_id}")

        destination = None
        if verdict == VERDICT_MOVE:
            choices = [committee_label(c.id) for c in COMMITTEES if c.id != current_id]
            picked = st.selectbox(
                "Move it to",
                choices,
                index=None,
                placeholder="Choose a committee",
                key=f"flag_dest_{flag_id}",
            )
            destination = parse_committee_label(picked) if picked else None

        reply = st.text_input(
            "Reply to the VP (optional)", key=f"flag_reply_{flag_id}",
            max_chars=flags_domain.MAX_NOTE_LENGTH,
        )

        ready = verdict is not None and (verdict != VERDICT_MOVE or destination is not None)
        if not st.button(
            "Apply decision", type="primary", key=f"flag_apply_{flag_id}",
            disabled=not ready or auth.current_user().read_only,
        ):
            return

        if verdict == VERDICT_MOVE:
            result = repo.move_flagged_charge(flag_id, destination, actor, reply)
            done = f"Moved to {committee_name(destination)}. The flag is closed and every total has updated."
        elif verdict == VERDICT_KEEP:
            result = repo.resolve_flag(flag_id, flags_domain.DISMISSED, actor, reply)
            done = f"Flag closed. The charge stays with {current_name}."
        else:
            result = repo.resolve_flag(flag_id, flags_domain.RESOLVED, actor, reply)
            done = "Flag marked resolved."

        if result.error:
            # Left on screen. If the charge did move but the flag could not close, the
            # next run shows the "moved since it was flagged" notice and the treasurer
            # can simply close the flag.
            shell.error_state("Could not apply that decision", result.error)
        else:
            st.session_state[_FLASH] = done
            st.rerun()
