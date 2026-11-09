"""
Events -- the club calendar, and what each event cost (domain/events.py).

Events come from the AIS Event Schedule workbooks Membership already keeps (or
an .ics export of the Google Calendar they feed). Spending is tied to an event
by confirming a suggestion: the app proposes the likeliest event for each
purchase and says why, and a treasurer ticks the ones that are right.

Live Google Calendar sync is shown as a preview only: the sandbox has no
network, and the calendar ID is read from the workbook's calculation column.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from html import escape

import pandas as pd
import streamlit as st

from ais_fmd import auth
from ais_fmd.data import repositories as repo
from ais_fmd.domain import events as events_domain
from ais_fmd.domain.money import format_currency
from ais_fmd.domain.terms import ordered_semesters, term_id_for_semester
from ais_fmd.ui import shell, theme

auth.require(auth.Role.MEMBER)
identity = auth.current_user()
can_edit = identity.can(auth.Role.TREASURER)
TODAY = date.today()

shell.environment_banner()
shell.page_header(
    "Events",
    "The club calendar next to the money: what happened when, who came, and "
    "what each event cost.",
)

bundle = repo.load_bundle()
all_events = repo.load_events()
links = repo.load_event_links()
committees = repo.load_committees()
committee_names = dict(zip(committees["CommitteeID"].astype(int), committees["Committee_Name"]))

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
    scope = st.selectbox(
        "Committee", ["All committees", *[committee_names.get(c, str(c)) for c in present]], key="events_committee"
    )
if scope != "All committees":
    wanted = next(c for c in present if committee_names.get(c) == scope)
    term_events = term_events[term_events["committee_id"] == wanted]
with view_column:
    view = st.segmented_control("View", ["Month", "List"], default="Month", key="events_view") or "Month"

spending = events_domain.event_spending(bundle.transactions, links)
term_events = term_events.merge(spending, on="event_id", how="left")
term_events["Spent"] = term_events["Spent"].fillna(0.0)
term_events["Purchases"] = term_events["Purchases"].fillna(0).astype(int)

suggestions = events_domain.suggest_links(bundle.transactions, all_events, links, committees)
term_ids = set(term_events["event_id"])
term_suggestions = [s for s in suggestions if s.event_id in term_ids]

# --- Headline -----------------------------------------------------------------

held = term_events[term_events["day"] <= TODAY]
attended = held["attendance"].dropna()
costed = term_events[term_events["Spent"] > 0]
to_review = [s for s in term_suggestions if s.confidence != events_domain.LOW]
tiles = [
    ("events-count", "Events", f"{len(term_events):,}", False,
     f"{len(held):,} held, {len(term_events) - len(held):,} to come"),
    ("events-linked", "Spending linked", float(costed["Spent"].sum()), True, f"across {len(costed):,} events"),
    ("events-attendance", "Average attendance", f"{attended.mean():.0f}" if len(attended) else "—", False,
     f"{len(attended):,} events with a count"),
    ("events-review", "Purchases to match", f"{len(to_review):,}", False, "likely belong to an event"),
]
for column, (name, label, value, currency, caption) in zip(st.columns(4, gap="medium"), tiles):
    with column, shell.card(name, order=2):
        shell.metric(st, label, value, currency=currency)
        shell.say(caption, caption=True)

# --- Month grid ---------------------------------------------------------------

COLORS = {
    events_domain.MEMBERSHIP: theme.ACCENT,
    events_domain.PRESIDENT: theme.INK,
    events_domain.OPERATIONS: theme.INK_MUTE,
    events_domain.CONSULTING: "#2F6FDB",
    events_domain.PROFESSIONAL_DEVELOPMENT: theme.POSITIVE,
    events_domain.MARKETING: theme.WARNING,
}


def _color(committee_id) -> str:
    return COLORS.get(int(committee_id), theme.INK_MUTE) if pd.notna(committee_id) else theme.INK_MUTE


CALENDAR_CSS = f"""
<style>
.ais-cal {{ display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:6px; margin-top:.5rem; }}
.ais-cal-dow {{ font-size:.72rem; color:{theme.INK_MUTE}; text-transform:uppercase; letter-spacing:.04em; padding:0 6px; }}
.ais-cal-day {{ background:{theme.CARD}; border-radius:12px; min-height:104px; padding:6px; box-shadow:{theme.SHADOW};
  display:flex; flex-direction:column; gap:4px; overflow:hidden; }}
