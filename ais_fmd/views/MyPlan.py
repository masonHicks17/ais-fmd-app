"""
My Plan -- a VP's basic budget planner.

One line per kind of spending (Food & Drink, Social Events, ...) with a planned
amount. What has been spent in each fills in from the ledger, so the sheet reads
Planned / Spent / Left, with a bar chart of plan against spending and a donut of
where the plan goes. Past spending can fill the amounts in, and the VP edits
from there.

This semester: does the plan fit the budget, and how is each category tracking?
Next semester: what the same season has cost before, a plan sheet compared with
last time, and a budget request for the treasurer.

For the committee VPs only: the page is in `nav.VP_PAGES` and not in the
treasurer's navigation. The committee comes from the signed-in profile, never
from a widget, so there is nothing a VP could change to see another committee's
plan. A plan is an estimate -- nothing here books, moves or reserves money --
and a request is a message, like a flag.
"""

from __future__ import annotations

from datetime import date
from html import escape

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.config import vp_committees
from ais_fmd.config.categories import PURPOSES, committee_name
from ais_fmd.data import repositories as repo
from ais_fmd.domain import budgets as budget_domain
from ais_fmd.domain import committee_plan as plan
from ais_fmd.domain import event_ideas
from ais_fmd.domain import events as events_domain
from ais_fmd.domain import vp_metrics
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import date_range_for_semester, default_semester, ordered_semesters
from ais_fmd.ui import calendar_grid, committee_charts, plan_charts, shell

identity = auth.require(auth.Role.OFFICER)

shell.environment_banner()
shell.page_header("My Plan", "A simple budget for your committee: plan each kind of spending, then track it.")

_FLASH = "my_plan_flash"
_MONEY = st.column_config.NumberColumn(format="dollar")
flash = st.session_state.pop(_FLASH, None)
if flash:
    shell.notify("success", flash)

bundle = repo.load_bundle()
semesters = ordered_semesters(bundle.terms)
current = default_semester(bundle.transactions, bundle.terms)
if not semesters or current is None:
    shell.empty_state("No terms defined yet", "The treasurer sets terms up on the Treasury page.")
    st.stop()
upcoming = plan.next_semester_name(current)

# --- Which committee ----------------------------------------------------------

if identity.committee_id is None:
    shell.empty_state(
        "No committee assigned to your account",
        " Ask the treasurer to add your email under Officer Access.",
    )
    st.stop()

committee_id = identity.committee_id
line_ids = vp_committees.budget_ids_for(committee_id)
line_names = [committee_name(cid) for cid in line_ids]
title = vp_committees.title_for(committee_id)

# The page is only in a VP's navigation, but the role check keeps it honest: a
# read-only identity is also refused by the write layer.
can_edit = identity.committee_scoped
own = (int(committee_id),)

charges = bundle.transactions
charges = charges[charges["budget_category"].isin(line_ids)] if not charges.empty else charges


def _load(loader, *args) -> pd.DataFrame | None:
    """The saved rows, or None when this deployment cannot store plans yet."""
    try:
        return loader(*args)
    except Exception:  # noqa: BLE001 - a missing table is a deployment state, not an error
        return None


def _position(semester: str) -> dict:
    summary = budget_domain.budget_vs_actual(bundle.transactions, bundle.budgets, bundle.terms, semester)
    return vp_metrics.rollup(summary, line_names)


def _spent_in(semester: str) -> pd.DataFrame:
    window = date_range_for_semester(bundle.terms, semester)
    if window is None:
        return pd.DataFrame(columns=["Purpose", "Amount"])
    return plan.spending_by_purpose(charges, window[0], window[1])


def _save(semester: str, lines: list[dict], message: str) -> None:
    result = repo.replace_plan_lines(committee_id, semester, lines, own, identity.email)
    if result.error:
        shell.notify("warning", result.error)
        return
    st.session_state[_FLASH] = message
    st.rerun()


