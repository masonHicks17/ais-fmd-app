"""
Which budget lines each VP's committee owns.

The website lists six committees, each with one VP. The app books money to 18
finer-grained budget lines (`config/categories.py`). A VP should see the money
for everything their committee owns, so this file says which lines roll up into
which committee.

PROVISIONAL. This mapping is a proposal that the treasurer has not confirmed.
Nothing here decides where money is booked -- it only decides which lines are
added together on a VP's page -- but a wrong line here shows a VP the wrong
budget, so confirm it before real VPs sign in. Each entry says how sure it is.

Not listed on purpose:
  * Finance (Treasury, line 2) -- the treasurer's own full view, not a VP page.
  * Operations -- no budget line is obviously theirs yet; awaiting the treasurer.
  * Passport (line 16) -- treated as outside the six committees. A VP never sees
    its transactions; only the treasurer does. Unconfirmed.

No committee currently owns more than one line, but the roll-up (a committee that
adds several lines together) is kept and tested, for the day one does.
A committee that is not listed falls back to showing just its own single line,
exactly as before, so an unmapped VP still sees something correct.

The shared ufais.org officer mailboxes are mapped to a role and committee in
officer_roster.json (2026-10-26); anyone else is added on the Officer Access page.
"""

from __future__ import annotations

from dataclasses import dataclass

from .categories import committee_name

# The President is not one of the six VP committees, but has their own budget line
# ("President" in categories.py). Their profile carries it, so My Committee opens on
# it; they can still pick any other committee, because they read everything.
PRESIDENT_LINE_ID = 4

# "Projected burn" on My Committee says when the money runs out at the pace so far.
# It is a straight-line average over the whole term, which misreads a committee whose
# spending comes in lumps (a big event, an early purchase), so it is switched on
# committee by committee rather than for everyone. Membership only, for now.
PROJECTED_BURN_LINE_IDS: tuple[int, ...] = (5,)


def shows_projected_burn(line_ids: tuple[int, ...]) -> bool:
    """Whether a committee owning these budget lines gets the Projected burn panel."""
    return any(int(line) in PROJECTED_BURN_LINE_IDS for line in line_ids)


@dataclass(frozen=True)
class VpCommittee:
    title: str
    # Budget-line ids this committee owns. The first is its "home" line, the id
    # a VP's profile is expected to carry.
    budget_ids: tuple[int, ...]

    @property
    def budget_names(self) -> list[str]:
        return [committee_name(cid) for cid in self.budget_ids]


VP_COMMITTEES: tuple[VpCommittee, ...] = (
    # Exact name match.
    VpCommittee("Professional Development", (10,)),
    # Exact name match.
    VpCommittee("Consulting", (7,)),
    # Exact name match. Merch spending books here (retired line 13, 2026-11-09).
    VpCommittee("Marketing", (9,)),
    # Exact name match. Meeting Food, Road Trip, ISOM Passport and Formal all
    # book here since the seven-committee change (2026-11-09).
    VpCommittee("Membership", (5,)),
    # Operations (formerly Corporate Relations) is an exact name match and owns only its own line. In the
    # sandbox seed every sponsorship deposit is booked to this line, so its "Money
    # received" shows sponsorship income; whether real bookings do the same is the
    # treasurer's call, not something this file decides.
    VpCommittee("Operations", (6,)),
)


def for_committee_id(committee_id: int | None) -> VpCommittee | None:
    """The VP committee that owns this budget line, or None if it is unmapped."""
    if committee_id is None:
        return None
    for committee in VP_COMMITTEES:
        if int(committee_id) in committee.budget_ids:
            return committee
    return None


def budget_ids_for(committee_id: int | None) -> tuple[int, ...]:
    """Every budget line a VP with this profile committee may see."""
    if committee_id is None:
        return ()
    owner = for_committee_id(committee_id)
    return owner.budget_ids if owner else (int(committee_id),)


def title_for(committee_id: int | None) -> str:
    owner = for_committee_id(committee_id)
    return owner.title if owner else committee_name(committee_id)
