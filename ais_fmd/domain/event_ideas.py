"""
Ideas for a VP's event plan, from the events their committee has already held.

The schedule sheets name events one at a time ("UF/Kentucky Watch Party",
"GBM # 3 - KPMG", "Passport: Pumpkins"). For planning, what matters is the kind
of event -- a watch party, a GBM, a Passport outing -- and what that kind has
cost and drawn before. `event_kind` reduces a title to (kind, detail): the
detail keeps what made that one different (which game, which speaker), so a
tailgate is planned as "a tailgate" with the games listed, not as a repeat of one
particular game.

Cost comes only from purchases confirmed as belonging to an event (`event_links`)
and only from money the caller may see -- the page passes its own committee's
transactions, so another committee's spending never shapes a VP's ideas.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

# (pattern, kind). First match wins; the detail is whatever is left of the title.
_RULES: tuple[tuple[str, str], ...] = (
    (r"watch party", "Watch party"),
    (r"tailgate", "Tailgate"),
    (r"away game", "Away game"),
    (r"gbm after hours", "GBM after hours"),
    (r"\bgbm\b", "GBM"),
    (r"workshop", "Workshop"),
    (r"passport", "Passport outing"),
    (r"consulting.*(final presentation)", "Consulting final presentations"),
    (r"consulting.*(social)", "Consulting social"),
    (r"consulting.*(kickoff|onboarding)", "Consulting kickoff"),
    (r"consulting.*(meeting)", "Consulting meeting"),
    (r"\b(bar )?tab\b", "Bar tab"),
    (r"coffee", "Coffee & pastries"),
    (r"study (hours|session)", "Study hours"),
    (r"headshot", "Headshots"),
    (r"semi[- ]?formal", "Semi-formal"),
    (r"\bformal\b", "Formal"),
    (r"basketball", "Basketball game"),
    (r"baseball", "Baseball game"),
    (r"karaoke", "Karaoke night"),
    (r"bar golf", "Bar golf"),
    (r"mixer", "Mixer"),
    (r"redneck wedding", "Redneck Wedding"),
    (r"champagne (&|and) shackles", "Champagne & Shackles"),
    (r"brunch", "Brunch"),
    (r"potluck", "Potluck"),
    (r"bonfire", "Bonfire"),
)
# Calendar notes, not events anyone spends on.
_NOT_EVENTS = re.compile(
    r"apps (open|close)|commencement|graduation|\beaster\b|^orientation$|orientation\b(?!.*tab)", re.I
)
_NUMBERING = re.compile(r"#\s*\d+|\b\d+\b|[?!]+")
_UF = re.compile(r"^\s*uf\s*(/|vs\.?|v\.?)\s*", re.I)


# Kinds where the words before the keyword name the particular one (the game, the bar).
_NAMED_BEFORE = {"Watch party", "Tailgate", "Away game", "Bar tab"}


def _subtitle(text: str) -> str:
    """The part after ' - ', ':' or ',' in what follows the keyword, if there is one."""
    split = re.search(r"(?:\s+-\s+|:\s*|,\s*)(.*)$", text)
    return _clean(split.group(1)) if split else ""


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", _NUMBERING.sub(" ", text)).strip(" -:,.&")
    return text


def event_kind(title: object) -> tuple[str | None, str]:
    """
    'UF/Kentucky Watch Party' -> ('Watch party', 'Kentucky');
    'GBM # 3 - KPMG' -> ('GBM', 'KPMG'); 'VP Apps Close' -> (None, '').
    """
    raw = " ".join(str(title or "").split())
    if not raw or _NOT_EVENTS.search(raw):
        return None, ""
    for pattern, kind in _RULES:
        match = re.search(pattern, raw, re.I)
        if match:
            if kind in _NAMED_BEFORE:
                # "UF/Kentucky Watch Party", "Opus Tab": what comes before says which one.
                before = _clean(_UF.sub("", raw[: match.start()]))
                after = _subtitle(raw[match.end():])
                return kind, before or after
            # Otherwise only an explicit subtitle counts: "GBM # 3 - KPMG",
            # "Passport: Pumpkins". "Coffee & Pastries" has none.
            return kind, _subtitle(raw[match.end():])
    split = re.match(r"(.*?)(?:\s+-\s+|:\s*)(.*)$", raw)
    head, tail = (split.group(1), split.group(2)) if split else (raw, "")
    kind = _clean(head)
    return (kind[:1].upper() + kind[1:]) if kind else None, _clean(tail)


def ideas(
    events: pd.DataFrame,
    spending: pd.DataFrame,
    links: pd.DataFrame,
    *,
    today: date | None = None,
) -> pd.DataFrame:
    """
    One row per kind of past event, most often held first.

    Columns: Kind, Times, Last held (date), Last title, Average attendance, Last
    attendance, Average cost (None when no held event has linked spending),
    Cost per person, Examples (the details, most recent first: which games,
    which speakers).
    """
    columns = ["Kind", "Times", "Last held", "Last title", "Average attendance", "Last attendance",
               "Average cost", "Cost per person", "Examples"]
    if events is None or events.empty:
        return pd.DataFrame(columns=columns)
    today = today or date.today()
    held = events.copy()
    held["day"] = pd.to_datetime(held["event_date"], errors="coerce").dt.date
    held = held[held["day"].notna() & (held["day"] < today)]
    if held.empty:
        return pd.DataFrame(columns=columns)
    kinds = held["title"].map(event_kind)
    held["kind"] = kinds.map(lambda pair: pair[0])
    held["detail"] = kinds.map(lambda pair: pair[1])
    held = held[held["kind"].notna()]

    cost = pd.Series(dtype=float)
    if links is not None and not links.empty and spending is not None and not spending.empty:
        out = spending[pd.to_numeric(spending["amount"], errors="coerce") < 0]
        joined = links.merge(out[["transactionid", "amount"]], left_on="transaction_id", right_on="transactionid")
        cost = (-joined.groupby("event_id")["amount"].sum()).astype(float)
    held["cost"] = held["event_id"].map(cost)

    rows = []
    for kind, group in held.sort_values("day").groupby("kind", sort=False):
        last = group.iloc[-1]
        attendance = pd.to_numeric(group["attendance"], errors="coerce").dropna()
        costed = group["cost"].dropna()
        per_person = (group["cost"] / pd.to_numeric(group["attendance"], errors="coerce")).dropna()
        examples = [d for d in dict.fromkeys(group["detail"].iloc[::-1]) if d][:4]
        rows.append({
            "Kind": kind,
            "Times": len(group),
            "Last held": last["day"],
            "Last title": last["title"],
            "Average attendance": round(float(attendance.mean())) if len(attendance) else None,
            # The most recent count there is: the last event may not have one yet.
            "Last attendance": int(attendance.iloc[-1]) if len(attendance) else None,
            "Average cost": round(float(costed.mean()), 2) if len(costed) else None,
            "Cost per person": round(float(per_person.mean()), 2) if len(per_person) else None,
            "Examples": ", ".join(examples),
        })
    frame = pd.DataFrame(rows, columns=columns)
    return frame.sort_values(["Times", "Last held"], ascending=[False, False], ignore_index=True)


# --- Turning ideas into a plan, and checking what a VP typed -------------------

# Kinds tied to one game: planned as the kind ("Tailgate"), with the past games
# as a hint, because next season's opponents are different (2026-11-09).
GAME_KINDS = {"Tailgate", "Watch party", "Away game"}
MAX_TITLE = 120
MAX_NOTE = 200
MAX_COST = 50_000.0

_WINDOWS = {"Fall": ((8, 15), (12, 15)), "Spring": ((1, 5), (5, 5)), "Summer": ((5, 10), (8, 10))}


def guess_window(semester: str | None) -> tuple[date, date] | None:
    """Rough dates for a semester the treasurer hasn't set up yet ('Spring 2027')."""
    match = re.match(r"^\s*(fall|spring|summer)\s+(\d{4})\s*$", semester or "", re.I)
    if not match:
        return None
    (sm, sd), (em, ed) = _WINDOWS[match.group(1).title()]
    year = int(match.group(2))
    return date(year, sm, sd), date(year, em, ed)