def _plan_editor(semester: str, saved: pd.DataFrame, sheet: pd.DataFrame, compare: str, key: str) -> None:
    """
    The budget sheet: Category and Planned are typed; `compare` (this term's
    Spent, or last time's spending for next term) and Left fill in.
    """
    notes = dict(zip(saved["purpose"], saved["note"])) if not saved.empty else {}
    frame = pd.DataFrame({
        "purpose": sheet["Category"],
        "amount": sheet["Planned"],  # $0.00 for a category with spending but no plan, not "None"
        "compare": sheet[compare],
        "left": sheet["Planned"] - sheet[compare] if compare == "Spent" else None,
        "note": ["" if pd.isna(notes.get(c)) else str(notes.get(c)) for c in sheet["Category"]],
    })
    # An empty plan gives empty columns pandas types as numbers; the editor needs
    # text for the text columns and numbers for the money ones.
    frame = frame.astype({"purpose": "object", "note": "object", "amount": "float64", "compare": "float64"})
    # The VP's own category names saved earlier come back as choices too.
    options = [*PURPOSES, *plan.custom_categories(saved)]
    config = {
        "purpose": st.column_config.TextColumn("Category"),
        "amount": st.column_config.NumberColumn(
            "Planned", min_value=0.0, max_value=plan.MAX_AMOUNT, step=10.0, format="dollar"
        ),
        "compare": st.column_config.NumberColumn(compare, format="dollar", disabled=True),
        "left": st.column_config.NumberColumn("Left", format="dollar", disabled=True),
        "note": st.column_config.TextColumn("Note", max_chars=plan.MAX_NOTE_LENGTH),
    }
    order = ["purpose", "amount", "compare", *(["left"] if compare == "Spent" else []), "note"]

    if not can_edit:
        shell.dataframe(frame, column_config=config, column_order=order)
        return

    # Pick-or-type: the only table cell Streamlit lets a user both choose from a
    # list and type a new value into is the multiselect one, so Category is a
    # one-item list here. `clean_lines` unwraps it and refuses a second pick.
    frame["purpose"] = pd.Series(
        [[c] if isinstance(c, str) and c else [] for c in frame["purpose"]], index=frame.index, dtype="object"
    )
    config["purpose"] = st.column_config.MultiselectColumn(
        "Category", options=options, accept_new_options=True,
        help="Pick one from the list, or type your own and press Enter.",
    )

    frame["remove"] = False
    config["remove"] = st.column_config.CheckboxColumn("Remove", default=False)
    edited = st.data_editor(
        frame, column_config=config, column_order=["remove", *order],
        num_rows="dynamic", hide_index=True, width="stretch", key=f"{key}_editor",
    )
    st.caption(
        "Add a category in the bottom row: pick one from the list, or type your own and press "
        "Enter. Tick Remove (or clear Planned) to drop one. Then Save plan."
    )
    if compare == "Spent" and plan.custom_categories(saved):
        st.caption("Bank charges are only ever tagged with the listed categories, so your own ones show $0 spent.")
    if st.button("Save plan", key=f"{key}_save", type="primary"):
        kept, removed = plan.drop_removed(edited)
        # A row with no planned amount is a category the VP isn't planning (for
        # example one that only has spending): it isn't saved, and isn't an error.
        amounts = pd.to_numeric(kept["amount"], errors="coerce").fillna(0)
        lines, problems = plan.clean_lines(kept[amounts > 0])
        if problems:
            for problem in problems:
                shell.notify("warning", problem)
            return
        total = sum(line["amount"] for line in lines)
        _save(semester, lines, f"Saved {len(lines)} categor{'y' if len(lines) == 1 else 'ies'} "
                               f"({format_currency(total)}) for {semester}.")


def _charts(sheet: pd.DataFrame, compare: str, label: str, key: str) -> None:
    if sheet.empty:
        return
    bars, ring = st.columns([3, 2], gap="medium")
    with bars:
        st.markdown(f"**Planned vs {label.lower()}**")
        shell.chart(
            # Only this term's real spending can be "over plan"; last season's can't.
            plan_charts.plan_vs_actual(sheet, actual=compare, actual_label=label, flag_over=compare == "Spent"),
            key=f"{key}_bars",
        )
    with ring:
        st.markdown("**Where the plan goes**")
        shell.chart(plan_charts.plan_donut(plan.donut_slices(sheet)), key=f"{key}_donut")


