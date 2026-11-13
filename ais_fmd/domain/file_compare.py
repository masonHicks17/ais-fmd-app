"""
Compare an uploaded file with what the app already holds (Treasury -> Reconcile).

Since 2026-11-13 the books, the roster and the calendar arrive by an automatic
sync from Google Drive and Supabase. Uploading a file is no longer how data gets
in; it is how the treasurer checks the sync. Each comparison here sorts the
file against the app into three groups:

  new       in the file, not in the app
  changed   in both, but a field differs (old -> new)
  missing   in the app, not in the file -- only inside the file's own scope
            (the statement's dates and account, the roster's term, the
            schedule's date span), so a one-month statement does not report
            the rest of the year as missing

Nothing is applied here. The Reconcile tab shows each group with a tick box and
applies only what the treasurer ticks.

Pure: no Streamlit, no I/O.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

import pandas as pd

from ..config.categories import normalize_account
from .calendar_export import resolve_times
from .dedupe import identity_tuple, split_new_and_duplicate
from .roster import _CLAIM_COLUMN_PREFIXES, Member

NEW, CHANGED, MISSING = "new", "changed", "missing"


@dataclass(frozen=True)
class FieldChange:
    field: str
    label: str
    old: object
    new: object

    def describe(self) -> str:
        return f"{self.label}: {_shown(self.old)} → {_shown(self.new)}"


@dataclass
class Difference:
    """One row of the comparison. `key` identifies it for the tick boxes."""

    kind: str
    key: str
    file_row: dict | None = None
    app_row: dict | None = None
    changes: list[FieldChange] = field(default_factory=list)
    note: str = ""

    @property
    def change_text(self) -> str:
        return "; ".join(change.describe() for change in self.changes)


@dataclass
class Comparison:
    new: list[Difference] = field(default_factory=list)
    changed: list[Difference] = field(default_factory=list)
    missing: list[Difference] = field(default_factory=list)
    # In the app, inside the scope, but never offered for removal (events typed
    # in the app). Listed so the treasurer knows why they are not "missing".
    kept: list[Difference] = field(default_factory=list)
    matching: int = 0
    scope: str = ""

    @property
    def differences(self) -> int:
        return len(self.new) + len(self.changed) + len(self.missing)


def _blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    if value is pd.NaT:
        return True
    return isinstance(value, str) and not value.strip()


def _shown(value: object) -> str:
    if _blank(value):
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _text(value: object) -> str:
    return "" if _blank(value) else str(value).strip()


def _day(value: object) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


# --- Bank statement ----------------------------------------------------------------

def compare_statement(file_records: Sequence[dict], stored_records: Sequence[dict], account: str) -> Comparison:
    """
    `file_records`: the parsed statement rows (categorized or not).
    `stored_records`: every stored transaction.

    New rows are the ones the dedupe would import. Missing rows are stored
    transactions on the same account, dated inside the statement's first and
    last day, that the statement does not account for -- counted as a multiset,
    the same way the dedupe counts, so two identical coffees stored against one
    in the file leaves one missing.

    There is no "changed" group: a transaction's identity is its content
    (date, amount, details, account), so a different row is a new one.
    """
    comparison = Comparison()
    if not file_records:
        return comparison
    dedupe = split_new_and_duplicate(file_records, stored_records)
    comparison.matching = len(dedupe.duplicates)
    for record in dedupe.new:
        comparison.new.append(Difference(NEW, str(record["natural_key"]), file_row=record))

    days = [day for day in (_day(r.get("transaction_date")) for r in file_records) if day is not None]
    if not days:
        return comparison
    first, last = min(days), max(days)
    wanted = normalize_account(account)
    comparison.scope = f"{wanted} · {first:%b %d, %Y} – {last:%b %d, %Y}"

    in_file = Counter(identity_tuple(record) for record in file_records)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in stored_records:
        if normalize_account(row.get("account")) != wanted:
            continue
        day = _day(row.get("transaction_date"))
        if day is None or not (first <= day <= last):
            continue
        groups[identity_tuple(row)].append(row)
    for identity, rows in groups.items():
        rows = sorted(rows, key=lambda r: int(r.get("transactionid") or 0))
        for row in rows[in_file[identity]:]:
            comparison.missing.append(
                Difference(MISSING, f"txn:{row.get('transactionid')}", app_row=row)
            )
    comparison.missing.sort(key=lambda d: (str(_day(d.app_row.get("transaction_date"))), d.key))
    return comparison


# --- Membership roster -----------------------------------------------------------

# What a roster upload may bring in (treasurer, 2026-11-13): the name, preferred
# name, UFID, UF email and the "I certify I paid" answer. Never phone numbers,
# gender or survey answers -- `parse_roster` does not read them, and nothing
# below copies anything else from the file.
ROSTER_FIELDS = (
    ("full_name", "Name"),
    ("preferred_name", "Preferred name"),
    ("email", "Email"),
    ("ufid", "UFID"),
    ("claims_paid", "Says they paid"),
)


def _file_text(value: object) -> str:
    """A cell as `parse_roster` gives it: "nan" and "none" are blanks."""
    text = _text(value)
    return "" if text.lower() in {"nan", "none"} else text


def member_row(member: Member) -> dict:
    """The stored shape of a member read from a file."""
    ufid = _file_text(member.ufid)
    return {
        "full_name": member.full_name,
        "match_key": member.match_key,
        "alt_keys": list(member.alt_keys),
        "preferred_name": _file_text(member.preferred_name),
        "claims_paid": bool(member.claims_paid),
        "email": _file_text(member.email),
        "ufid": ufid[:-2] if ufid.endswith(".0") and ufid[:-2].isdigit() else ufid,
    }


def _roster_value(row: dict, name: str) -> object:
    value = row.get(name)
    if name == "claims_paid":
        return bool(0 if _blank(value) else int(bool(value)))
    if name == "email":
        return _text(value).lower()
    if name == "ufid":
        text = _text(value)
        return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text
    return _text(value)


def roster_has_claim_column(columns: Iterable[object]) -> bool:
    """Whether the file asks "I certify that I paid" at all (see `parse_roster`)."""
    return any(str(c).strip().lower().startswith(_CLAIM_COLUMN_PREFIXES) for c in columns)


def compare_roster(
    file_members: Iterable[Member],
    stored_rows: Sequence[dict],
    term_label: str = "",
    *,
    compare_claims: bool = True,
) -> Comparison:
    """
    Match each file member to a stored one by UFID when both have it, otherwise
    by the normalised name. A name is only reported as changed when the match
    was made on UFID: matched by name, the names are the same by definition.

    `compare_claims=False` when the file has no "I certify I paid" column, so a
    sheet without the question does not read as everyone taking it back.
    """
    comparison = Comparison(scope=term_label)
    stored = [dict(row) for row in stored_rows]
    by_ufid = {_roster_value(r, "ufid"): i for i, r in enumerate(stored) if _roster_value(r, "ufid")}
    by_key = {_text(r.get("match_key")): i for i, r in enumerate(stored) if _text(r.get("match_key"))}
    used: set[int] = set()

    for member in file_members:
        incoming = member_row(member)
        position = None
        ufid = _roster_value(incoming, "ufid")
        if ufid and ufid in by_ufid and by_ufid[ufid] not in used:
            position = by_ufid[ufid]
        elif member.match_key in by_key and by_key[member.match_key] not in used:
            position = by_key[member.match_key]
        if position is None:
            comparison.new.append(Difference(NEW, f"new:{member.match_key}", file_row=incoming))
            continue
        used.add(position)
        current = stored[position]
        changes = []
        for name, label in ROSTER_FIELDS:
            if name == "full_name" and _text(current.get("match_key")) == member.match_key:
                continue
            if name == "claims_paid" and not compare_claims:
                continue
            old, new = _roster_value(current, name), _roster_value(incoming, name)
            # A blank in the file is "not given", not "remove it".
            if name != "claims_paid" and not new:
                continue
            if old != new:
                shown_old = old if name == "claims_paid" else _text(current.get(name))
                shown_new = new if name == "claims_paid" else _text(incoming.get(name))
                changes.append(FieldChange(name, label, shown_old, shown_new))
        if changes:
            comparison.changed.append(
                Difference(CHANGED, f"member:{current.get('match_key')}", file_row=incoming,
                           app_row=current, changes=changes)
            )
        else:
            comparison.matching += 1

    for position, row in enumerate(stored):
        if position not in used:
            comparison.missing.append(Difference(MISSING, f"member:{row.get('match_key')}", app_row=row))
    return comparison


def merged_roster(
    stored_rows: Sequence[dict], comparison: Comparison, apply: set[str]
) -> list[dict]:
    """
    The roster to save: what is stored, plus the ticked new members, with the
    ticked changes made and the ticked removals left out. Fields the file does
    not carry (committee, notes) and aliases confirmed on the Roster page are
    kept.
    """
    changed = {d.key: d for d in comparison.changed if d.key in apply}
    removed = {d.key for d in comparison.missing if d.key in apply}
    merged: list[dict] = []
    for row in stored_rows:
        key = f"member:{row.get('match_key')}"
        if key in removed:
            continue
        out = {
            "full_name": _text(row.get("full_name")),
            "match_key": _text(row.get("match_key")),
            "alt_keys": [k.strip() for k in _text(row.get("alt_keys")).split(",") if k.strip()],
            "preferred_name": _text(row.get("preferred_name")),
            "claims_paid": bool(_roster_value(row, "claims_paid")),
            "email": _text(row.get("email")),
            "ufid": _text(row.get("ufid")),
            "committee_id": None if _blank(row.get("committee_id")) else int(row.get("committee_id")),
            "notes": _text(row.get("notes")),
        }
        difference = changed.get(key)
        if difference is not None:
            incoming = difference.file_row
            for change in difference.changes:
                out[change.field] = change.new
                if change.field == "full_name":
                    out["match_key"] = incoming["match_key"]
            out["alt_keys"] = list(dict.fromkeys([*out["alt_keys"], *incoming.get("alt_keys", [])]))
            out["alt_keys"] = [k for k in out["alt_keys"] if k != out["match_key"]]
        merged.append(out)
    merged.extend(dict(d.file_row) for d in comparison.new if d.key in apply)

    # One row per name: the table is keyed on (term, match_key).
    seen: set[str] = set()
    unique = []
    for row in merged:
        if row["match_key"] in seen:
            continue
        seen.add(row["match_key"])
        unique.append(row)
    return unique


# --- Event schedule ----------------------------------------------------------------

EVENT_FIELDS = (
    ("title", "Title"),
    ("start_time", "Start"),
    ("end_time", "End"),
    ("location", "Location"),
    ("owner", "Owner"),
    ("audience", "Audience"),
    ("status", "Status"),
    ("description", "Description"),
    ("committee_id", "Committee"),
)


def _event_value(row: dict, name: str) -> object:
    value = row.get(name)
    if name == "committee_id":
        return None if _blank(value) else int(value)
    return _text(value)


def _event_differs(name: str, current: dict, row: dict) -> bool:
    """
    Whether the file really changes this field. A blank in the file never
    clears a value (a Google Calendar export has no owner, audience or status
    column). Times are compared as a calendar shows them: "7:00" on a sheet
    and "19:00" from Google are the same evening, and the hour an export adds
    when the app has no end time is not a change.
    """
    old, new = _event_value(current, name), _event_value(row, name)
    if new in (None, ""):
        return False
    if name in ("start_time", "end_time"):
        have = resolve_times(current.get("start_time"), current.get("end_time"))
        got = resolve_times(row.get("start_time"), row.get("end_time"))
        if have[0] is not None or got[0] is not None:
            return have[0] != got[0] if name == "start_time" else have[1] != got[1]
    if isinstance(old, str) and isinstance(new, str):
        return " ".join(old.split()) != " ".join(new.split())  # .ics descriptions come back on one line
    return old != new


def filled_from_app(file_row: dict, app_row: dict | None) -> dict:
    """The file's row with its blanks filled from what the app holds, so applying a change clears nothing."""
    if not app_row:
        return dict(file_row)
    merged = dict(file_row)
    for name, _ in EVENT_FIELDS:
        if _event_value(file_row, name) in (None, "") and _event_value(app_row, name) not in (None, ""):
            merged[name] = app_row.get(name)
    return merged


