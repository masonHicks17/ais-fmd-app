"""Module M21 -- membership roster upload and dues reconciliation."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import roster as roster_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import (
    default_semester_index,
    ordered_semesters,
    term_id_for_semester,
)
from ais_fmd.ui import shell

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Roster & dues reconciliation",
    "Upload the membership list, then compare it against who has actually paid. "
    "Payments made on someone else's behalf are credited to the person the memo "
    "names, not the person who sent the money.",
)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)

if not semesters:
    shell.empty_state("No terms defined", "Add an academic term first, on Treasury.")
    st.stop()

selected = st.selectbox(
    "Semester",
    semesters,
    index=default_semester_index(bundle.transactions, bundle.terms),
    key="roster_semester",
)

terms = bundle.terms
term_id = term_id_for_semester(terms, selected) or ""

# --- Upload ------------------------------------------------------------------

with st.expander("Upload a membership list", expanded=False):
    shell.say(
        "A CSV or Excel export of the membership sheet. It needs a name column "
        "— either one full-name column, or separate first and last name columns. "
        "Email and UFID are picked up when present and just make the chase list "
        "more useful. Columns are matched by their heading, never by position."
    )
    uploaded = st.file_uploader(
        "Membership list",
        type=["csv", "xlsx", "xls"],
        key="roster_upload",
    )

    if uploaded is not None:
        try:
            if uploaded.name.lower().endswith(".csv"):
                raw = pd.read_csv(uploaded)
            else:
                raw = pd.read_excel(uploaded)
        except Exception as exc:  # noqa: BLE001 - a bad file must not crash the page
            raw = None
            shell.error_state("Could not read that file", f"{type(exc).__name__}: {exc}")

        if raw is not None:
            parsed = roster_domain.parse_roster(raw)
            for warning in parsed.warnings:
                shell.notify("warning", warning)
            for error in parsed.errors:
                shell.error_state("Roster not imported", error)

            if parsed.ok:
                shell.notify(
                    "info",
                    f"Read **{len(parsed.members)}** members from column "
                    f"`{parsed.name_column}`.",
                )
                shell.dataframe(
                    pd.DataFrame(
                        [
                            {"Name": m.full_name, "Email": m.email, "UFID": m.ufid}
                            for m in parsed.members[:10]
                        ]
                    ),
                    hide_index=True,
                )
                shell.say(
                    f"Saving replaces the whole roster for **{selected}**. A name "
                    "not in this file is treated as having left.",
                    caption=True,
                )
                if st.button(f"Save roster for {selected}", type="primary", disabled=identity.read_only):
                    # `save_result`, not `result`: see the note on `alias_result`
                    # below. `result` is the page's reconciliation object.
                    save_result = repo.replace_members(
                        term_id,
                        [
                            {
                                "full_name": m.full_name,
                                "match_key": m.match_key,
                                "alt_keys": m.alt_keys,
                                "preferred_name": m.preferred_name,
                                "claims_paid": m.claims_paid,
                                "email": m.email,
                                "ufid": m.ufid,
                            }
                            for m in parsed.members
                        ],
                        uploaded.name,
                        identity.email,
                    )
                    if save_result.ok:
                        repo.invalidate()
                        shell.notify(
                            "success",
                            f"Saved {save_result.updated} members to {selected}.",
                        )
                        st.rerun()
                    else:
                        shell.error_state(
                            "Could not save the roster", save_result.error or ""
                        )

# --- Reconciliation ----------------------------------------------------------

stored = repo.load_members(term_id)
if stored.empty:
    shell.empty_state(
        f"No roster stored for {selected}",
        "Upload a membership list above to see who has not paid.",
    )
    st.stop()

members = roster_domain.members_from_rows(stored)
paid_rows = roster_domain.semester_dues_rows(bundle.transactions, terms, selected)

result = roster_domain.reconcile(paid_rows, members)
summary = result.summary()

shell.metric_row(
    [
        # currency=False on the counts: these are people, and the shell formats
        # as money unless told otherwise.
        {"label": "On the roster", "value": f"{summary['expected']:,}", "currency": False},
        {"label": "Paid", "value": f"{summary['paid']:,}", "currency": False},
        {"label": "Outstanding", "value": f"{summary['unpaid']:,}", "currency": False},
        {"label": "Collection rate", "value": f"{summary['rate']:.0f}%", "currency": False},
        {
            "label": "Collected",
            "value": format_currency(summary["collected"]),
            "currency": False,
        },
    ]
)

if summary["unmatched_payments"]:
    shell.notify(
        "warning",
        f"**{summary['unmatched_payments']}** dues payment(s) totalling "
        f"{format_currency(summary['unmatched_amount'])} name nobody on this "
        "roster. The money is counted as income; the person is unknown. Either "
        "they are missing from the list, or the memo needs a human to read it.",
    )

if summary["on_behalf"]:
    shell.notify(
        "info",
        f"**{summary['on_behalf']}** payment(s) were credited to someone other "
        "than the sender, because the memo named them.",
    )

suggestions = result.suggestions()
if suggestions:
    shell.notify(
        "info",
        f"**{len(suggestions)}** unpaid member(s) look like they match a payment "
        "whose name is spelled differently. Nothing has been credited — see the "
        "**Likely matches** tab.",
    )

if summary["disputed"]:
    shell.notify(
        "warning",
        f"**{summary['disputed']}** member(s) certified on the form that they "
        "paid, but no payment could be found for them.",
    )

(
    outstanding_tab,
    suggested_tab,
    disputed_tab,
    paid_tab,
    unmatched_tab,
    flags_tab,
) = st.tabs(
    [
        "Has not paid",
        f"Likely matches ({len(suggestions)})",
        f"Says they paid ({summary['disputed']})",
        "Credited payments",
        "Unmatched payments",
        "Paid more than once",
    ]
)

with suggested_tab:
    if not suggestions:
        shell.notify("success", "No near-misses to resolve.")
    else:
        shell.say(
            "Each row is an unpaid member next to a payment that names them with "
            "a different spelling — a shortened first name, or a stray plural. "
            "Nothing here is credited automatically: a matcher loose enough to "
            "accept these is loose enough to credit the wrong person's dues. "
            "Confirming one teaches the roster that spelling permanently, so the "
            "next statement matches it directly."
        )
        for payment, member in suggestions:
            alias = roster_domain.suggested_alias(payment, member)
            cols = st.columns([3, 1.2, 1.6, 2.6, 1.2])
            cols[0].markdown(f"**{member.display_name}**")
            cols[1].markdown(format_currency(payment.amount))
            cols[2].markdown(payment.payer_name)
            cols[3].markdown(f"*{roster_domain.extract_memo(payment.details) or '—'}*")
            if cols[4].button("Confirm", key=f"confirm_{member.match_key}_{payment.index}", disabled=identity.read_only):
                # Deliberately NOT `result`: that name holds the page's
                # reconciliation object, which every tab below still reads. On
                # the failure path this function returns and the script carries
                # on, so rebinding it turned a failed alias save into an
                # AttributeError two tabs later.
                alias_result = repo.add_member_alias(
                    term_id, member.match_key, alias, identity.email
                )
                if alias_result.ok:
                    repo.invalidate()
                    shell.notify(
                        "success",
                        f"Taught the roster '{payment.payer_name}' = "
                        f"{member.display_name}.",
                    )
                    st.rerun()
                else:
                    shell.error_state("Could not save that alias", alias_result.error or "")

with disputed_tab:
    disputed = roster_domain.disputed_frame(result)
    if disputed.empty:
        shell.notify(
            "success", "Nobody claims to have paid without a matching payment."
        )
    else:
        shell.say(
            "These members ticked the box on the membership form saying they had "
            "completed the dues transaction, but no payment matches them. Each is "
            "one of three things: paid by a method this statement does not cover "
            "(cash, or the Venmo account being retired), paid under a name the "
            "matcher could not connect, or did not actually pay."
        )
        shell.dataframe(disputed, hide_index=True)
        st.download_button(
            "Download list (CSV)",
            disputed.to_csv(index=False).encode("utf-8"),
            file_name=f"disputed-{term_id or 'term'}.csv",
            mime="text/csv",
        )

with outstanding_tab:
    unpaid = roster_domain.unpaid_frame(result)
    if unpaid.empty:
        shell.notify("success", "Everyone on the roster has paid.")
    else:
        shell.say(f"**{len(unpaid)}** members with no payment recorded.")
        shell.dataframe(unpaid, hide_index=True)
        st.download_button(
            "Download chase list (CSV)",
            unpaid.to_csv(index=False).encode("utf-8"),
            file_name=f"unpaid-{term_id or 'term'}.csv",
            mime="text/csv",
        )

with paid_tab:
    matched = roster_domain.matched_frame(result)
    matched = matched[matched["Credited to"] != ""]
    if matched.empty:
        shell.empty_state("No payments matched to a roster member yet")
    else:
        shell.dataframe(matched, hide_index=True)

with unmatched_tab:
    unmatched = roster_domain.matched_frame(result)
    unmatched = unmatched[unmatched["Credited to"] == ""]
    if unmatched.empty:
        shell.notify("success", "Every dues payment was matched to a member.")
    else:
        shell.say(
            "These payments could not be tied to anyone on the roster. Nothing "
            "is wrong with the money — only the attribution is missing."
        )
        shell.dataframe(unmatched, hide_index=True)

with flags_tab:
    repeats = result.duplicates()
    if not repeats:
        shell.notify("success", "No member was credited by more than one payment.")
    else:
        shell.say(
            "Usually an instalment or a correction, occasionally a genuine "
            "double charge worth refunding."
        )
        shell.dataframe(
            pd.DataFrame(
                [
                    {
                        "Member": member.full_name,
                        "Payments": count,
                        "Total": total,
                    }
                    for member, count, total in repeats
                ]
            ),
            hide_index=True,
        )