def _from_history(semester: str, saved: pd.DataFrame, bases: dict[str, str], key: str) -> None:
    """Fill the planned amounts in from what a past semester actually spent."""
    if not can_edit or not bases:
        return
    with st.expander("Fill in from past spending"):
        basis = st.radio("Based on", list(bases), key=f"{key}_basis", horizontal=True)
        past = bases[basis]
        by_purpose = _spent_in(past)
        if by_purpose.empty:
            st.caption(f"{title} has no spending recorded in {past}, so there's nothing to copy.")
            return
        st.caption(
            f"What {title} spent in {past}, by category, rounded up to $5. Using it sets those "
            f"categories' planned amounts and keeps the rest of your plan."
        )
        # Leave it empty for every category, or pick just the ones to fill in.
        chosen = st.multiselect(
            "Only these categories", list(by_purpose["Purpose"]), key=f"{key}_pick",
            placeholder="All categories",
        )
        picked = by_purpose[by_purpose["Purpose"].isin(chosen)] if chosen else by_purpose
        new_lines = plan.lines_from_history(picked)
        shell.dataframe(
            pd.DataFrame({
                "Category": picked["Purpose"].values,
                "Spent then": picked["Amount"].values,
                "Plan": [plan.round_up(float(amount)) for amount in picked["Amount"]],
            }),
            column_config={"Spent then": _MONEY, "Plan": _MONEY},
        )
        what = (
            "all categories" if not chosen
            else chosen[0] if len(chosen) == 1
            else f"{len(chosen)} categories"
        )
        total = sum(line["amount"] for line in new_lines)
        if st.button(f"Use for {what} ({format_currency(total)})", key=f"{key}_use"):
            existing, _ = plan.clean_lines(saved) if not saved.empty else ([], [])
            lines = plan.merge_lines(existing, new_lines)
            _save(semester, lines, f"Filled in {what} from {past}. Change any you like.")

# --- Events: the plan's backbone ---------------------------------------------------
#
# Added 2026-11-09. A VP plans a semester as a run of events, so the plan starts
# there: a calendar, the list of planned events (removing one asks first), and
# ideas from what this committee has already held -- what each kind cost, who
# came, and when it last happened. Game-day events (tailgates, watch parties)
# are planned as the kind, with past games as a hint, since opponents change.

TODAY = date.today()
_GAME_KINDS = event_ideas.GAME_KINDS


def _window(semester: str) -> tuple[date, date] | None:
    known = date_range_for_semester(bundle.terms, semester)
    if known is not None:
        return pd.Timestamp(known[0]).date(), pd.Timestamp(known[1]).date()
    return event_ideas.guess_window(semester)


def _save_events(semester: str, events: list[dict], message: str, key: str) -> None:
    result = repo.replace_planned_events(committee_id, semester, events, own, identity.email)
    if result.error:
        shell.notify("warning", result.error)
        return
    st.session_state.pop(f"{key}_pending", None)
    # A new version gives the two tables fresh widgets, so old ticks don't linger on screen.
    st.session_state[f"{key}_version"] = _version(key) + 1
    st.session_state[_FLASH] = message
    st.rerun()


def _version(key: str) -> int:
    return int(st.session_state.get(f"{key}_version", 0))


def _as_rows(planned: pd.DataFrame) -> list[dict]:
    rows = []
    for row in planned.to_dict("records"):
        rows.append({k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in row.items()
                     if k in ("event_date", "title", "kind", "estimated_cost", "expected_attendance", "note")})
    return rows


def _event_costs() -> dict:
    """event_id -> reported spend, from this committee's own charges linked to events."""
    try:
        links = repo.load_event_links()
    except Exception:  # noqa: BLE001 - no links table yet: nothing reported per event
        return {}
    spent = events_domain.event_spending(charges, links)
    return dict(zip(spent["event_id"], spent["Spent"].astype(float))) if not spent.empty else {}


def _dollars(amount: float | None) -> str:
    # Whole dollars, so four tiles side by side don't cut figures off.
    if amount is None:
        return "—"
    return f"{'-' if amount < 0 else ''}${abs(amount):,.0f}"


def _usual_extra() -> tuple[str, float] | None:
    """The cheapest-to-repeat thing a VP adds on a whim: a bar tab, else their cheapest costed kind."""
    all_events = repo.load_events()
    mine = all_events[all_events["committee_id"].isin(line_ids)] if not all_events.empty else all_events
    try:
        links = repo.load_event_links()
    except Exception:  # noqa: BLE001
        links = pd.DataFrame()
    table = event_ideas.ideas(mine, charges, links, today=TODAY)
    if table.empty:
        return None
    costed = table[pd.to_numeric(table["Average cost"], errors="coerce") > 0]
    if costed.empty:
        return None
    tab = costed[costed["Kind"] == "Bar tab"]
    row = (tab if not tab.empty else costed.sort_values("Average cost")).iloc[0]
    return str(row["Kind"]), float(row["Average cost"])


