"""
Add fake demo transactions for the VP committees to the SANDBOX database.

The normal seed books almost nothing to Membership or Passport and spreads most
committees' spending over a single purpose, so a VP's page comes up nearly empty.
This tops each VP committee up to a chosen share of its budget in every term the
sandbox has (five, back to Fall 2024), with several purposes, a few refunds or
deposits coming in, and a mix of states (on track, approaching, over), so every
chart, table and past-term comparison on My Committee and My Transactions has
something to draw. It also adds a fake sign-in profile for each VP and the
treasurer, and a few sample flags for the Review Queue.

Everything here is invented: made-up venues and vendors, no real names, card
numbers or amounts from any statement. It refuses to run outside the sandbox.

    .venv\\Scripts\\python scripts/seed_vp_demo.py

Safe to run twice: a second run only adds what is missing (a newly listed
committee, a new profile) and never repeats what is already there. "Reset & reseed
data" in the sidebar wipes it along with everything else; run this again after.
Restart Streamlit afterwards, or wait a few minutes for its cache to expire.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from ais_fmd import settings
from ais_fmd.config.categories import ACCOUNT_WELLS_FARGO, committee_name
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.dedupe import assign_natural_keys

FILE_NAME = "vp_demo_seed.csv"
ACTOR = "vp-demo-seed@sandbox.local"

# The terms to fill and how far into the latest one the demo data reaches. The
# regular seed already runs to late October of Fall 2026.
TERM_IDS = ("FA24", "SP25", "FA25", "SP26", "FA26")
LATEST_DATA_DAY = date(2026, 10, 22)

# Share of each term's budget to bring a line up to: {line: {term: share}}.
# Chosen to give every status and some variety from term to term, so the
# past-term comparison has a real spread to report. Existing spending counts
# toward the share, and a line already past its target is left alone, never
# reduced.
TARGET_SHARE = {
    5: {"FA24": 0.55, "SP25": 0.70, "FA25": 0.62, "SP26": 0.80, "FA26": 0.88},   # Membership: approaching
    10: {"FA24": 0.50, "SP25": 0.65, "FA25": 0.55, "SP26": 0.70, "FA26": 0.45},  # Professional Development: on track
    7: {"FA24": 0.40, "SP25": 0.55, "FA25": 0.50, "SP26": 0.90, "FA26": 0.60},   # Consulting
    9: {"FA24": 0.60, "SP25": 0.45, "FA25": 0.50, "SP26": 0.70, "FA26": 0.50},   # Marketing (already over in Fall 2026)
    6: {"FA24": 0.50, "SP25": 0.65, "FA25": 0.58, "SP26": 0.72, "FA26": 0.66},   # Operations
}

# (purpose, weight, vendor names). Invented vendors only.
SPENDING = {
    # Membership, which runs Passport since the club went to seven committees
    # (2026-11-09; Passport was its own line 16 before).
    5: [
        ("Social Events", 0.35, ["TOPGOLF GAINESVILLE", "BOWLING LANES GNV", "POTTERY STUDIO GNV", "INTL STUDENT MIXER VENUE"]),
        ("Food & Drink", 0.30, ["ARCADE BAR GNV", "TACO SHACK GNV", "PIZZA PALACE GNV", "CULTURAL NIGHT CATERING"]),
        ("ISOM Passport", 0.20, ["PASSPORT STAMPS PRINT SHOP", "PASSPORT BOOKLETS PRINTING"]),
        ("Misc.", 0.15, ["LAKE PARK ENTRY FEES", "PARTY SUPPLIES OUTLET"]),
    ],
    10: [
        ("Professional Events", 0.50, ["INDUSTRY PANEL VENUE", "CAREER NIGHT HALL RENTAL"]),
        ("Professional Development", 0.30, ["WORKSHOP SPEAKER HONORARIUM", "ONLINE COURSE LICENSES"]),
        ("Food & Drink", 0.20, ["CATERED WORKSHOP LUNCH", "COFFEE CART GNV"]),
    ],
    7: [
        ("Technology", 0.40, ["CLOUD HOSTING CREDITS", "PROJECT TOOLS SUBSCRIPTION"]),
        ("Food & Drink", 0.35, ["CLIENT KICKOFF LUNCH", "TEAM WORKING DINNER"]),
        ("Misc.", 0.25, ["CLIENT GIFT BASKETS", "PRESENTATION PRINTING"]),
    ],
    9: [
        ("Marketing", 0.50, ["BANNER PRINTING GNV", "FLYER PRINTING GNV"]),
        ("Merch", 0.30, ["EMBROIDERY SHOP GNV", "STICKER PRINTER"]),
        ("Technology", 0.20, ["WEBSITE HOSTING", "DESIGN TOOL SUBSCRIPTION"]),
    ],
    6: [
        ("Professional Events", 0.45, ["SPONSOR MIXER VENUE", "COMPANY SITE VISIT TRANSPORT"]),
        ("Food & Drink", 0.35, ["SPONSOR APPRECIATION LUNCH", "PARTNER COFFEE MEETING"]),
        ("Misc.", 0.20, ["THANK YOU CARDS PRINTING", "SPONSOR PACKET PRINTING"]),
    ],
}

# Money coming back in, so "Money received" has something to show:
# (line, purpose, details, amount, term).
INCOMING = [
    (10, "Sponsorship / Donation", "CORPORATE WORKSHOP SPONSOR DEPOSIT", 300.00, "SP26"),
    (10, "Sponsorship / Donation", "CORPORATE CAREER NIGHT SPONSOR DEPOSIT", 450.00, "FA26"),
    (5, "Refunded", "VENUE DEPOSIT REFUND", 75.00, "SP26"),
    (9, "Refunded", "PRINT ORDER REFUND", 42.50, "FA26"),
    (5, "Sponsorship / Donation", "CULTURAL NIGHT SPONSOR DEPOSIT", 200.00, "FA26"),
    (10, "Sponsorship / Donation", "CORPORATE WORKSHOP SPONSOR DEPOSIT", 250.00, "SP25"),
    (5, "Refunded", "VENUE DEPOSIT REFUND", 60.00, "FA24"),
    (9, "Refunded", "PRINT ORDER REFUND", 35.00, "FA25"),
]

# Fake sign-in profiles, one per VP committee plus the treasurer. Invented
# `@sandbox.local` addresses only -- real emails never go in this repository.
# (email, role, committee line, display name)
PROFILES = [
    ("vp.professional-development@sandbox.local", "officer", 10, "Demo VP (Professional Development)"),
    ("vp.consulting@sandbox.local", "officer", 7, "Demo VP (Consulting)"),
    ("vp.marketing@sandbox.local", "officer", 9, "Demo VP (Marketing)"),
    ("vp.membership@sandbox.local", "officer", 5, "Demo VP (Membership)"),
    ("vp.operations@sandbox.local", "officer", 6, "Demo VP (Operations)"),
    ("treasurer@sandbox.local", "treasurer", None, "Demo Treasurer"),
    ("president@sandbox.local", "president", 4, "Demo President"),
]

# Sample "This isn't ours" flags so the Review Queue has something in it:
# (flagging profile, the committee line the charge sits in, note, how it is closed).
DEMO_FLAGS = [
    ("vp.consulting@sandbox.local", 7,
     "This looks like a Marketing website charge, not a Consulting project.", None),
    ("vp.professional-development@sandbox.local", 10,
     "We did not host this event; it was a Membership social.", None),
    ("vp.marketing@sandbox.local", 9,
     "I think part of this was split with Membership.",
     ("dismissed", "Checked the receipt: it is Marketing's. Leaving it booked there.")),
]


def _describe(vendor: str, day: date, rng: random.Random) -> str:
    """A bank-statement-looking line with an invented reference number."""
    return (
        f"PURCHASE AUTHORIZED ON {day:%m/%d} {vendor} GAINESVILLE FL "
        f"S{rng.randint(100000, 999999)}"
    )


def _split(total: float, parts: int, rng: random.Random) -> list[float]:
    """`total` divided into `parts` uneven amounts that add up to it exactly."""
    weights = [rng.uniform(0.5, 1.5) for _ in range(parts)]
    scale = total / sum(weights)
    amounts = [round(weight * scale, 2) for weight in weights]
    amounts[-1] = round(total - sum(amounts[:-1]), 2)
    return amounts


def build_records(
    terms: dict[str, tuple[date, date]],
    budgets: dict,
    spent: dict,
    lines: tuple[int, ...] | None = None,
) -> list[dict]:
    """
    Spending (and, on a full run, a few incoming deposits) for the demo committees.

    `lines` limits it to some committees, for adding a newly listed one to a database
    that is already seeded. The incoming deposits are skipped then: they are one-off
    rows for the original committees and would otherwise be added a second time.
    """
    rng = random.Random(20261001)
    records: list[dict] = []

    for line, shares in TARGET_SHARE.items():
        if lines is not None and line not in lines:
            continue
        for term_id in TERM_IDS:
            if term_id not in terms:
                continue
            budget = budgets.get((term_id, line))
            if not budget:
                continue
            needed = round(shares[term_id] * budget - spent.get((term_id, line), 0.0), 2)
            if needed < 20:
                continue

            start, end = terms[term_id]
            end = min(end, LATEST_DATA_DAY)
            window = max((end - start).days, 1)
            categories = SPENDING[line]

            for purpose, weight, vendors in categories:
                share = round(needed * weight, 2)
                if share < 5:
                    continue
                count = max(2, int(share // 55))
                for amount in _split(share, count, rng):
                    day = start + timedelta(days=rng.randint(0, window))
                    records.append(
                        {
                            "transaction_date": day.isoformat(),
                            "amount": -abs(amount),
                            "details": _describe(rng.choice(vendors), day, rng),
                            "budget_category": line,
                            "purpose": purpose,
                            "account": ACCOUNT_WELLS_FARGO,
                        }
                    )

    for line, purpose, details, amount, term_id in INCOMING:
        if lines is not None or term_id not in terms:
            continue
        start, end = terms[term_id]
        end = min(end, LATEST_DATA_DAY)
        day = start + timedelta(days=rng.randint(0, max((end - start).days, 1)))
        records.append(
            {
                "transaction_date": day.isoformat(),
                "amount": amount,
                "details": f"DEPOSIT {day:%m/%d} {details}",
                "budget_category": line,
                "purpose": purpose,
                "account": ACCOUNT_WELLS_FARGO,
            }
        )

    records.sort(key=lambda record: record["transaction_date"])
    return records


def main() -> int:
    if not settings.is_sandbox():
        print("Refusing to run: this adds fake data and only runs in the sandbox.")
        return 1

    backend = SqliteBackend()

    uploaded = backend.fetch_uploaded_files()
    already_loaded = not uploaded.empty and FILE_NAME in set(uploaded["file_name"])

    # A second run never repeats what is there. It adds a committee that was listed
    # after the first run (it has no demo rows yet) and makes sure every profile
    # exists, so a newly added VP can sign in without wiping the sandbox.
    only_lines: tuple[int, ...] | None = None
    if already_loaded:
        existing = backend.fetch_transactions()
        seeded = set()
        if not existing.empty and "source_file" in existing.columns:
            demo_rows = existing[existing["source_file"] == FILE_NAME]
            seeded = {int(line) for line in demo_rows["budget_category"].dropna()}
        only_lines = tuple(line for line in TARGET_SHARE if line not in seeded)
        if not only_lines:
            return _ensure_profiles(backend, "already loaded; nothing new to add")

    terms_frame = backend.fetch_terms()
    budgets_frame = backend.fetch_budgets()
    if terms_frame.empty or budgets_frame.empty:
        print("No terms or budgets yet. Start the app once so it seeds the sandbox, then run this.")
        return 1

    terms = {
        row["TermID"]: (date.fromisoformat(str(row["start_date"])[:10]), date.fromisoformat(str(row["end_date"])[:10]))
        for _, row in terms_frame.iterrows()
    }
    budgets = {
        (row["termid"], int(row["committeeid"])): float(row["budget_amount"] or 0)
        for _, row in budgets_frame.iterrows()
    }

    # What each line has already spent per term, so targets top up, never double up.
    spent: dict[tuple[str, int], float] = {}
    transactions = backend.fetch_transactions()
    for _, row in transactions.iterrows():
        if row["amount"] >= 0 or pd.isna(row["budget_category"]):
            continue
        day = row["transaction_date"].date()
        for term_id, (start, end) in terms.items():
            if start <= day <= end:
                key = (term_id, int(row["budget_category"]))
                spent[key] = spent.get(key, 0.0) + abs(float(row["amount"]))
                break

    records = assign_natural_keys(build_records(terms, budgets, spent, only_lines))
    receipt = backend.insert_transactions(records, FILE_NAME, ACTOR)
    if not receipt.ok:
        print(f"Could not load the demo data: {receipt.error}")
        return 1

    print(f"Added {receipt.inserted} fake transactions ({receipt.duplicates_skipped} duplicates skipped).")
    for line in only_lines or TARGET_SHARE:
        added = sum(1 for record in records if record["budget_category"] == line)
        print(f"  {committee_name(line):<26} +{added} rows")

    status = _ensure_profiles(backend, "")
    if status:
        return status
    if not already_loaded:
        added_flags = _add_demo_flags(backend)
        print(f"Added {added_flags} sample flags for the Review Queue.")
    print("Restart Streamlit to see them straight away.")
    return 0


def _ensure_profiles(backend: SqliteBackend, note: str) -> int:
    """Create or refresh every fake sign-in profile. Safe to repeat."""
    for email, role, line, name in PROFILES:
        result = backend.upsert_profile(email, role, line, name, ACTOR)
        if not result.ok:
            print(f"Could not add profile {email}: {result.error}")
            return 1
    suffix = f" ({note})" if note else ""
    print(f"{len(PROFILES)} fake sign-in profiles in place (VPs, the treasurer and the president){suffix}.")
    return 0


def _add_demo_flags(backend: SqliteBackend) -> int:
    """File the sample flags on each committee's newest charge, closing one of them."""
    from ais_fmd.config import vp_committees

    charges = backend.fetch_transactions()
    made = 0
    for email, line, note, closing in DEMO_FLAGS:
        owned = vp_committees.budget_ids_for(line)
        rows = charges[charges["budget_category"].eq(line).fillna(False) & (charges["amount"] < 0)]
        if rows.empty:
            continue
        charge_id = int(rows.sort_values("transaction_date").iloc[-1]["transactionid"])
        if not backend.create_flag(charge_id, owned, note, email).ok:
            continue
        made += 1
        if closing is not None:
            status, reply = closing
            open_flags = backend.fetch_flags(owned)
            flag_id = int(open_flags[open_flags["transaction_id"] == charge_id].iloc[0]["flag_id"])
            backend.resolve_flag(flag_id, status, "treasurer@sandbox.local", reply)
    return made


if __name__ == "__main__":
    raise SystemExit(main())
