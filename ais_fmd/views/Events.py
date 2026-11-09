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

Live Google Calendar sync is shown as a preview only: the sandbox has no
network, and the calendar ID is read from the workbook's calculation column.

A VP (`identity.committee_scoped`) gets this page too. VPs work as one team,
so they see every club event (titles, dates, places, attendance), but money -- what an event cost, purchases that may
belong to one -- is worked out from their own committee's transactions only,
and shown only on their own committee's events. They can add their own
committee's unlinked purchases to their own committee's events (2026-11-09);
syncing, moving and unlinking stay with the treasurer.
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
from ais_fmd.domain import events as events_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import ordered_semesters, term_id_for_semester
from ais_fmd.ui import calendar_grid, clickable, committee_charts, shell, theme

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
        shell.heading("Google Calendar", shell.pill("Preview · not connected", "muted"))
        short_id = f"{calendar_id[:14]}…{calendar_id.split('@')[-1]}" if calendar_id else "not found yet"
        shell.say(
            f"AIS Events calendar (`{short_id}`), found in the schedule workbook. Connected, it "
            "would sync every hour. The sandbox has no network, so for now events come from the "
            "spreadsheets on the right.",
            caption=True,
        )
        st.button(
            "Connect Google Calendar", icon=":material/calendar_month:", disabled=True,
            help="Needs Google sign-in, which isn't available in the sandbox.",
            key="events_connect_google",
        )
    with files_column, shell.card("events-files", order=2):
        shell.heading("Synced from", shell.pill(f"{len(all_events)} events", "muted"))
        if sources.empty:
            shell.say("Nothing yet. Upload an event schedule workbook or an .ics file.", caption=True)
        for source, row in sources.iterrows():
            # SQLite's CURRENT_TIMESTAMP is UTC; show local time.
            synced = pd.to_datetime(row["synced"], errors="coerce", utc=True)
            when = f"{synced.tz_convert(datetime.now().astimezone().tzinfo):%b %d, %-I:%M %p}" if not pd.isna(synced) else ""
            shell.say(f"**{escape(str(source).split(': ', 1)[-1])}**  \n{row['events']} events · {when}", caption=True)
        if can_edit:
            with st.popover("Sync from a file", icon=":material/sync:"):
                _upload_form()


def _upload_form() -> None:
    upload = st.file_uploader(
        "Event schedule (.xlsx) or calendar export (.ics)", type=["xlsx", "ics"], key="events_upload"
    )
    if upload is None:
        return
    parsed = events_domain.parse_upload(upload.getvalue(), upload.name, bundle.terms)
    known = set(all_events["external_key"]) if not all_events.empty else set()
    new = sum(1 for e in parsed.events if e.key not in known)
    shell.say(
        f"{len(parsed.events)} events in {', '.join(parsed.sheets_read) or 'the file'}: "
        f"{new} new, {len(parsed.events) - new} already here (updated).",
        caption=True,
    )
    for sheet, count in parsed.skipped_outside_term.items():
        shell.say(f"{sheet}: skipped {count} rows dated outside that semester.", caption=True)
    for note in parsed.notes:
        shell.say(note, caption=True)
    if st.button("Sync these events", type="primary", disabled=not parsed.events, key="events_sync"):
        result = repo.sync_events([e.as_row() for e in parsed.events], identity.email)
        if result.error:
            shell.notify("error", result.error)
        else:
            st.toast(f"Synced: {result.updated} new, {result.unchanged} updated.")
            st.rerun()


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
    view = st.segmented_control("View", ["Month", "List"], default="Month", key="events_view") or "Month"

spending = events_domain.event_spending(money, links)
term_events = term_events.merge(spending, on="event_id", how="left")
term_events["Spent"] = term_events["Spent"].fillna(0.0)
term_events["Purchases"] = term_events["Purchases"].fillna(0).astype(int)
# Another committee's event shows no money to a VP, even one of their own
# purchases linked to it -- the event's cost belongs to its committee.
term_events.loc[~term_events["committee_id"].map(is_mine), ["Spent", "Purchases"]] = 0

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
    ("events-linked", "Your spending linked" if scoped else "Spending linked", float(costed["Spent"].sum()), True,
     f"across {len(costed):,} events"),
    ("events-attendance", "Average attendance", f"{attended.mean():.0f}" if len(attended) else "—", False,
     f"{len(attended):,} events with a count"),
    ("events-review", "Purchases to match", f"{len(to_review):,}", False,
     "the treasurer confirms these" if scoped else "likely belong to an event"),
]
for column, (name, label, value, currency, caption) in zip(st.columns(4, gap="medium"), tiles):
    with column, shell.card(name, order=2):
        shell.metric(st, label, value, currency=currency)
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