def _round_up(amount: float, step: int = 5) -> float:
    return float(-(-amount // step) * step) if amount > 0 else 0.0


def plan_rows_from_ideas(picked: pd.DataFrame) -> list[dict]:
    """New planned events from chosen ideas: cost and attendance start at the past average."""
    rows = []
    for idea in picked.to_dict("records"):
        kind = idea["Kind"]
        examples = idea.get("Examples") or ""
        if kind in GAME_KINDS:
            note = f"Which game? Past: {examples}" if examples else "Which game?"
        else:
            note = f"Last time: {idea.get('Last title') or ''}".strip()
        cost = idea.get("Average cost")
        people = idea.get("Average attendance")
        rows.append({
            "event_date": None,
            "title": kind,
            "kind": kind,
            "estimated_cost": _round_up(float(cost)) if cost is not None and not pd.isna(cost) else 0.0,
            "expected_attendance": int(people) if people is not None and not pd.isna(people) else None,
            "note": note[:MAX_NOTE],
        })
    return rows


def clean_planned_events(
    frame: pd.DataFrame, window: tuple[date, date] | None, semester: str
) -> tuple[list[dict], list[str]]:
    """
    The rows of the event-plan editor, checked: a name, a cost from $0, a whole
    number of people, and a date inside the semester (or none yet). Problems are
    sentences for the VP; nothing is saved while there are any.
    """
    events, problems = [], []
    for row in frame.to_dict("records"):
        title = " ".join(str(row.get("title") or "").split())
        if not title:
            if any(row.get(k) not in (None, "") and not (isinstance(row.get(k), float) and pd.isna(row.get(k)))
                   for k in ("estimated_cost", "note", "event_date")):
                problems.append("Every event needs a name.")
            continue
        if len(title) > MAX_TITLE:
            problems.append(f"'{title[:30]}…' is too long a name (keep it under {MAX_TITLE} characters).")
            continue
        cost = pd.to_numeric(row.get("estimated_cost"), errors="coerce")
        cost = 0.0 if pd.isna(cost) else float(cost)
        if cost < 0 or cost > MAX_COST:
            problems.append(f"{title}: the cost has to be between $0 and ${MAX_COST:,.0f}.")
            continue
        people = pd.to_numeric(row.get("expected_attendance"), errors="coerce")
        if not pd.isna(people) and people < 0:
            problems.append(f"{title}: attendance can't be negative.")
            continue
        day = row.get("event_date")
        day = None if day is None or (isinstance(day, float) and pd.isna(day)) or pd.isna(pd.to_datetime(day, errors="coerce")) \
            else pd.to_datetime(day).date()
        if day is not None and window is not None and not (window[0] <= day <= window[1]):
            problems.append(f"{title}: {day:%b %d, %Y} isn't in {semester} "
                            f"({window[0]:%b %d} to {window[1]:%b %d}).")
            continue
        note = " ".join(str(row.get("note") or "").split())[:MAX_NOTE] or None
        kind = row.get("kind")
        events.append({
            "event_date": None if day is None else day.isoformat(),
            "title": title,
            "kind": None if kind is None or (isinstance(kind, float) and pd.isna(kind)) else str(kind),
            "estimated_cost": round(cost, 2),
            "expected_attendance": None if pd.isna(people) else int(round(float(people))),
            "note": note,
        })
    return events, problems


# --- Projected spending from the plan -------------------------------------------

def projection(budget: float | None, spent: float, planned: pd.DataFrame, *, today: date | None = None) -> dict:
    """
    Where the term ends up if the plan happens: what the bank has already
    reported, plus the estimated cost of planned events still to come. What is
    left of the budget after that is room for extras -- a spontaneous tab, a
    mini event (2026-11-09).

    A planned event dated before today is not added: if it happened, its cost is
    already in `spent`; if it didn't, it won't cost anything now. Undated events
    count as still to come.
    """
    today = today or date.today()
    if planned is None or planned.empty:
        ahead_rows = passed_rows = pd.DataFrame(columns=["estimated_cost"])
    else:
        days = pd.to_datetime(planned["event_date"], errors="coerce").dt.date
        upcoming = days.isna() | days.map(lambda d: d is not None and not pd.isna(d) and d >= today)
        ahead_rows, passed_rows = planned[upcoming], planned[~upcoming]
    ahead = float(pd.to_numeric(ahead_rows["estimated_cost"], errors="coerce").fillna(0).sum())
    projected = float(spent) + ahead
    room = None if not budget else float(budget) - projected
    if room is None:
        status = "no budget"
    elif room < 0:
        status = "over"
    elif room < float(budget) * 0.10:
        status = "tight"
    else:
        status = "room"
    return {
        "budget": budget, "spent": float(spent), "ahead": ahead, "ahead_count": len(ahead_rows),
        "passed_count": len(passed_rows), "projected": projected, "room": room, "status": status,
    }