.ais-cal-day.out {{ background:transparent; box-shadow:none; }}
.ais-cal-day.today {{ outline:2px solid {theme.ACCENT}; }}
.ais-cal-num {{ font-size:.75rem; color:{theme.INK_SOFT}; font-weight:600; }}
.ais-cal-ev {{ font-size:.74rem; line-height:1.2; padding:3px 6px; border-radius:6px; background:{theme.SUBTLE};
  border-left:3px solid var(--c); color:{theme.INK}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.ais-cal-ev.past {{ opacity:.82; }}
.ais-cal-ev b {{ font-weight:600; }}
.ais-cal-meta {{ display:block; color:{theme.INK_MUTE}; font-size:.68rem; }}
.ais-cal-ev .spent {{ color:{theme.ACCENT_DIM}; font-weight:600; }}
</style>
"""


def _month_grid(year: int, month: int, rows: pd.DataFrame) -> str:
    by_day: dict[date, list] = {}
    for row in rows.sort_values(["day", "start_time"], na_position="last").itertuples():
        by_day.setdefault(row.day, []).append(row)
    cells = [f'<div class="ais-cal-dow">{name}</div>' for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    for week in calendar.Calendar().monthdatescalendar(year, month):
        for day in week:
            classes = ["ais-cal-day"] + (["out"] if day.month != month else []) + (["today"] if day == TODAY else [])
            chips = []
            for event in by_day.get(day, []) if day.month == month else []:
                meta = []
                if isinstance(event.start_time, str) and event.start_time:
                    meta.append(escape(event.start_time))
                if pd.notna(event.attendance):
                    meta.append(f"{int(event.attendance)} came")
                if event.Spent > 0:
                    meta.append(f'<span class="spent">${event.Spent:,.0f}</span>')
                title = escape(str(event.title))
                tip = escape(f"{event.title} · {event.location or ''} · {committee_names.get(int(event.committee_id), '') if pd.notna(event.committee_id) else ''}")
                chips.append(
                    f'<div class="ais-cal-ev{" past" if day < TODAY else ""}" style="--c:{_color(event.committee_id)}" title="{tip}">'
                    f'<b>{title}</b><span class="ais-cal-meta">{" · ".join(meta) or "&nbsp;"}</span></div>'
                )
            number = day.day if day.month == month else ""
            cells.append(f'<div class="{" ".join(classes)}"><span class="ais-cal-num">{number}</span>{"".join(chips)}</div>')
    return CALENDAR_CSS + f'<div class="ais-cal">{"".join(cells)}</div>'


if view == "Month":
    months = sorted({(d.year, d.month) for d in term_events["day"]})
    if months:
        labels = [f"{calendar.month_abbr[m]} {y}" for y, m in months]
        default = next((label for (y, m), label in zip(months, labels) if (y, m) == (TODAY.year, TODAY.month)), labels[-1])
        month_label = st.segmented_control("Month", labels, default=default, key=f"events_month_{term_id}",
                                           label_visibility="collapsed") or default
        year, month = months[labels.index(month_label)]
        legend = shell.legend([(committee_names.get(c, ""), color) for c, color in COLORS.items()
                               if c in set(term_events["committee_id"].dropna().astype(int))])
        shell.html(f'<div style="display:flex;justify-content:flex-end">{legend}</div>')
        shell.html(_month_grid(year, month, term_events))
else:
    table = term_events.sort_values("day")
    table = pd.DataFrame({
        "Date": table["day"],
        "Event": table["title"],
        "Committee": table["committee_id"].map(lambda c: committee_names.get(int(c), "") if pd.notna(c) else ""),
        "Location": table["location"],
        "Attendance": table["attendance"],
        "Spent": table["Spent"].where(table["Spent"] > 0),
        "Per person": (table["Spent"] / table["attendance"]).where((table["Spent"] > 0) & table["attendance"].notna()),
        "Status": table["status"],
    })
    shell.dataframe(
        table,
        column_config={
            "Date": st.column_config.DateColumn(format="ddd MMM D"),
            "Attendance": st.column_config.NumberColumn(format="%d"),
            "Spent": st.column_config.NumberColumn(format="$%.2f"),
            "Per person": st.column_config.NumberColumn(format="$%.2f"),
        },
        height=460,
    )
    shell.say(
        "Attendance comes from the schedule sheet or, for GBMs, the member database's check-in "
        "columns. Spent counts only purchases linked to the event.",
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
    "merchant, and the committee it's already categorized to. Nothing is linked until you tick it. "
    "Later the Assistant (Haiku) can explain or second-guess these.",
    caption=True,
)

confidence = st.segmented_control(
    "Show", ["High", "Medium", "Low"], default=["High", "Medium"], selection_mode="multi", key="events_confidence",
)
shown = [s for s in term_suggestions if s.confidence in (confidence or [])]
if not shown:
    shell.empty_state("Nothing to match", "No unlinked purchases near this semester's events at that confidence.")
else:
    transactions = bundle.transactions.set_index("transactionid")
    event_lookup = all_events.set_index("event_id")
    frame = pd.DataFrame([
        {
            "Link": s.confidence == events_domain.HIGH,
            "Date": pd.to_datetime(transactions.at[s.transaction_id, "transaction_date"]).date(),
            "Purchase": events_domain.purchase_label(transactions.at[s.transaction_id, "details"])[:48],
            "Amount": -float(transactions.at[s.transaction_id, "amount"]),
            "Event": f"{event_lookup.at[s.event_id, 'title']} ({pd.to_datetime(event_lookup.at[s.event_id, 'event_date']):%b %d})",
            "Confidence": s.confidence,
            "Why": s.reason,
            "_transaction": s.transaction_id,
            "_event": s.event_id,
        }
        for s in sorted(shown, key=lambda s: (-s.score, s.transaction_id))
    ])
    edited = st.data_editor(
        frame,
        hide_index=True,
        width="stretch",
        disabled=[c for c in frame.columns if c != "Link"] if can_edit else True,
        column_order=["Link", "Date", "Purchase", "Amount", "Event", "Confidence", "Why"],
        column_config={
            "Link": st.column_config.CheckboxColumn(width="small"),
            "Date": st.column_config.DateColumn(format="MMM D"),
            "Amount": st.column_config.NumberColumn(format="$%.2f"),
            "Why": st.column_config.TextColumn(width="large"),
        },
        key=f"events_suggestions_{term_id}",
    )
    if can_edit:
        chosen = edited[edited["Link"]]
        if st.button(f"Link {len(chosen)} purchase(s)", type="primary", disabled=chosen.empty, key="events_link"):
            result = repo.link_transactions_to_events(
                list(zip(chosen["_transaction"].astype(int), chosen["_event"].astype(int))), identity.email
            )
            if result.error:
                shell.notify("error", result.error)
            else:
                st.toast(f"Linked {result.updated} purchase(s).")
                st.rerun()

# --- One event ----------------------------------------------------------------

st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
st.markdown("#### Event details")
options = term_events.sort_values("day")
labels = {int(r.event_id): f"{r.day:%b %d} · {r.title}" for r in options.itertuples()}
default_event = next((eid for eid, r in zip(options["event_id"], options.itertuples()) if r.Spent > 0), None)
picked = st.selectbox(
    "Event", list(labels), format_func=labels.get, key=f"events_pick_{term_id}",
    index=list(labels).index(default_event) if default_event in labels else 0,
)
event = options[options["event_id"] == picked].iloc[0]
info_column, spend_column = st.columns([2, 3], gap="medium")
with info_column, shell.card("events-detail", order=3):
    shell.heading(escape(str(event["title"])), shell.pill(committee_names.get(int(event["committee_id"]), "—") if pd.notna(event["committee_id"]) else "—", "muted"))
    when = f"{event['day']:%A, %b %d}" + (f" · {event['start_time']}" if pd.notna(event["start_time"]) else "") + (
        f"–{event['end_time']}" if pd.notna(event["end_time"]) else "")
    lines = [when]
    for label, key in (("Where", "location"), ("Owner", "owner"), ("Audience", "audience"), ("Status", "status")):
        if pd.notna(event[key]):
            lines.append(f"{label}: {event[key]}")
    if pd.notna(event["attendance"]):
        lines.append(f"Attendance: {int(event['attendance'])} ({event['attendance_source']})")
    shell.say("  \n".join(escape(str(line)) for line in lines), caption=True)
    if pd.notna(event["description"]):
        shell.say(escape(str(event["description"])), caption=True)
with spend_column, shell.card("events-spend", order=4):
    linked_ids = links.loc[links["event_id"] == picked, "transaction_id"].astype(int).tolist() if not links.empty else []
    purchases = bundle.transactions[bundle.transactions["transactionid"].isin(linked_ids)]
    total = -pd.to_numeric(purchases["amount"], errors="coerce").sum() if not purchases.empty else 0.0
    per_person = (
        f" · {format_currency(total / event['attendance'])} per person"
        if total and pd.notna(event["attendance"]) and event["attendance"] else ""
    )
    shell.heading("Spending", shell.pill(f"{format_currency(total)}{per_person}", "muted"))
    if purchases.empty:
        shell.say("No purchases linked yet.", caption=True)
    else:
        shell.dataframe(
            pd.DataFrame({
                "Date": pd.to_datetime(purchases["transaction_date"]).dt.date,
                "Purchase": purchases["details"].map(events_domain.purchase_label).str[:48],
                "Amount": -pd.to_numeric(purchases["amount"], errors="coerce"),
            }),
            column_config={"Amount": st.column_config.NumberColumn(format="$%.2f"),
                           "Date": st.column_config.DateColumn(format="MMM D")},
        )
        if can_edit:
            unlink = st.selectbox(
                "Unlink a purchase", [None, *purchases["transactionid"].astype(int)],
                format_func=lambda t: "—" if t is None else str(
                    purchases.loc[purchases["transactionid"] == t, "details"].iloc[0])[:48],
                key=f"events_unlink_{picked}",
            )
            if unlink is not None and st.button("Unlink", key="events_unlink_go"):
                repo.unlink_transaction_from_event(int(unlink), identity.email)
                st.rerun()
