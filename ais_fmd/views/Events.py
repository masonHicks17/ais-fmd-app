"""
Events -- the club calendar, and what each event cost (domain/events.py).

Events come from the AIS Event Schedule workbooks Membership already keeps (or
an .ics export of the Google Calendar they feed). Spending is tied to an event
by confirming a suggestion: the app proposes the likeliest event for each
purchase and says why, and a treasurer ticks the ones that are right -- or
picks the right event in the table when the guess is wrong.

Clicking an event (a calendar chip, or a row in the list) opens it in a
dialog: what, when, who came, and the purchases linked to it, which the
treasurer can move to another event, unlink, or add to (2026-11-09).

The panel also holds what an event is expected to cost and costs the bank
statement won't show (2026-11-13): estimates, and typed costs such as cash.
The month works like a calendar app for whoever may plan an event:
double-click a day (or its "+") to add one, drag an event to move it. An
.ics export takes the whole calendar, the semester, or just the changes made
here, to Google Calendar.

Live Google Calendar sync is shown as a preview only: the sandbox has no
network, and the calendar ID is read from the workbook's calculation column.
Schedule files are loaded on Treasury -> Reconcile, which shows what a file
would change before anything is saved.

A VP (`identity.committee_scoped`) gets this page too. VPs work as one team,
so they see every club event (titles, dates, places, attendance), but money -- what an event cost, purchases that may
belong to one -- is worked out from their own committee's transactions only,
and shown only on their own committee's events. They can add their own
committee's unlinked purchases to their own committee's events (2026-11-09);
moving and unlinking purchases stay with the treasurer. A VP can add, edit and
move their own committee's events and type their costs.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from html import escape

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitAPIException

from ais_fmd import auth
from ais_fmd.config import vp_committees
from ais_fmd.data import repositories as repo
from ais_fmd.domain import calendar_export
from ais_fmd.domain import events as events_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import ordered_semesters, term_id_for_semester
from ais_fmd.ui import calendar_grid, clickable, committee_charts, shell

auth.require(auth.Role.MEMBER)
identity = auth.current_user()
can_edit = identity.can_write
# A VP may add (never move or unlink) their committee's purchases to their
# committee's events: the panel returns before any control on another
# committee's event, and `money` is already only their committee's rows.
can_add = can_edit or (identity.committee_scoped and not identity.read_only)
scoped = identity.committee_scoped
TODAY = date.today()

shell.environment_banner()
if scoped and identity.committee_id is None:
    shell.page_header("Events", "The club calendar, and what your committee's events cost.")
    shell.empty_state("No committee assigned to your account", " Ask the treasurer to add your email under Officer Access.")
    st.stop()
my_title = vp_committees.title_for(identity.committee_id) if scoped else ""
shell.page_header(
    "Events",
    f"The club calendar, and what {my_title}'s events cost." if scoped else
    "The club calendar next to the money: what happened when, who came, and "
    "what each event cost.",
)

bundle = repo.load_bundle()
all_events = repo.load_events()
links = repo.load_event_links()
committees = repo.load_committees()
committee_names = dict(zip(committees["CommitteeID"].astype(int), committees["Committee_Name"]))

# Money comes only from transactions this identity may see. For a VP that is
# their own committee's rows, picked before anything else touches them.
my_ids = set(vp_committees.budget_ids_for(identity.committee_id)) if scoped else None
money = bundle.transactions
if scoped:
    money = money[pd.to_numeric(money["budget_category"], errors="coerce").isin(my_ids)]


def is_mine(committee_id) -> bool:
    """Whether this identity sees money for an event booked to `committee_id`."""
    return not scoped or (pd.notna(committee_id) and int(committee_id) in my_ids)


def can_plan(committee_id) -> bool:
    """Whether this identity may add, edit or move an event booked to `committee_id`."""
    return can_edit or (can_add and is_mine(committee_id))

# --- Sync ---------------------------------------------------------------------


def _sync_status() -> None:
    sources = (
        all_events.groupby("source").agg(events=("event_id", "size"), synced=("synced_at", "max"))
        if not all_events.empty
        else pd.DataFrame()
    )
    calendar_id = (
        all_events["calendar_id"].dropna().iloc[0]
        if not all_events.empty and all_events["calendar_id"].notna().any()
        else None
    )
    google_column, files_column = st.columns([3, 2], gap="medium")
    with google_column, shell.card("events-google", order=1):
        shell.heading(
            "Google Calendar", shell.pill("Preview · not connected", "muted"),
            help="The calendar ID comes from the schedule workbook. Connected, it would sync every hour. "
                 "The sandbox has no network, so for now events come from the spreadsheets on the right.",
        )
        short_id = f"{calendar_id[:14]}…{calendar_id.split('@')[-1]}" if calendar_id else "not found yet"
        shell.say(f"AIS Events calendar (`{short_id}`).", caption=True)
        st.button(
            "Connect Google Calendar", icon=":material/calendar_month:", disabled=True,
            help="Needs Google sign-in, which isn't available in the sandbox.",
            key="events_connect_google",
        )
    with files_column, shell.card("events-files", order=2):
        shell.heading("Synced from", shell.pill(f"{len(all_events)} events", "muted"))
        if sources.empty:
            shell.say("Nothing yet.", caption=True)
        for source, row in sources.iterrows():
            # SQLite's CURRENT_TIMESTAMP is UTC; show local time.
            synced = pd.to_datetime(row["synced"], errors="coerce", utc=True)
            when = f"{synced.tz_convert(datetime.now().astimezone().tzinfo):%b %d, %-I:%M %p}" if not pd.isna(synced) else ""
            name = "Added in the app" if source == "manual" else str(source).split(": ", 1)[-1]
            count = f"{row['events']} event{'s' if row['events'] != 1 else ''}"
            shell.say(f"**{escape(name)}**  \n{count} · {when}", caption=True)
        if can_edit:
            st.button(
                "Check a schedule file", icon=":material/difference:", key="events_to_reconcile",
                on_click=_to_reconcile,
                help="On Treasury → Reconcile: upload an event schedule (.xlsx) or a calendar export (.ics), "
                     "see what's new, changed or missing, and pick what to apply.",
            )


def _to_reconcile() -> None:
    st.session_state[RECONCILE_KEY] = RECONCILE_TARGET
    st.session_state["reconcile_source"] = "Event schedule"
    st.session_state["_events_switch"] = True


RECONCILE_KEY, RECONCILE_TARGET = "treasury_tab", "Reconcile"
if st.session_state.pop("_events_switch", False):
    st.switch_page("ais_fmd/views/Treasury.py")


_sync_status()

if all_events.empty:
    st.stop()

# --- Which events -------------------------------------------------------------

semesters = [s for s in ordered_semesters(bundle.terms)
             if term_id_for_semester(bundle.terms, s) in set(all_events["term_id"].dropna())]
current = next(
    (s for s in reversed(semesters)
     if term_id_for_semester(bundle.terms, s) in set(all_events.loc[pd.to_datetime(all_events["event_date"]).dt.date <= TODAY, "term_id"])),
    semesters[-1],
)
semester_column, committee_column, view_column = st.columns([2, 2, 2])
with semester_column:
    semester = st.selectbox("Semester", semesters, index=semesters.index(current), key="events_semester")
term_id = term_id_for_semester(bundle.terms, semester)
term_events = all_events[all_events["term_id"] == term_id].copy()
term_events["day"] = pd.to_datetime(term_events["event_date"]).dt.date
with committee_column:
    present = sorted({int(c) for c in term_events["committee_id"].dropna()}, key=lambda c: committee_names.get(c, ""))
    if scoped:
        # VPs work as one team, so every VP sees the whole club calendar; only the
        # money is limited to their own committee (see is_mine).
        scope = st.selectbox("Show", ["All club events", f"{my_title} events"], key="events_committee_vp")
    else:
        scope = st.selectbox(
            "Committee", ["All committees", *[committee_names.get(c, str(c)) for c in present]], key="events_committee"
        )
if scoped:
    if scope != "All club events":
        term_events = term_events[term_events["committee_id"].map(is_mine)]
elif scope != "All committees":
    wanted = next(c for c in present if committee_names.get(c) == scope)
    term_events = term_events[term_events["committee_id"] == wanted]
with view_column:
    view = st.segmented_control(
        "View", ["Month", "List"], default="Month", key="events_view",
        help="Click an event in the month, or a row in the list, to see its details and purchases.",
    ) or "Month"

spending = events_domain.event_spending(money, links)
event_costs = repo.load_event_costs()


def _typed(kind: str) -> pd.Series:
    """event_id -> total of the costs of `kind` typed against it."""
    if event_costs.empty:
        return pd.Series(dtype=float)
    rows = event_costs[event_costs["kind"] == kind]
    return pd.to_numeric(rows["amount"], errors="coerce").groupby(rows["event_id"].astype(int)).sum()


term_events = term_events.merge(spending, on="event_id", how="left")
term_events["Spent"] = term_events["Spent"].fillna(0.0)
term_events["Purchases"] = term_events["Purchases"].fillna(0).astype(int)
# Costs typed in the panel: an estimate, and costs no statement shows (cash).
term_events["Estimated"] = term_events["event_id"].astype(int).map(_typed("estimate")).fillna(0.0)
term_events["Spent"] += term_events["event_id"].astype(int).map(_typed("manual")).fillna(0.0)
# Another committee's event shows no money to a VP, even one of their own
# purchases linked to it -- the event's cost belongs to its committee.
term_events.loc[~term_events["committee_id"].map(is_mine), ["Spent", "Purchases", "Estimated"]] = 0

suggestions = events_domain.suggest_links(money, all_events, links, committees)
term_ids = set(term_events.loc[term_events["committee_id"].map(is_mine), "event_id"])
term_suggestions = [s for s in suggestions if s.event_id in term_ids]

# --- Headline -----------------------------------------------------------------

held = term_events[term_events["day"] <= TODAY]
attended = held["attendance"].dropna()
costed = term_events[term_events["Spent"] > 0]
to_review = [s for s in term_suggestions if s.confidence != events_domain.LOW]
tiles = [
    ("events-count", "Events", f"{len(term_events):,}", False,
     f"{len(held):,} held, {len(term_events) - len(held):,} to come"),
    ("events-linked", "Your events' spending" if scoped else "Spent on events", float(costed["Spent"].sum()), True,
     f"across {len(costed):,} events"),
    ("events-attendance", "Average attendance", f"{attended.mean():.0f}" if len(attended) else "—", False,
     f"{len(attended):,} events with a count"),
    ("events-review", "Purchases to match", f"{len(to_review):,}", False, ""),
]
tile_help = {
    "events-linked": "Purchases linked from the bank statement, plus costs typed in an event that no statement shows.",
    "events-review": "Purchases that likely belong to an event. The treasurer confirms these."
    if scoped else "Purchases that likely belong to an event.",
}
for column, (name, label, value, currency, caption) in zip(st.columns(4, gap="medium"), tiles):
    with column, shell.card(name, order=2):
        shell.metric(st, label, value, currency=currency, help=tile_help.get(name))
        if caption:
            shell.say(caption, caption=True)

# --- One event: a panel opened from the calendar or the list -----------------
#
# Replaced the "Event details" section at the foot of the page (2026-11-09):
# it sat a screen below the calendar behind its own picker. Now a click on an
# event opens it in a dialog, with its purchases, and the treasurer can move,
# unlink or link purchases there.

OPEN_KEY = "events_open"   # an event id to open on this run (tests set it directly)
event_lookup = all_events.set_index("event_id")


def _event_name(event_id) -> str:
    if event_id not in event_lookup.index:
        return str(event_id)
    row = event_lookup.loc[event_id]
    return f"{pd.to_datetime(row['event_date']):%b %d} · {row['title']}"


def _purchase_name(transaction_id, extra: str = "") -> str:
    row = money.loc[money["transactionid"] == transaction_id].iloc[0]
    amount = format_currency(-float(pd.to_numeric(row["amount"], errors="coerce")))
    label = events_domain.purchase_label(row["details"])[:40]
    return f"{pd.to_datetime(row['transaction_date']):%b %d} · {label} · {amount}{extra}"


def _events_near(day: date, *, exclude: int | None = None) -> list[int]:
    """Events this identity may book money to, within a month of `day`, closest first."""
    days = pd.to_datetime(all_events["event_date"]).dt.date
    near = all_events[(days >= day - timedelta(days=30)) & (days <= day + timedelta(days=30))
                      & all_events["committee_id"].map(is_mine)]
    near = near.assign(_gap=[abs((d - day).days) for d in days[near.index]]).sort_values(["_gap", "event_date"])
    return [int(e) for e in near["event_id"] if int(e) != exclude]


def _money_near(day: date) -> pd.DataFrame:
    """Spending posted from 10 days before an event to 30 after (cards post late)."""
    posted = pd.to_datetime(money["transaction_date"], errors="coerce").dt.date
    amount = pd.to_numeric(money["amount"], errors="coerce")
    window = money[(posted >= day - timedelta(days=10)) & (posted <= day + timedelta(days=30)) & (amount < 0)].copy()
    window["_gap"] = [abs((d - day).days) for d in posted[window.index]]
    return window.sort_values(["_gap", "transactionid"])


def _changed(message: str) -> None:
    """Redraw the open panel with the change; the page behind refreshes when it closes."""
    st.toast(message)
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:  # not inside a dialog rerun (a full run, or a test)
        st.rerun()


def _stat(label: str, value: str, sub: str = "", *, help: str = "", first: bool = False, over: bool = False) -> str:
    edge = " first" if first else ""
    small = f"<small>{sub}</small>" if sub else ""
    tone = ' class="over"' if over else ""
    return f'<div class="ais-stat{edge}"><span>{label}{shell.tip(help)}</span><b{tone}>{value}</b>{small}</div>'


def _purchase_rows(event_id: int, purchases: pd.DataFrame) -> None:
    """Each linked purchase; the treasurer can move it to another event or unlink it."""
    for row in purchases.itertuples():
        tid = int(row.transactionid)
        widths = [1, 3, 1.2, 3, 0.7] if can_edit else [1, 3, 1.2]
        cells = st.columns(widths, vertical_alignment="center")
        cells[0].caption(f"{pd.to_datetime(row.transaction_date):%b %d}")
        cells[1].write(events_domain.purchase_label(row.details)[:40])
        cells[2].write(format_currency(-float(row.amount)))
        if can_edit:
            target = cells[3].selectbox(
                "Move to", _events_near(pd.to_datetime(row.transaction_date).date(), exclude=event_id),
                index=None, placeholder="Move to another event…", format_func=_event_name,
                key=f"events_move_{event_id}_{tid}", label_visibility="collapsed",
            )
            if target is not None:
                result = repo.link_transactions_to_events([(tid, int(target))], identity.email)
                if result.error:
                    shell.notify("warning", result.error)
                else:
                    _changed(f"Moved to {_event_name(int(target))}.")
            if cells[4].button("", icon=":material/link_off:", key=f"events_unlink_{event_id}_{tid}",
                               help="Unlink this purchase from the event"):
                repo.unlink_transaction_from_event(tid, identity.email)
                _changed("Unlinked.")


def _event_panel(event_id: int) -> None:
    # Read fresh: inside the dialog only this function reruns, so the page's
    # own copy is stale after an edit made here.
    fresh = repo.load_events().set_index("event_id")
    if event_id not in fresh.index:
        shell.say("This event is no longer on the calendar.", caption=True)
        return
    event = fresh.loc[event_id]
    day = pd.to_datetime(event["event_date"]).date()
    committee_id = event["committee_id"]
    owner = committee_names.get(int(committee_id), "") if pd.notna(committee_id) else ""

    # The schedule sheets sometimes hold words ("Never", "TBD") in the time
    # columns; only show what reads as a time.
    start, end = (str(event[k]) if pd.notna(event[k]) and any(ch.isdigit() for ch in str(event[k])) else None
                  for k in ("start_time", "end_time"))
    when = f"{day:%A, %b %d}" + (f" · {start}" if start else "") + (f"–{end}" if start and end else "")
    facts = [when]
    if pd.notna(event["location"]):
        facts.append(str(event["location"]))
    if pd.notna(event["attendance"]):
        facts.append(f"{int(event['attendance'])} came")
    added_here = str(event.get("source") or "") == "manual"
    shell.html(
        f'<div class="ais-event-facts">{shell.pill(escape(owner or "No committee"), "muted")}'
        + (shell.pill("Added here", "muted") if added_here else "")
        + f'<span>{" · ".join(escape(str(f)) for f in facts)}</span></div>'
    )
    more = [f"{label}: {event[key]}" for label, key in (("Owner", "owner"), ("Audience", "audience"), ("Status", "status"))
            if pd.notna(event[key])]
    if pd.notna(event["attendance"]) and pd.notna(event["attendance_source"]):
        more.append(f"Attendance from the {event['attendance_source']}")
    if more:
        shell.say(escape(" · ".join(str(m) for m in more)), caption=True)
    if pd.notna(event["description"]):
        shell.say(escape(str(event["description"])), caption=True)

    if not is_mine(committee_id):
        shell.heading("Costs")
        shell.say(f"What this event cost is visible to the {escape(owner or 'committee')} VP and the treasurer.", caption=True)
        return

    links_now = repo.load_event_links()
    linked_ids = links_now.loc[links_now["event_id"] == event_id, "transaction_id"].astype(int).tolist() if not links_now.empty else []
    purchases = money[money["transactionid"].isin(linked_ids)].sort_values("transaction_date")
    bought = float(-pd.to_numeric(purchases["amount"], errors="coerce").sum()) if not purchases.empty else 0.0
    costs_now = repo.load_event_costs()
    typed = costs_now[costs_now["event_id"].astype(int) == event_id] if not costs_now.empty else costs_now
    amounts = pd.to_numeric(typed["amount"], errors="coerce") if not typed.empty else pd.Series(dtype=float)
    estimate = float(amounts[typed["kind"] == "estimate"].sum()) if not typed.empty else 0.0
    other = float(amounts[typed["kind"] == "manual"].sum()) if not typed.empty else 0.0
    spent = bought + other
    planner = can_plan(committee_id)

    # --- Costs: the estimate against what was spent, in one place.
    shell.heading(
        "Costs",
        help="Estimated: what you expect it to cost, typed below. Spent: purchases linked from the bank "
             "statement plus costs typed here that no statement shows (cash, a card not yet reimbursed).",
    )
    left = estimate - spent
    stats = [
        _stat("Estimated", format_currency(estimate) if estimate else "—", first=True,
              sub=f"{int((typed['kind'] == 'estimate').sum())} line(s)" if estimate else "none yet"),
        _stat("Spent", format_currency(spent),
              sub=" + ".join(part for part in (
                  f"{format_currency(bought)} bank" if bought else "",
                  f"{format_currency(other)} typed" if other else "",
              ) if part) or "nothing yet"),
    ]
    if estimate:
        stats.append(_stat("Over by" if left < 0 else "Left", format_currency(abs(left)), over=left < 0,
                           sub=f"{spent / estimate:.0%} of the estimate"))
    if spent and pd.notna(event["attendance"]) and event["attendance"]:
        stats.append(_stat("Per person", format_currency(spent / event["attendance"]),
                           sub=f"{int(event['attendance'])} came"))
    shell.html(f'<div class="ais-stat-row">{"".join(stats)}</div>')

    for row in typed.itertuples() if not typed.empty else []:
        cells = st.columns([3, 1.6, 1.2, 0.7] if planner else [3, 1.6, 1.2], vertical_alignment="center")
        cells[0].write(str(row.label))
        kind_name = "Estimate" if row.kind == "estimate" else "Paid, not on the statement"
        cells[1].caption("" if str(row.label) == kind_name else kind_name)
        cells[2].write(format_currency(float(row.amount)))
        if planner and cells[3].button("", icon=":material/delete:", key=f"events_cost_del_{row.cost_id}",
                                       help="Remove this line"):
            repo.delete_event_cost(int(row.cost_id), identity.email)
            _changed("Removed.")

    if planner:
        with st.form(f"events_cost_form_{event_id}", clear_on_submit=True, border=False):
            cells = st.columns([3, 1.5, 2.2, 1.3], vertical_alignment="bottom")
            label = cells[0].text_input("What for", placeholder="Pizza for 60, room fee…", key=f"events_cost_label_{event_id}")
            amount = cells[1].number_input("Amount", min_value=0.0, step=5.0, value=None, format="%.2f",
                                           key=f"events_cost_amount_{event_id}")
            kind = cells[2].selectbox("Kind", ["Estimate", "Paid, not on the statement"],
                                      key=f"events_cost_kind_{event_id}")
            if cells[3].form_submit_button("Add cost", icon=":material/add:"):
                if not amount:
                    shell.notify("warning", "Type an amount first.")
                else:
                    result = repo.add_event_cost(event_id, "estimate" if kind == "Estimate" else "manual",
                                                 label or "", float(amount), identity.email)
                    if result.error:
                        shell.notify("error", result.error)
                    else:
                        _changed("Added.")

    shell.heading("From the bank statement", shell.pill(format_currency(bought), "muted"))
    if purchases.empty:
        shell.say("No purchases linked yet.", caption=True)
    _purchase_rows(event_id, purchases)

    nearby = _money_near(day)
    here = [s for s in events_domain.suggest_links(nearby, all_events, links_now, committees) if s.event_id == event_id]
    if here:
        shell.heading("Suggested for this event", shell.pill(str(len(here)), "muted"),
                      help="" if can_add else "The treasurer confirms these.")
        for s in sorted(here, key=lambda s: -s.score):
            cells = st.columns([1, 3, 1.2, 1.4, 1.1] if can_add else [1, 3, 1.2, 1.4], vertical_alignment="center")
            row = money.loc[money["transactionid"] == s.transaction_id].iloc[0]
            cells[0].caption(f"{pd.to_datetime(row['transaction_date']):%b %d}")
            cells[1].write(events_domain.purchase_label(row["details"])[:40])
            cells[2].write(format_currency(-float(row["amount"])))
            cells[3].caption(s.confidence, help=s.reason)
            if can_add and cells[4].button("Link", key=f"events_suggest_{event_id}_{s.transaction_id}"):
                repo.link_transactions_to_events([(s.transaction_id, event_id)], identity.email)
                _changed("Linked.")

    if can_add:
        elsewhere = dict(zip(links_now["transaction_id"].astype(int), links_now["event_id"].astype(int))) if not links_now.empty else {}
        # A VP adds only purchases on no event yet: taking one off another
        # event would undo a link someone confirmed, which is the treasurer's call.
        candidates = [int(t) for t in nearby["transactionid"]
                      if int(t) not in linked_ids and (can_edit or int(t) not in elsewhere)]
        first, last = day - timedelta(days=10), day + timedelta(days=30)
        choice = st.selectbox(
            "Link another purchase", candidates, index=None,
            placeholder=f"Search purchases from {first:%b %d} to {last:%b %d}",
            format_func=lambda t: _purchase_name(t, f" (now on {_event_name(elsewhere[t])})" if t in elsewhere else ""),
            key=f"events_add_{event_id}",
            help=("Spending posted from 10 days before the event to 30 after. A purchase already on "
                  "another event moves here.") if can_edit else
                 ("Your committee's spending from 10 days before the event to 30 after that isn't on an "
                  "event yet. To move or unlink a purchase, ask the treasurer."),
        )
        if st.button("Link purchase", disabled=choice is None, key=f"events_add_go_{event_id}"):
            repo.link_transactions_to_events([(int(choice), event_id)], identity.email)
            _changed("Linked.")

    if planner:
        with st.expander("Edit event", icon=":material/edit:"):
            _edit_form(event_id, event, len(linked_ids))


# --- Adding, editing, moving --------------------------------------------------

def _committee_choices() -> list[int | None]:
    """Committees this identity may book an event to (a VP: only their own)."""
    if scoped:
        return sorted(my_ids)
    return [None, *sorted(committee_names, key=lambda c: committee_names[c])]


def _committee_label(committee_id) -> str:
    return committee_names.get(int(committee_id), str(committee_id)) if committee_id is not None else "No committee"


def _clock(value) -> str | None:
    return f"{value:%H:%M}" if value is not None else None


def _term_for(day: date) -> str | None:
    return events_domain.term_for_day(bundle.terms, day)[0] or term_id


def _edit_form(event_id: int, event: pd.Series, linked: int) -> None:
    choices = _committee_choices()
    current = int(event["committee_id"]) if pd.notna(event["committee_id"]) else None
    with st.form(f"events_edit_{event_id}", border=False):
        title = st.text_input("Title", value=str(event["title"]), key=f"events_edit_title_{event_id}")
        cells = st.columns(3)
        day = cells[0].date_input("Date", value=pd.to_datetime(event["event_date"]).date(),
                                  key=f"events_edit_day_{event_id}")
        start = cells[1].text_input("Starts", value=str(event["start_time"] or "") if pd.notna(event["start_time"]) else "",
                                    placeholder="18:30", key=f"events_edit_start_{event_id}")
        end = cells[2].text_input("Ends", value=str(event["end_time"] or "") if pd.notna(event["end_time"]) else "",
                                  placeholder="20:00", key=f"events_edit_end_{event_id}")
        location = st.text_input("Location", value=str(event["location"]) if pd.notna(event["location"]) else "",
                                 key=f"events_edit_location_{event_id}")
        committee = st.selectbox("Committee", choices, index=choices.index(current) if current in choices else 0,
                                 format_func=_committee_label, key=f"events_edit_committee_{event_id}")
        description = st.text_area("Notes", value=str(event["description"]) if pd.notna(event["description"]) else "",
                                   key=f"events_edit_notes_{event_id}", height=90)
        if st.form_submit_button("Save changes", type="primary"):
            wanted = {
                "title": title.strip(), "event_date": day.isoformat(), "start_time": start.strip() or None,
                "end_time": end.strip() or None, "location": location.strip() or None,
                "committee_id": committee, "description": description.strip() or None, "term_id": _term_for(day),
            }
            def same(old, new) -> bool:
                return (None if pd.isna(old) else str(old)) == (None if new is None else str(new))
            changes = {k: v for k, v in wanted.items() if not same(event.get(k), v)}
            if not wanted["title"]:
                shell.notify("warning", "An event needs a title.")
            elif not changes:
                shell.notify("info", "Nothing changed.")
            else:
                result = repo.update_event(event_id, changes, identity.email)
                if result.error:
                    shell.notify("error", result.error)
                else:
                    _changed("Saved. Export the changes to put them on Google Calendar.")
    confirm_key = f"_events_delete_{event_id}"
    if linked:
        st.button("Delete event", icon=":material/delete:", disabled=True, key=f"events_delete_{event_id}",
                  help=f"{linked} purchase(s) are linked to it. Move or unlink them first.")
    elif not st.session_state.get(confirm_key):
        if st.button("Delete event", icon=":material/delete:", key=f"events_delete_{event_id}"):
            st.session_state[confirm_key] = True
            _changed("Click again to confirm.")
    else:
        cells = st.columns([2, 1, 3])
        if cells[0].button("Yes, delete it", type="primary", key=f"events_delete_yes_{event_id}"):
            result = repo.delete_event(event_id, identity.email)
            st.session_state.pop(confirm_key, None)
            if result.error:
                shell.notify("error", result.error)
            else:
                st.toast("Deleted.")
                st.rerun()
        if cells[1].button("Cancel", key=f"events_delete_no_{event_id}"):
            st.session_state.pop(confirm_key, None)
            _changed("Kept.")
        cells[2].caption("This removes it here only. If it's on Google Calendar, delete it there too.")


def _add_panel(day: date) -> None:
    choices = _committee_choices()
    with st.form("events_new", border=False):
        title = st.text_input("Title", placeholder="GBM #5, Resume workshop…", key="events_new_title")
        cells = st.columns(3)
        when = cells[0].date_input("Date", value=day, key="events_new_day")
        start = cells[1].time_input("Starts", value=None, step=900, key="events_new_start")
        end = cells[2].time_input("Ends", value=None, step=900, key="events_new_end")
        location = st.text_input("Location", key="events_new_location")
        cells = st.columns(2)
        committee = cells[0].selectbox("Committee", choices, format_func=_committee_label, key="events_new_committee")
        estimate = cells[1].number_input("Estimated cost", min_value=0.0, step=5.0, value=None, format="%.2f",
                                         key="events_new_estimate", help="Optional. You can add more cost lines after.")
        description = st.text_area("Notes", key="events_new_notes", height=90)
        if st.form_submit_button("Add event", type="primary", icon=":material/add:", key="events_new_submit"):
            if not title.strip():
                shell.notify("warning", "An event needs a title.")
                return
            row = events_domain.Event(
                event_date=when, title=title.strip(), start_time=_clock(start), end_time=_clock(end),
                location=location.strip() or None, description=description.strip() or None,
                committee_id=committee, term_id=_term_for(when), source="manual",
            ).as_row()
            result = repo.add_event(row, identity.email)
            if result.error:
                shell.notify("error", result.error)
                return
            if estimate:
                repo.add_event_cost(result.updated, "estimate", "Estimate", float(estimate), identity.email)
            st.session_state[OPEN_KEY] = int(result.updated)
            st.session_state[f"events_month_{row['term_id']}"] = f"{calendar.month_abbr[when.month]} {when.year}"
            st.toast(f"Added {title.strip()}.")
            st.rerun()


MOVE_KEY = "events_last_move"   # (event_id, from day, to day, title): the undo offered after a drag


def _move(event_id: int, day: date) -> None:
    if event_id not in event_lookup.index:
        return
    event = event_lookup.loc[event_id]
    if not can_plan(event["committee_id"]):
        shell.notify("warning", "Only the treasurer or this committee's VP can move this event.")
        return
    before = pd.to_datetime(event["event_date"]).date()
    result = repo.update_event(event_id, {"event_date": day.isoformat(), "term_id": _term_for(day)}, identity.email)
    if result.error:
        shell.notify("error", result.error)
        return
    st.session_state[MOVE_KEY] = (event_id, before.isoformat(), day.isoformat(), str(event["title"]))
    st.rerun()


def _undo_move() -> None:
    moved = st.session_state.pop(MOVE_KEY, None)
    if moved:
        repo.update_event(int(moved[0]), {"event_date": moved[1], "term_id": _term_for(date.fromisoformat(moved[1]))},
                          identity.email)


def _open(event_id: int) -> None:
    title = str(event_lookup.loc[event_id, "title"]) if event_id in event_lookup.index else "Event"
    st.dialog(title, width="large", on_dismiss="rerun")(_event_panel)(event_id)


def _open_add(day: date) -> None:
    st.dialog("Add an event", width="large", on_dismiss="rerun")(_add_panel)(day)


to_open: int | None = st.session_state.pop(OPEN_KEY, None)
ADD_KEY = "events_add_day"   # a day to open the add-event form on (the button, or a test)
to_add: date | None = st.session_state.pop(ADD_KEY, None)

# --- Month grid ---------------------------------------------------------------

def _color(committee_id) -> str:
    return committee_charts.committee_color(int(committee_id) if pd.notna(committee_id) else None)


def _month_grid(year: int, month: int, rows: pd.DataFrame) -> str:
    chips: dict[date, list[calendar_grid.Chip]] = {}
    for event in rows.sort_values(["day", "start_time"], na_position="last").itertuples():
        meta = []
        if isinstance(event.start_time, str) and event.start_time:
            meta.append(escape(event.start_time))
        if pd.notna(event.attendance):
            meta.append(f"{int(event.attendance)} came")
        if event.Spent > 0:
            meta.append(f'<span class="spent">&#36;{event.Spent:,.0f}</span>')
        elif event.Estimated > 0:
            meta.append(f'<span class="est">est &#36;{event.Estimated:,.0f}</span>')
        owner = committee_names.get(int(event.committee_id), "") if pd.notna(event.committee_id) else ""
        where = f" · {event.location}" if isinstance(event.location, str) and event.location else ""
        chips.setdefault(event.day, []).append(calendar_grid.Chip(
            title=str(event.title), meta_html=" · ".join(meta), color=_color(event.committee_id),
            tip=f"{event.title}{where} · {owner}" + (" · drag to move" if can_plan(event.committee_id) else ""),
            pick=str(int(event.event_id)), movable=can_plan(event.committee_id),
        ))
    return calendar_grid.month_grid(year, month, chips, today=TODAY, editable=can_add)


def _actions(month_shown: tuple[int, int] | None) -> None:
    """Add an event, and export to Google Calendar, right-aligned above the calendar."""
    cells = st.columns(2 if can_add else 1)
    if can_add:
        year, month = month_shown or (TODAY.year, TODAY.month)
        first = date(year, month, 1)
        default = TODAY if (TODAY.year, TODAY.month) == (year, month) else first
        cells[0].button(
            "Add event", icon=":material/add:", key="events_add_button", width="stretch",
            on_click=lambda: st.session_state.__setitem__(ADD_KEY, default),
            help="Or double-click a day on the calendar. Drag an event to move it.",
        )
    with cells[-1].popover("Export", icon=":material/ios_share:", width="stretch"):
        _export_menu()


def _export_menu() -> None:
    changes = calendar_export.changed_here(all_events)
    semester_rows = all_events[all_events["term_id"] == term_id]
    options = {
        f"Changes made here ({len(changes)})": ("changes", changes),
        f"{semester} ({len(semester_rows)})": (semester, semester_rows),
        f"Whole calendar ({len(all_events)})": ("all", all_events),
    }
    pick = st.radio(
        "What to export", list(options), key="events_export_scope",
        help="Changes made here: events added, moved or edited in this app since the calendar last synced them.",
    )
    scope, rows = options[pick]
    st.download_button(
        "Download .ics", data=calendar_export.to_ics(rows, committee_names), type="primary",
        file_name=calendar_export.file_name(scope, TODAY), mime="text/calendar", icon=":material/download:",
        disabled=rows.empty, key="events_export_download", width="stretch",
    )
    shell.say(
        "In Google Calendar: **Settings → Import & export → Import**, choose this file and the AIS Events "
        "calendar. Importing the same file twice doesn't add copies. Costs are never exported.",
        caption=True,
    )


moved = st.session_state.get(MOVE_KEY)
if moved:
    note, undo, dismiss = st.columns([6, 1, 1], vertical_alignment="center")
    note.markdown(
        f"Moved **{escape(moved[3])}** from {date.fromisoformat(moved[1]):%a, %b %-d} to "
        f"{date.fromisoformat(moved[2]):%a, %b %-d}."
    )
    undo.button("Undo", icon=":material/undo:", key="events_undo_move", on_click=_undo_move)
    dismiss.button("Done", key="events_move_done", on_click=lambda: st.session_state.pop(MOVE_KEY, None))

if view == "Month":
    months = sorted({(d.year, d.month) for d in term_events["day"]})
    if months:
        labels = [f"{calendar.month_abbr[m]} {y}" for y, m in months]
        default = next((label for (y, m), label in zip(months, labels) if (y, m) == (TODAY.year, TODAY.month)), labels[-1])
        month_key = f"events_month_{term_id}"
        if st.session_state.get(month_key) not in (None, *labels):
            st.session_state.pop(month_key)  # a month that no longer has events
        month_column, action_column = st.columns([3, 1.3], vertical_alignment="center")
        with month_column:
            month_label = st.segmented_control("Month", labels, default=default, key=month_key,
                                               label_visibility="collapsed") or default
        year, month = months[labels.index(month_label)]
        with action_column:
            _actions((year, month))
        legend = shell.legend([(committee_names.get(c, ""), color) for c, color in committee_charts.committee_colors().items()
                               if c in set(term_events["committee_id"].dropna().astype(int))])
        hint = ('<span class="ais-cal-hint">Double-click a day to add · drag to move</span>' if can_add else "")
        shell.html(f'<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">'
                   f'{legend}{hint}</div>')
        clicked = clickable.html(_month_grid(year, month, term_events), key=f"events_cal_{term_id}_{year}_{month}")
        if clicked and clicked.startswith("add:"):
            to_add = date.fromisoformat(clicked[4:])
        elif clicked and clicked.startswith("move:"):
            _, moved_id, moved_day = clicked.split(":")
            _move(int(moved_id), date.fromisoformat(moved_day))
        elif clicked:
            to_open = int(clicked)
    else:
        _actions(None)
else:
    _actions(None)
    ordered = term_events.sort_values("day").reset_index(drop=True)
    table = pd.DataFrame({
        "Date": ordered["day"],
        "Event": ordered["title"],
        "Committee": ordered["committee_id"].map(lambda c: committee_names.get(int(c), "") if pd.notna(c) else ""),
        "Location": ordered["location"].fillna(""),
        "Attendance": pd.to_numeric(ordered["attendance"], errors="coerce"),
        "Spent": ordered["Spent"].where(ordered["Spent"] > 0),
        "Per person": (ordered["Spent"] / pd.to_numeric(ordered["attendance"], errors="coerce"))
                      .where((ordered["Spent"] > 0) & ordered["attendance"].notna()),
        "Status": ordered["status"].fillna(""),
    })
    selection = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "Date": st.column_config.DateColumn(format="ddd MMM D"),
            "Attendance": st.column_config.NumberColumn(
                format="%d",
                help="From the schedule sheet or, for GBMs, the member database's check-in columns.",
            ),
            "Spent": st.column_config.NumberColumn(format="dollar", help="Only purchases linked to the event."),
            "Per person": st.column_config.NumberColumn(format="dollar"),
        },
        height=460,
        on_select="rerun",
        selection_mode="single-row",
        key=f"events_list_{term_id}",
    )
    rows = list(getattr(getattr(selection, "selection", None), "rows", []) or [])
    picked_row = int(ordered.at[rows[0], "event_id"]) if rows else None
    # Open on a new selection only, so closing the dialog (which reruns the
    # page with the row still selected) doesn't open it again.
    seen_key = f"_events_list_seen_{term_id}"
    if picked_row != st.session_state.get(seen_key):
        st.session_state[seen_key] = picked_row
        if picked_row is not None:
            to_open = picked_row

# --- Spending -> events -------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    "Purchases that may belong to an event",
    shell.pill(str(len(to_review)), "over" if to_review else "on track"),
    help="Matched on the date, the merchant appearing in the event's location or name, the kind of "
    "merchant, and the committee it's already categorized to. "
    + ("Only your committee's purchases are listed. Tick Link to add one to its event, or pick the right "
       "event in the Event column. To move or unlink a purchase later, ask the treasurer."
       if scoped else
       "Tick Link to accept a match. If the event is wrong, pick the right one in the Event column; "
       "changing it counts as accepting. To move or unlink a purchase later, open its event above."),
)

confidence = st.segmented_control(
    "Show", ["High", "Medium", "Low"], default=["High", "Medium"], selection_mode="multi", key="events_confidence",
)
shown = [s for s in term_suggestions if s.confidence in (confidence or [])]
if not shown:
    shell.empty_state("Nothing to match", "No unlinked purchases near this semester's events at that confidence.")
else:
    transactions = money.set_index("transactionid")
    frame = pd.DataFrame([
        {
            "Link": s.confidence == events_domain.HIGH,
            "Date": pd.to_datetime(transactions.at[s.transaction_id, "transaction_date"]).date(),
            "Purchase": events_domain.purchase_label(transactions.at[s.transaction_id, "details"])[:48],
            "Amount": -float(transactions.at[s.transaction_id, "amount"]),
            "Event": s.event_id,
            "Confidence": s.confidence,
            "Why": s.reason,
            "_transaction": s.transaction_id,
            "_event": s.event_id,
        }
        for s in sorted(shown, key=lambda s: (-s.score, s.transaction_id))
    ])
    # Any of this identity's events in the term can be picked instead of the guess.
    choices = [int(e) for e in term_events.loc[term_events["committee_id"].map(is_mine)].sort_values("day")["event_id"]]
    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        disabled=[c for c in frame.columns if c not in ("Link", "Event")] if can_add else True,
        column_order=(["Link"] if can_add else []) + ["Date", "Purchase", "Amount", "Event", "Confidence", "Why"],
        column_config={
            "Link": st.column_config.CheckboxColumn(width="small"),
            "Date": st.column_config.DateColumn(format="MMM D"),
            "Amount": st.column_config.NumberColumn(format="dollar"),
            "Event": st.column_config.SelectboxColumn(
                options=choices, format_func=_event_name, required=True, width="medium",
                help="The likeliest event. Pick another if it's wrong." if can_add else None,
            ),
            "Why": st.column_config.TextColumn(width="large"),
        },
        key=f"events_suggestions_{term_id}",
    )
    if can_add:
        moved = edited["Event"].astype(int) != edited["_event"].astype(int)
        chosen = edited[edited["Link"] | moved]
        if st.button(f"Link {len(chosen)} purchase(s)", type="primary", disabled=chosen.empty, key="events_link"):
            result = repo.link_transactions_to_events(
                list(zip(chosen["_transaction"].astype(int), chosen["Event"].astype(int))), identity.email
            )
            if result.error:
                shell.notify("error", result.error)
            else:
                st.toast(f"Linked {result.updated} purchase(s).")
                st.rerun()

if to_add is not None:
    _open_add(to_add)
elif to_open is not None:
    _open(int(to_open))