def _projection(budget: float | None, spent: float, planned: pd.DataFrame, semester: str) -> None:
    """Spent (reported) + planned events still ahead (estimate) = projected; the rest is room for extras."""
    figures = event_ideas.projection(budget, spent, planned, today=TODAY)
    room = figures["room"]
    tone = {"over": "over", "tight": "warn", "room": "ok"}.get(figures["status"])
    shell.metric_row([
        {"label": "Budget", "value": _dollars(budget), "currency": False},
        {"label": "Spent so far", "value": _dollars(spent), "currency": False},
        {"label": "Planned events ahead", "value": _dollars(figures["ahead"]), "currency": False},
        {"label": "Over the budget" if room is not None and room < 0 else "Room for extras",
         "value": _dollars(abs(room) if room is not None else None), "currency": False, "tone": tone},
    ])
    if not budget:
        shell.say(f"{title} has no budget for {semester}, so there's nothing to project against.", caption=True)
        return
    shell.html(plan_charts.projection_bar(figures, committee_charts.committee_color(committee_id)))
    ahead = figures["ahead_count"]
    events_word = f"{ahead} planned event{'s' if ahead != 1 else ''}"
    if figures["status"] == "over":
        shell.notify(
            "warning",
            f"What's been spent plus your {events_word} comes to {_dollars(figures['projected'])}, "
            f"{_dollars(-room)} over {title}'s budget. Trim an event or its estimate, or talk to the treasurer.",
        )
    else:
        extra = _usual_extra()
        line = (f"If your {events_word} cost what you estimated, {title} ends {semester} having spent "
                f"about {_dollars(figures['projected'])}, leaving **{_dollars(room)} for extras**")
        if extra and extra[1] > 0 and room >= extra[1]:
            count = int(room // extra[1])
            line += (f": room for about {count} more {extra[0].lower()}{'s' if count != 1 else ''} "
                     f"at your usual {_dollars(extra[1])}")
        line += "."
        if figures["status"] == "tight":
            line += " That's under a tenth of the budget, so keep spontaneous spending small."
        shell.say(line)
    notes = ["Spent is what the bank has reported. Planned events are only your estimates, "
             "and an event drops out of them once its date passes, because its real cost is in Spent by then."]
    if figures["passed_count"]:
        notes.append(f"{figures['passed_count']} planned event(s) have passed and no longer count.")
    st.caption(" ".join(notes))


def _plan_calendar(planned: pd.DataFrame, window: tuple[date, date] | None, key: str) -> None:
    if window is None:
        return
    months = list(dict.fromkeys(
        (d.year, d.month) for d in pd.date_range(window[0], window[1], freq="D").date
    ))
    labels = [f"{pd.Timestamp(year=y, month=m, day=1):%b %Y}" for y, m in months]
    default = next((label for (y, m), label in zip(months, labels) if (y, m) == (TODAY.year, TODAY.month)), labels[0])
    picked = st.segmented_control("Month", labels, default=default, key=f"{key}_month",
                                  label_visibility="collapsed") or default
    year, month = months[labels.index(picked)]

    color = committee_charts.committee_color(committee_id)
    chips: dict[date, list[calendar_grid.Chip]] = {}
    # What is already on the club calendar, for context: no money on it.
    club = repo.load_events()
    if not club.empty:
        club = club.assign(day=pd.to_datetime(club["event_date"], errors="coerce").dt.date)
        club = club[club["day"].map(lambda d: d is not None and not pd.isna(d) and d.year == year and d.month == month)]
        # What the bank reported for this committee's own events (linked purchases). Other
        # committees' events carry no money: a VP sees only their own figures.
        spent_on = _event_costs()
        for event in club.sort_values("day").itertuples():
            mine = pd.notna(event.committee_id) and int(event.committee_id) in line_ids
            cost = spent_on.get(event.event_id, 0.0) if mine else 0.0
            meta = [escape(event.start_time)] if isinstance(event.start_time, str) else []
            if cost > 0:
                meta.append(f'<span class="spent">spent &#36;{cost:,.0f}</span>')
            chips.setdefault(event.day, []).append(calendar_grid.Chip(
                title=str(event.title),
                meta_html=" · ".join(meta),
                color=committee_charts.committee_color(int(event.committee_id) if pd.notna(event.committee_id) else None),
                tip=f"On the club calendar: {event.title}" + (f" · reported spend {format_currency(cost)}" if cost else ""),
            ))
    for event in planned.itertuples():
        day = pd.to_datetime(event.event_date, errors="coerce")
        if pd.isna(day) or (day.year, day.month) != (year, month):
            continue
        passed = day.date() < TODAY
        meta = [f"plan &#36;{float(event.estimated_cost):,.0f}"]
        if passed:
            meta.append("date passed")
        elif pd.notna(event.expected_attendance):
            meta.append(f"~{int(event.expected_attendance)} people")
        tip = f"Planned: {event.title}" + (f" · {event.note}" if isinstance(event.note, str) else "")
        if passed:
            tip += " · The date has passed, so this estimate no longer counts: what it really cost is in Spent."
        chips.setdefault(day.date(), []).append(calendar_grid.Chip(
            title=str(event.title), meta_html=" · ".join(meta), color=color, planned=True, tip=tip,
        ))
    shell.html(calendar_grid.month_grid(year, month, chips, today=TODAY))
    undated = planned[pd.to_datetime(planned["event_date"], errors="coerce").isna()] if not planned.empty else planned
    caption = ("Dashed: your planned events, with their estimated cost. Solid: what's on the club calendar; "
               f"\"spent\" is what the bank reported for {title}'s own events.")
    if not undated.empty:
        caption += f" Not dated yet: {', '.join(undated['title'].astype(str))}."
    st.caption(caption)


def _plan_cards(planned: pd.DataFrame) -> None:
    """The planned events as cards, for phones; the table there scrolled sideways."""
    if planned.empty:
        return
    color = committee_charts.committee_color(committee_id)
    cards = []
    for event in planned.itertuples():
        day = pd.to_datetime(event.event_date, errors="coerce")
        when = f"{day:%a %b %d}" if pd.notna(day) else "Not dated yet"
        meta = [when, f"plan {format_currency(float(event.estimated_cost or 0))}"]
        if pd.notna(event.expected_attendance):
            meta.append(f"~{int(event.expected_attendance)} people")
        note = f"<em>{escape(str(event.note))}</em>" if isinstance(event.note, str) and event.note else ""
        cards.append(f'<div class="ais-plan-card" style="--c:{color}"><b>{escape(str(event.title))}</b>'
                     f'<span>{" · ".join(meta)}</span>{note}</div>')
    shell.html('<div class="ais-plan-cards">' + "".join(cards)
               + '<em style="font-size:.8rem">To add or remove events, open My Plan on a laptop.</em></div>')


def _planned_editor(semester: str, planned: pd.DataFrame, window, key: str) -> None:
    pending = st.session_state.get(f"{key}_pending")
    if pending:
        # Removing asks first: say what goes and what it was planned to cost.
        gone = pending["removed"]
        shell.notify(
            "warning",
            f"Remove {len(gone)} planned event{'s' if len(gone) != 1 else ''}: "
            f"{', '.join(r['title'] for r in gone)} "
            f"({format_currency(sum(float(r['estimated_cost'] or 0) for r in gone))} planned)? "
            "This can't be undone.",
        )
        yes, no, _ = st.columns([1.2, 1, 4])
        if yes.button("Remove them", type="primary", key=f"{key}_confirm"):
            _save_events(semester, pending["events"], f"Removed {len(gone)} event(s) from the {semester} plan.", key)
        if no.button("Keep them", key=f"{key}_cancel"):
            st.session_state.pop(f"{key}_pending", None)
            st.session_state[f"{key}_version"] = _version(key) + 1  # untick Remove
            st.rerun()
        return

    source = planned if not planned.empty else pd.DataFrame(
        columns=["event_date", "title", "estimated_cost", "expected_attendance", "note", "kind", "planned_event_id"]
    )
    frame = pd.DataFrame({
        "remove": pd.Series([False] * len(source), dtype="bool"),
        # datetime64 even when empty: the editor's date column refuses anything else.
        "event_date": pd.to_datetime(source["event_date"], errors="coerce").astype("datetime64[ns]").values,
        "title": source["title"].astype("object").values,
        "estimated_cost": pd.to_numeric(source["estimated_cost"], errors="coerce").astype("float64").values,
        "expected_attendance": pd.to_numeric(source["expected_attendance"], errors="coerce").astype("float64").values,
        "note": source["note"].astype("object").values,
        "kind": source["kind"].astype("object").values,
        "planned_event_id": pd.to_numeric(source["planned_event_id"], errors="coerce").astype("float64").values,
    })
    config = {
        "remove": st.column_config.CheckboxColumn("Remove", default=False, width="small"),
        "event_date": st.column_config.DateColumn(
            "Date", format="ddd MMM D",
            min_value=window[0] if window else None, max_value=window[1] if window else None,
        ),
        "title": st.column_config.TextColumn("Event", max_chars=event_ideas.MAX_TITLE),
        "estimated_cost": st.column_config.NumberColumn(
            "Est. cost", min_value=0.0, max_value=event_ideas.MAX_COST, step=5.0, format="dollar"),
        "expected_attendance": st.column_config.NumberColumn("Expected people", min_value=0, step=1, format="%d"),
        "note": st.column_config.TextColumn("Note", max_chars=event_ideas.MAX_NOTE, width="large"),
    }
    order = ["remove", "event_date", "title", "estimated_cost", "expected_attendance", "note"]
    # Phones: one card per event (CSS shows these under 640px and hides the table).
    _plan_cards(planned)
    if not can_edit:
        with st.container(key=f"{key}_table"):
            shell.dataframe(frame, column_config=config, column_order=order[1:])
        return
    table = st.container(key=f"{key}_table")
    with table:
        edited = st.data_editor(
            frame, column_config=config, column_order=order, num_rows="dynamic",
            hide_index=True, width="stretch", key=f"{key}_editor_{_version(key)}",
        )
        st.caption("Add an event in the bottom row, or pick ideas below. Tick Remove to drop one; you'll be asked first.")
        save = st.button("Save events", type="primary", key=f"{key}_save")
    if save:
        kept = edited[~edited["remove"].fillna(False).astype(bool)]
        events, problems = event_ideas.clean_planned_events(kept, window, semester)
        if problems:
            for problem in dict.fromkeys(problems):
                shell.notify("warning", problem)
            return
        kept_ids = set(pd.to_numeric(kept["planned_event_id"], errors="coerce").dropna().astype(int))
        # Ticked Remove, or deleted with the editor's own row delete: either way, ask first.
        removed = [] if planned.empty else [
            row for row, pid in zip(_as_rows(planned), planned["planned_event_id"]) if int(pid) not in kept_ids
        ]
        if removed:
            st.session_state[f"{key}_pending"] = {"events": events, "removed": removed}
            st.rerun()
        _save_events(semester, events, f"Saved {len(events)} event(s) for {semester}.", key)


def _ideas(semester: str, planned: pd.DataFrame, key: str) -> None:
    st.markdown("**Ideas from past events**")
    all_events = repo.load_events()
    mine = all_events[all_events["committee_id"].isin(line_ids)] if not all_events.empty else all_events
    try:
        links = repo.load_event_links()
    except Exception:  # noqa: BLE001 - no links table: ideas still show attendance
        links = pd.DataFrame()
    # Cost comes from this committee's own charges only (`charges`, scoped above).
    table = event_ideas.ideas(mine, charges, links, today=TODAY)
    if table.empty:
        st.caption(f"{title} has no past events on the club calendar yet.")
        return
    shown = pd.DataFrame({
        "add": False,
        "Kind": table["Kind"],
        "Last held": [f"{d:%b %d, %Y} · {t}" for d, t in zip(table["Last held"], table["Last title"])],
        "Times": table["Times"],
        "Avg people": table["Average attendance"],
        "Last count": table["Last attendance"],
        "Avg cost": table["Average cost"],
        "Per person": table["Cost per person"],
        "Past ones": table["Examples"],
    })
    config = {
        "add": st.column_config.CheckboxColumn("Add", default=False, width="small"),
        "Times": st.column_config.NumberColumn("Times held", format="%d", width="small"),
        "Avg people": st.column_config.NumberColumn(format="%d", width="small"),
        "Last count": st.column_config.NumberColumn(format="%d", width="small"),
        "Avg cost": st.column_config.NumberColumn(format="$%.0f", width="small"),
        "Per person": st.column_config.NumberColumn(format="dollar", width="small"),
        "Past ones": st.column_config.TextColumn(width="medium",
                                                 help="Which game, speaker or theme it was each time."),
    }
    if not can_edit:
        shell.dataframe(shown.drop(columns="add"), column_config=config)
        return
    edited = st.data_editor(
        shown, column_config=config, hide_index=True, width="stretch", height=320,
        disabled=[c for c in shown.columns if c != "add"], key=f"{key}_ideas_{_version(key)}",
    )
    st.caption(
        f"From {title}'s past events. Cost counts only purchases the treasurer has linked to an event, "
        "so a blank cost means none were linked yet. Tailgates and watch parties are planned as one kind: "
        "pick the game when the schedule is out."
    )
    chosen = table[edited["add"].fillna(False).astype(bool).values]
    if st.button(f"Add {len(chosen)} to the plan", disabled=chosen.empty, key=f"{key}_add"):
        events = _as_rows(planned) + event_ideas.plan_rows_from_ideas(chosen)
        _save_events(semester, events, f"Added {len(chosen)} event(s) to the {semester} plan.", key)


def _event_plan(semester: str, planned: pd.DataFrame | None, key: str, *, estimate: float | None = None) -> None:
    st.markdown("#### Events")
    if planned is None:
        st.caption("Event plans can't be saved on this site yet. The treasurer has to switch the feature on first.")
        return
    window = _window(semester)
    total = float(pd.to_numeric(planned["estimated_cost"], errors="coerce").sum()) if not planned.empty else 0.0
    if planned.empty:
        shell.say(f"No events planned for {semester} yet. Start from an idea below, or add your own.", caption=True)
    elif estimate:
        left = estimate - total
        tail = (f"leaving {format_currency(left)} of the rough {format_currency(estimate)} estimate for extras"
                if left >= 0 else f"{format_currency(-left)} more than the rough {format_currency(estimate)} estimate")
        shell.say(f"**{len(planned)} event{'s' if len(planned) != 1 else ''} planned**, about "
                  f"{format_currency(total)}, {tail}.")
    elif key == "events_next":
        shell.say(f"**{len(planned)} event{'s' if len(planned) != 1 else ''} planned**, about {format_currency(total)}.")
    _plan_calendar(planned, window, key)
    _planned_editor(semester, planned, window, key)
    _ideas(semester, planned, key)
    st.markdown("#### Other spending, by category")
    st.caption("Spending that isn't tied to one event: supplies, software, merch.")


st.markdown(f"**Committee**  \n{title}")
if can_edit:
    st.caption("Your plan is your own estimate. It doesn't move or reserve any money.")
else:
    st.caption(f"Read-only: only {title}'s VP edits this plan.")

# A switch rather than tabs: tabs jump back to the first one after every save.
views = {"this": f"This semester · {current}", "next": f"Next semester · {upcoming}"}
view = st.radio(
    "Plan for", list(views), format_func=views.get, horizontal=True,
    key="plan_view", label_visibility="collapsed",
)

# Earlier semesters a plan can be copied from: the last one, and the same season a year before.
earlier = [name for name in semesters if name != current and semesters.index(name) < semesters.index(current)]
last_semester = earlier[-1] if earlier else None

# --- This semester ---------------------------------------------------------------

if view == "this":
    saved = _load(repo.load_plan_lines, own, current)
    if saved is None:
        shell.notify("info", "Plans can't be saved on this site yet. The treasurer has to switch the feature on first.")
        st.stop()

    sheet = plan.budget_sheet(saved, _spent_in(current))
    position = _position(current)
    totals = plan.sheet_totals(sheet, position["budget"])
    planned_events = _load(repo.load_planned_events, own, current)
    _projection(position["budget"], float(position["spent"]), planned_events
                if planned_events is not None else pd.DataFrame(), current)

    _event_plan(current, planned_events, "events_now")
    # The category plan: spending that isn't one event. Its fit against the budget is told here.
    if totals["status"] == "over":
        shell.notify(
            "warning",
            f"The category plan is {format_currency(-totals['unplanned'])} more than {title}'s budget. "
            f"Trim it, or talk to the treasurer.",
        )
    elif totals["status"] == "tight":
        shell.say(f"Tight: {format_currency(totals['unplanned'])} of the budget is left unplanned by category.",
                  caption=True)
    elif totals["status"] == "fits" and totals["planned"]:
        shell.say(f"The category plan fits, with {format_currency(totals['unplanned'])} of the budget not "
                  "given to a category.", caption=True)
    _plan_editor(current, saved, sheet, "Spent", "plan_now")
    _charts(sheet, "Spent", "Spent", "plan_now")
    same_season = next((n for n in reversed(earlier) if plan.season_of(n) == plan.season_of(current)), None)
    _from_history(
        current, saved,
        {f"Last semester ({n})" if n == last_semester else f"Last {plan.season_of(n)} ({n})": n
         for n in dict.fromkeys(filter(None, [last_semester, same_season]))},
        "fill_now",
    )

# --- Next semester ---------------------------------------------------------------

else:
    if upcoming is None:
        shell.empty_state("Can't tell which semester comes next", f"'{current}' isn't named like 'Fall 2026'.")
        st.stop()

    season = plan.season_of(upcoming)
    spent_by, budget_by = {}, {}
    for name in semesters:
        if plan.season_of(name) == season:
            figures = _position(name)
            spent_by[name], budget_by[name] = figures["spent"], figures["budget"]
    history = plan.season_history(semesters, spent_by, budget_by, upcoming)
    guess = plan.suggestion(history)
    last_same = history["Term"].iloc[-1] if not history.empty else None

    st.markdown(f"#### What past {season} terms cost")
    estimate = 0.0
    if guess is None:
        st.caption(f"No earlier {season} term to go on, so there's no suggestion.")
    else:
        shell.dataframe(history, column_config={"Budget": _MONEY, "Spent": _MONEY})
        shell.say(
            f"A typical {season} cost {format_currency(guess['typical'])} "
            f"(lowest {format_currency(guess['low'])}, highest {format_currency(guess['high'])}, "
            f"across {guess['terms']} term(s)).",
            caption=True,
        )
        percent = st.slider(
            f"{upcoming} compared with a typical {season}", min_value=50, max_value=200, value=100,
            step=5, format="%d%%", key="plan_scale",
            help="100% means about the same as usual. Move it if you expect a bigger or smaller term.",
        )
        estimate = plan.scaled_estimate(guess["typical"], percent)
        shell.say(f"Rough estimate: {format_currency(estimate)}.", caption=True)

    saved = _load(repo.load_plan_lines, own, upcoming)
    if saved is None:
        shell.notify("info", "Plans can't be saved on this site yet. The treasurer has to switch the feature on first.")
        st.stop()

    _event_plan(upcoming, _load(repo.load_planned_events, own, upcoming), "events_next", estimate=estimate or None)
    st.markdown(f"#### Plan for {upcoming}")
    compare_label = f"Last {season}" if last_same else "Last time"
    reference = _spent_in(last_same) if last_same else pd.DataFrame(columns=["Purpose", "Amount"])
    sheet = plan.budget_sheet(saved, reference).rename(columns={"Spent": compare_label})
    planned_next = float(sheet["Planned"].sum()) if not sheet.empty else 0.0
    if planned_next:
        shell.say(f"Planned so far: {format_currency(planned_next)}.", caption=True)
    _plan_editor(upcoming, saved, sheet, compare_label, "plan_next")
    _charts(sheet, compare_label, compare_label, "plan_next")
    _from_history(
        upcoming, saved,
        {f"Last semester ({n})" if n == current else f"Last {season} ({n})": n
         for n in dict.fromkeys(filter(None, [current, last_same]))},
        "fill_next",
    )

    st.markdown("#### Budget request")
    requests = _load(repo.load_budget_requests, own, upcoming)
    if requests is None:
        st.caption("Budget requests can't be sent from this site yet.")
    else:
        sent = None if requests.empty else requests.iloc[0]
        if sent is not None:
            when = pd.to_datetime(sent["requested_at"], errors="coerce")
            shell.say(
                f"Sent {'' if pd.isna(when) else when.strftime('%b %d, %Y') + ' '}"
                f"for {format_currency(float(sent['amount']))}: {sent['reason']}",
                caption=True,
            )
        if can_edit:
            st.caption("The treasurer sets every budget. This tells them what you'd like and why.")
            amount = st.number_input(
                "Amount", min_value=0.0, max_value=plan.MAX_AMOUNT,
                value=float(sent["amount"]) if sent is not None else plan.default_request(planned_next, estimate),
                step=10.0, format="%.2f", key="plan_request_amount",
            )
            reason = st.text_area(
                "What it's for", value="" if sent is None else str(sent["reason"]),
                max_chars=plan.MAX_REASON_LENGTH, key="plan_request_reason",
            )
            label = "Update request" if sent is not None else "Send to the treasurer"
            if st.button(label, key="plan_request_send"):
                value, text, problem = plan.clean_request(amount, reason)
                if problem:
                    shell.notify("warning", problem)
                else:
                    result = repo.save_budget_request(committee_id, upcoming, value, text, own, identity.email)
                    if result.error:
                        shell.notify("warning", result.error)
                    else:
                        st.session_state[_FLASH] = f"Budget request for {upcoming} sent to the treasurer."
                        st.rerun()
        elif sent is None:
            st.caption(f"{title} hasn't sent a request for {upcoming}.")