def is_manual(row: dict) -> bool:
    """An event typed in the app rather than synced from a calendar or sheet."""
    return _text(row.get("source")).lower() == "manual"


def compare_events(file_rows: Sequence[dict], stored_rows: Sequence[dict]) -> Comparison:
    """
    `file_rows`: `Event.as_row()` for each event in the file. Matched on
    `external_key` (the day and the normalised title). Missing is limited to
    the file's first and last event day; events typed in the app are listed
    under `kept`, never offered for removal.
    """
    comparison = Comparison()
    incoming: dict[str, dict] = {}
    for row in file_rows:
        incoming.setdefault(str(row["external_key"]), row)
    if not incoming:
        return comparison
    stored = {str(row.get("external_key")): row for row in stored_rows}

    for key, row in incoming.items():
        current = stored.get(key)
        if current is None:
            comparison.new.append(Difference(NEW, key, file_row=row))
            continue
        changes = [
            FieldChange(name, label, _event_value(current, name), _event_value(row, name))
            for name, label in EVENT_FIELDS
            if _event_differs(name, current, row)
        ]
        if changes:
            note = "Edited in the app" if bool(current.get("edited_in_app") or 0) else ""
            comparison.changed.append(
                Difference(CHANGED, key, file_row=row, app_row=current, changes=changes, note=note)
            )
        else:
            comparison.matching += 1

    days = [day for day in (_day(r.get("event_date")) for r in incoming.values()) if day is not None]
    first, last = min(days), max(days)
    comparison.scope = f"{first:%b %d, %Y} – {last:%b %d, %Y}"
    for key, row in stored.items():
        if key in incoming:
            continue
        day = _day(row.get("event_date"))
        if day is None or not (first <= day <= last):
            continue
        if is_manual(row):
            comparison.kept.append(Difference(MISSING, key, app_row=row, note="Added in the app"))
            continue
        note = "Edited in the app" if bool(row.get("edited_in_app") or 0) else ""
        comparison.missing.append(Difference(MISSING, key, app_row=row, note=note))
    comparison.missing.sort(key=lambda d: str(d.app_row.get("event_date")))
    comparison.kept.sort(key=lambda d: str(d.app_row.get("event_date")))
    return comparison


def as_text(value: object) -> str:
    """A stored value as display text: blank for None/NaN, never the string "nan"."""
    return _text(value)