def _event_panel(event_id: int) -> None:
    if event_id not in event_lookup.index:
        shell.say("This event is no longer on the calendar.", caption=True)
        return
    event = event_lookup.loc[event_id]
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
    shell.html(
        f'<div class="ais-event-facts">{shell.pill(escape(owner or "No committee"), "muted")}'
        f'<span>{" · ".join(escape(str(f)) for f in facts)}</span></div>'
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
        shell.heading("Spending")
        shell.say(f"What this event cost is visible to the {escape(owner or 'committee')} VP and the treasurer.", caption=True)
        return

    links_now = repo.load_event_links()
    linked_ids = links_now.loc[links_now["event_id"] == event_id, "transaction_id"].astype(int).tolist() if not links_now.empty else []
    purchases = money[money["transactionid"].isin(linked_ids)].sort_values("transaction_date")
    total = float(-pd.to_numeric(purchases["amount"], errors="coerce").sum()) if not purchases.empty else 0.0
    per_person = (f" · {format_currency(total / event['attendance'])} per person"
                  if total and pd.notna(event["attendance"]) and event["attendance"] else "")
    shell.heading("Spending", shell.pill(f"{format_currency(total)}{per_person}", "muted"))
    if purchases.empty:
        shell.say("No purchases linked yet.", caption=True)
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

    nearby = _money_near(day)
    here = [s for s in events_domain.suggest_links(nearby, all_events, links_now, committees) if s.event_id == event_id]
    if here:
        shell.heading("Suggested for this event", shell.pill(str(len(here)), "muted"))
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
        if not can_add:
            shell.say("The treasurer confirms these.", caption=True)

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


def _open(event_id: int) -> None:
    title = str(event_lookup.loc[event_id, "title"]) if event_id in event_lookup.index else "Event"
    st.dialog(title, width="large", on_dismiss="rerun")(_event_panel)(event_id)


to_open: int | None = st.session_state.pop(OPEN_KEY, None)

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
        owner = committee_names.get(int(event.committee_id), "") if pd.notna(event.committee_id) else ""
        chips.setdefault(event.day, []).append(calendar_grid.Chip(
            title=str(event.title), meta_html=" · ".join(meta), color=_color(event.committee_id),
            tip=f"{event.title} · {event.location or ''} · {owner}", pick=str(int(event.event_id)),
        ))
    return calendar_grid.month_grid(year, month, chips, today=TODAY)


if view == "Month":
    months = sorted({(d.year, d.month) for d in term_events["day"]})
    if months:
        labels = [f"{calendar.month_abbr[m]} {y}" for y, m in months]
        default = next((label for (y, m), label in zip(months, labels) if (y, m) == (TODAY.year, TODAY.month)), labels[-1])
        month_column, legend_column = st.columns([3, 2], vertical_alignment="center")
        with month_column:
            month_label = st.segmented_control("Month", labels, default=default, key=f"events_month_{term_id}",
                                               label_visibility="collapsed") or default
        year, month = months[labels.index(month_label)]
        with legend_column:
            legend = shell.legend([(committee_names.get(c, ""), color) for c, color in committee_charts.committee_colors().items()
                                   if c in set(term_events["committee_id"].dropna().astype(int))])
            shell.html(f'<div style="display:flex;justify-content:flex-end">{legend}</div>')
        clicked = clickable.html(_month_grid(year, month, term_events), key=f"events_cal_{term_id}_{year}_{month}")
        if clicked:
            to_open = int(clicked)
        shell.say("Click an event to see its details and purchases.", caption=True)
else:
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
            "Attendance": st.column_config.NumberColumn(format="%d"),
            "Spent": st.column_config.NumberColumn(format="dollar"),
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
    shell.say(
        "Click a row to see the event's details and purchases. Attendance comes from the schedule "
        "sheet or, for GBMs, the member database's check-in columns. Spent counts only purchases "
        "linked to the event.",
        caption=True,
    )

# --- Spending -> events -------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
shell.heading(
    "Purchases that may belong to an event",
    shell.pill(str(len(to_review)), "over" if to_review else "on track"),
)
shell.say(
    "Matched on the date, the merchant appearing in the event's location or name, the kind of "
    "merchant, and the committee it's already categorized to. "
    + ("Only your committee's purchases are listed. Tick Link to add one to its event, or pick the right "
       "event in the Event column. To move or unlink a purchase later, ask the treasurer."
       if scoped else
       "Tick Link to accept a match. If the event is wrong, pick the right one in the Event column; "
       "changing it counts as accepting. To move or unlink a purchase later, open its event above."),
    caption=True,
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

if to_open is not None:
    _open(int(to_open))
