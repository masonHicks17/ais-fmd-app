"""
Treasury portal: budgets, terms and exports. (Statement upload moved to
Transactions -> Import; see views/ImportStatement.py.)
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import dues
from ais_fmd.domain import sponsorship
from ais_fmd.domain.terms import ordered_semesters, validate_semester_name
from ais_fmd.ui import charts, shell

identity = auth.require(auth.Role.TREASURER)

shell.environment_banner()
auth.read_only_notice(identity)
shell.page_header(
    "Treasury",
    "Allocate budgets, manage academic terms, and export the books.",
)

# Statement upload moved to Transactions → Import (ImportStatement.py).
tab_budgets, tab_terms, tab_export = st.tabs(["Budgets", "Terms", "Export"])


# ============================================================================
# BUDGETS
# ============================================================================

with tab_budgets:
    terms = repo.load_terms()
    if terms.empty:
        shell.empty_state("No terms defined", "Add a term first.")
    else:
        term_ids = terms.sort_values("start_date", ascending=False)["TermID"].tolist()
        semester_by_id = dict(zip(terms["TermID"], terms["Semester"]))

        selected_term = st.selectbox(
            "Term",
            term_ids,
            format_func=lambda value: f"{value} — {semester_by_id.get(value, '')}",
            key="budget_term",
        )

        budgets = repo.load_budgets()
        current = {}
        if not budgets.empty:
            scoped = budgets[budgets["termid"] == selected_term]
            current = dict(zip(scoped["committeeid"], scoped["budget_amount"]))

        st.caption(
            "Allocations are upserted per committee. Unlike the original, existing "
            "rows are not deleted first, so there is no window where the term has "
            "no budget and no history is destroyed."
        )

        with st.form("budget_form"):
            inputs: dict[int, float] = {}
            columns = st.columns(3)
            for position, committee_id in enumerate(BUDGETED_COMMITTEE_IDS):
                with columns[position % 3]:
                    inputs[committee_id] = st.number_input(
                        committee_name(committee_id),
                        min_value=0.0,
                        value=float(current.get(committee_id, 0.0) or 0.0),
                        step=50.0,
                        format="%.2f",
                        key=f"budget_{selected_term}_{committee_id}",
                        disabled=identity.read_only,
                    )
            saved = st.form_submit_button("Save budgets", type="primary", disabled=identity.read_only)

        if saved:
            result = repo.upsert_budgets(selected_term, inputs, identity.email)
            if result.error:
                shell.error_state("Could not save budgets", result.error)
            else:
                st.success(
                    f"Saved. {result.updated} allocation(s) changed, "
                    f"{result.unchanged} already matched."
                )
                st.rerun()

# ============================================================================
# TERMS
# ============================================================================

with tab_terms:
    terms = repo.load_terms()
    if not terms.empty:
        view = terms.sort_values("start_date", ascending=False).copy()
        view["Status"] = (
            view["locked"].fillna(0).astype(int).map({1: "closed", 0: "open"})
            if "locked" in view.columns
            else "open"
        )
        view["Dues rates"] = (
            view["dues_rates"].fillna("—") if "dues_rates" in view.columns else "—"
        )
        view["Rates confirmed"] = (
            view["dues_rates_verified"].fillna(0).astype(int).map({1: "yes", 0: "no"})
            if "dues_rates_verified" in view.columns
            else "no"
        )
        shell.dataframe(
            view[
                ["TermID", "Semester", "start_date", "end_date", "Status",
                 "Dues rates", "Rates confirmed"]
            ].rename(
                columns={"TermID": "Term ID", "start_date": "Start", "end_date": "End"}
            )
        )

    # --- Per-term dues rates -------------------------------------------------

    st.markdown("#### Dues rates for a term")
    shell.say(
        "Dues are matched on an exact amount. The rate has changed nearly every "
        "term historically, and a term charging a rate the app does not know "
        "about stops categorizing dues altogether — no error, the payments just "
        "land in the review queue. Every term currently carries rates copied "
        "from the old built-in default, marked unconfirmed until someone checks "
        "them against what was actually charged."
    )

    if terms.empty:
        shell.empty_state("No terms yet")
    else:
        ordered_terms = terms.sort_values("start_date", ascending=False)
        semester_names = dict(zip(ordered_terms["TermID"], ordered_terms["Semester"]))
        with st.form("dues_rates_form"):
            rate_columns = st.columns([2, 2, 1])
            with rate_columns[0]:
                rate_term = st.selectbox(
                    "Term",
                    ordered_terms["TermID"].tolist(),
                    format_func=lambda value: f"{value} — {semester_names.get(value, '')}",
                    key="dues_rate_term",
                )
            current = ordered_terms[ordered_terms["TermID"] == rate_term]
            existing = ""
            already_confirmed = False
            if not current.empty:
                raw = current.iloc[0].get("dues_rates")
                existing = "" if raw is None or pd.isna(raw) else str(raw)
                flag = current.iloc[0].get("dues_rates_verified")
                already_confirmed = bool(0 if flag is None or pd.isna(flag) else int(flag))
            with rate_columns[1]:
                entered = st.text_input(
                    "Rates charged (comma separated)",
                    value=existing,
                    placeholder="35.00,52.50",
                    key="dues_rate_values",
                    disabled=identity.read_only,
                )
            with rate_columns[2]:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                confirmed = st.checkbox(
                    "Confirmed", value=already_confirmed, key="dues_rate_confirmed",
                    disabled=identity.read_only,
                )
            if st.form_submit_button("Save rates", type="primary", disabled=identity.read_only):
                parsed = dues.parse_rates(entered)
                if entered.strip() and not parsed:
                    shell.error_state(
                        "Could not read those rates",
                        "Enter amounts separated by commas, for example 35.00,52.50.",
                    )
                else:
                    result = repo.set_term_dues_rates(
                        rate_term,
                        dues.format_rates(parsed) if parsed else "",
                        confirmed,
                        identity.email,
                    )
                    if result.error:
                        shell.error_state("Could not save the rates", result.error)
                    elif result.unchanged:
                        shell.notify("info", f"{rate_term} already had those rates.")
                    else:
                        st.success(f"Saved dues rates for {rate_term}.")
                        st.rerun()

    # --- Dues price dates (domain/dues_checks.py) -----------------------------

    st.markdown("#### Dues price dates")
    shell.say(
        "When the price goes up during the term, and when a payment counts as late. "
        "The Dues page checks every payment against the price for its date: the early "
        "price after the increase is underpaid, two prices added up is someone paying "
        "for another member, and anything after the late cutoff gets a second look."
    )
    if not terms.empty:
        # The term picker sits outside the form: inside one, choosing another term
        # would not reload that term's stored dates until the form was submitted.
        schedule_term = st.selectbox(
            "Term",
            ordered_terms["TermID"].tolist(),
            format_func=lambda value: f"{value} — {semester_names.get(value, '')}",
            key="dues_schedule_term",
        )
        row = ordered_terms[ordered_terms["TermID"] == schedule_term].iloc[0].to_dict()
        with st.form(f"dues_schedule_form_{schedule_term}"):

            def _stored_price(field: str) -> float | None:
                value = row.get(field)
                return None if value is None or pd.isna(value) else float(value)

            def _stored_day(field: str):
                value = pd.to_datetime(row.get(field), errors="coerce")
                return None if pd.isna(value) else value.date()

            price_columns = st.columns(4)
            early_price = price_columns[0].number_input(
                "Early price", min_value=0.0, step=5.0, format="%.2f",
                value=_stored_price("dues_early_price"), placeholder="50.00", disabled=identity.read_only,
            )
            increase_date = price_columns[1].date_input(
                "Early price through", value=_stored_day("dues_increase_date"),
                help="The last day the early price applies, inclusive.", disabled=identity.read_only,
            )
            late_price = price_columns[2].number_input(
                "Then", min_value=0.0, step=5.0, format="%.2f",
                value=_stored_price("dues_late_price"), placeholder="65.00", disabled=identity.read_only,
            )
            late_cutoff = price_columns[3].date_input(
                "Late after", value=_stored_day("dues_late_cutoff"),
                help="Payments after this date are flagged: late dues, or paying for someone else?", disabled=identity.read_only,
            )
            if st.form_submit_button("Save price dates", type="primary", disabled=identity.read_only):
                if (early_price is None) != (late_price is None) or (
                    early_price is not None and increase_date is None
                ):
                    shell.error_state(
                        "Fill in both prices and the date",
                        "The checks need the early price, the date it ends, and the price after.",
                    )
                else:
                    result = repo.set_term_dues_schedule(
                        schedule_term,
                        early_price,
                        late_price,
                        increase_date.isoformat() if increase_date else None,
                        late_cutoff.isoformat() if late_cutoff else None,
                        identity.email,
                    )
                    if result.error:
                        shell.error_state("Could not save the price dates", result.error)
                    elif result.unchanged:
                        shell.notify("info", f"{schedule_term} already had those dates.")
                    else:
                        st.success(f"Saved dues price dates for {schedule_term}.")
                        st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    # --- Per-term sponsorship goal (M20) --------------------------------------

    st.markdown("#### Sponsorship goal for a term")
    shell.say(
        "How much sponsorship/donation money the org is aiming to raise this "
        "term. Set once here; Home (This term in detail) shows how collection is "
        "tracking against it, the same way it does for dues."
    )

    if terms.empty:
        shell.empty_state("No terms yet")
    else:
        ordered_for_goal = terms.sort_values("start_date", ascending=False)
        goal_semester_names = dict(
            zip(ordered_for_goal["TermID"], ordered_for_goal["Semester"])
        )
        with st.form("sponsorship_goal_form"):
            goal_columns = st.columns([2, 2, 1])
            with goal_columns[0]:
                goal_term = st.selectbox(
                    "Term",
                    ordered_for_goal["TermID"].tolist(),
                    format_func=lambda value: f"{value} — {goal_semester_names.get(value, '')}",
                    key="sponsorship_goal_term",
                )
            current_goal_row = ordered_for_goal[ordered_for_goal["TermID"] == goal_term]
            existing_goal = 0.0
            goal_already_unset = True
            if not current_goal_row.empty and "sponsorship_goal" in current_goal_row.columns:
                raw_goal = current_goal_row.iloc[0].get("sponsorship_goal")
                if raw_goal is not None and not pd.isna(raw_goal):
                    existing_goal = float(raw_goal)
                    goal_already_unset = False
            with goal_columns[1]:
                entered_goal = st.number_input(
                    "Goal ($)",
                    min_value=0.0,
                    value=existing_goal,
                    step=100.0,
                    format="%.2f",
                    key="sponsorship_goal_value",
                    disabled=identity.read_only,
                )
            with goal_columns[2]:
                st.markdown("&nbsp;", unsafe_allow_html=True)
                clear_goal = st.checkbox(
                    "No goal", value=goal_already_unset, key="sponsorship_goal_clear",
                    disabled=identity.read_only,
                )
            if st.form_submit_button("Save goal", type="primary", disabled=identity.read_only):
                result = repo.set_term_sponsorship_goal(
                    goal_term,
                    None if clear_goal else entered_goal,
                    identity.email,
                )
                if result.error:
                    shell.error_state("Could not save the sponsorship goal", result.error)
                elif result.unchanged:
                    shell.notify("info", f"{goal_term} already had that goal.")
                else:
                    st.success(f"Saved sponsorship goal for {goal_term}.")
                    st.rerun()

    # --- Sponsorship history, all terms at a glance ---------------------------

    st.markdown("##### Sponsorship history")
    shell.say(
        "Every term with its goal and how much actually came in -- so a goal "
        "typo or an old target is easy to spot without switching terms one "
        "at a time on Home.",
        caption=True,
    )

    if terms.empty:
        shell.empty_state("No terms yet")
    else:
        history_semesters = terms.sort_values("start_date", ascending=False)["Semester"].tolist()
        history = sponsorship.compare_semesters(
            repo.load_transactions(), terms, history_semesters
        )

        history_view = st.radio(
            "History view",
            ["Chart", "Table"],
            horizontal=True,
            key="sponsorship_history_view",
            label_visibility="collapsed",
        )

        if history_view == "Chart":
            # Chart view stays chart-only, deliberately -- no payment
            # drill-down here. Oldest-to-newest reads left-to-right, the
            # opposite order from the table below (which leads with the most
            # recent term); a trend chart should run chronologically, same
            # convention as the Dashboard's "Across semesters" chart.
            shell.chart(
                charts.sponsorship_trend_bars(history.iloc[::-1]),
                key="sponsorship_history_chart",
            )
        else:
            display_history = history.copy()
            display_history["Goal"] = display_history["Goal"].map(
                lambda value: "—" if pd.isna(value) else f"${value:,.2f}"
            )
            display_history["Raised"] = display_history["Raised"].map(
                lambda value: f"${value:,.2f}"
            )
            display_history["% of Goal"] = display_history["% of Goal"].map(
                lambda value: "—" if pd.isna(value) else f"{value:.0f}%"
            )
            shell.dataframe(display_history)

            # --- Drill-down: who actually sponsored, per semester -------------
            #
            # The row above answers "how much" -- this answers "from whom",
            # and lets a treasurer type in the sponsor's name against a
            # payment (transactions.sponsor_name -- bank text alone never
            # carries one). Table-view only, per feedback that Chart should
            # stay compact; only semesters with a payment get an expander,
            # since a $0 term already says so above and has nothing to
            # expand into.
            transactions_bundle = repo.load_transactions()
            with_payments = history[history["Payments"] > 0]
            if not with_payments.empty:
                st.markdown("###### Individual payments")
                for _, row in with_payments.iterrows():
                    semester = row["Semester"]
                    detail = sponsorship.transactions_for_semester(
                        transactions_bundle, terms, semester
                    )
                    original_sponsors = dict(zip(detail["transactionid"], detail["Sponsor"]))

                    with st.expander(f"{semester} — {len(detail)} payment(s)"):
                        editable_detail = detail.copy()
                        editable_detail["Date"] = editable_detail["Date"].dt.strftime("%Y-%m-%d")
                        edited_detail = st.data_editor(
                            editable_detail,
                            key=f"sponsor_editor_{semester}",
                            hide_index=True,
                            disabled=["transactionid", "Date", "Amount", "Details"],
                            column_config={
                                "transactionid": None,  # identity only, not shown
                                "Amount": st.column_config.NumberColumn(format="dollar"),
                                "Details": st.column_config.TextColumn(width="large"),
                                "Sponsor": st.column_config.TextColumn(
                                    "Sponsor", help="Who this payment was actually from."
                                ),
                            },
                        )

                        sponsor_changes = [
                            (int(edited_row["transactionid"]), edited_row["Sponsor"])
                            for _, edited_row in edited_detail.iterrows()
                            if (edited_row["Sponsor"] or None)
                            != (original_sponsors.get(edited_row["transactionid"]) or None)
                        ]
                        if st.button(
                            f"Save {len(sponsor_changes)} sponsor name"
                            f"{'s' if len(sponsor_changes) != 1 else ''}",
                            key=f"save_sponsors_{semester}",
                            disabled=not sponsor_changes,
                        ):
                            errors = []
                            for transaction_id, sponsor_name in sponsor_changes:
                                result = repo.set_transaction_sponsor_name(
                                    transaction_id, sponsor_name, identity.email
                                )
                                if result.error:
                                    errors.append(result.error)
                            if errors:
                                shell.error_state("Some sponsor names could not be saved", errors[0])
                            else:
                                st.success("Saved.")
                                st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    # --- Period locking (M10) -----------------------------------------------

    st.markdown("#### Close or reopen a term")
    st.caption(
        "Closing a term makes its transactions read-only — imports and edits "
        "dated inside it are refused. The check lives in the data layer, so no "
        "page can bypass it. Close a term once it has been reconciled and "
        "reported, so a later edit cannot silently change a published figure."
    )

    if terms.empty:
        shell.empty_state("No terms to close yet")
    else:
        lock_columns = st.columns([2, 1, 2])
        with lock_columns[0]:
            ordered = terms.sort_values("start_date", ascending=False)
            semester_by_term = dict(zip(ordered["TermID"], ordered["Semester"]))
            lock_target = st.selectbox(
                "Term",
                ordered["TermID"].tolist(),
                format_func=lambda value: f"{value} — {semester_by_term.get(value, '')}",
                key="lock_term",
            )

        currently_locked = False
        if "locked" in terms.columns:
            match = terms[terms["TermID"] == lock_target]
            if not match.empty:
                currently_locked = bool(int(match.iloc[0]["locked"] or 0))

        with lock_columns[1]:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown(
                shell.pill("closed" if currently_locked else "open",
                           "over" if currently_locked else "on track"),
                unsafe_allow_html=True,
            )

        with lock_columns[2]:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            action = "Reopen term" if currently_locked else "Close term"
            if st.button(action, key="toggle_lock", type="primary", disabled=identity.read_only):
                result = repo.set_term_lock(lock_target, not currently_locked, identity.email)
                if result.error:
                    shell.error_state("Could not change the term", result.error)
                else:
                    st.success(
                        f"{lock_target} is now "
                        f"{'open' if currently_locked else 'closed'}."
                    )
                    st.rerun()

    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    st.markdown("#### Add a term")
    with st.form("term_form"):
        columns = st.columns(2)
        with columns[0]:
            term_id = st.text_input("Term ID", placeholder="FA26", disabled=identity.read_only)
            semester = st.text_input("Semester name", placeholder="Fall 2026", disabled=identity.read_only)
        with columns[1]:
            start_date = st.date_input("Start date", disabled=identity.read_only)
            end_date = st.date_input("End date", disabled=identity.read_only)
        add_clicked = st.form_submit_button("Add term", type="primary", disabled=identity.read_only)

    if add_clicked:
        valid, message = validate_semester_name(semester)
        if not term_id.strip():
            shell.error_state("Term ID is required")
        elif not valid:
            shell.error_state("Semester name is not valid", message)
        elif end_date <= start_date:
            shell.error_state("End date must be after the start date")
        else:
            result = repo.insert_term(
                term_id.strip(),
                semester.strip().title(),
                start_date.isoformat(),
                end_date.isoformat(),
                identity.email,
            )
            if result.error:
                shell.error_state("Could not add the term", result.error)
            else:
                st.success(f"Added {term_id.strip()}.")
                st.rerun()

# ============================================================================
# EXPORT
# ============================================================================

with tab_export:
    st.markdown("#### Export data")
    bundle = repo.load_bundle()
    exports = {
        "transactions": bundle.transactions,
        "budgets": bundle.budgets,
        "terms": bundle.terms,
        "committees": bundle.committees,
        "merchants": repo.load_merchants(),
        "audit_log": repo.load_audit(10_000),
    }

    columns = st.columns(3)
    for position, (name, frame) in enumerate(exports.items()):
        with columns[position % 3]:
            st.download_button(
                f"{name} ({len(frame):,})",
                frame.to_csv(index=False),
                file_name=f"{name}.csv",
                mime="text/csv",
                key=f"export_{name}",
                width="stretch",
            )

    st.caption(
        "Downloads are generated in the browser session. In sandbox mode these "
        "come from the local SQLite file."
    )
