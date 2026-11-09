"""
Which pages a signed-in person can navigate to.

A VP (an Officer, see `Identity.committee_scoped`) gets five pages: their own
committee's dashboard, their own committee's transactions, their own committee's
plan, the club Events calendar (money on it limited to their committee), and the Assistant (see the note on `VP_PAGES`). Everything else -- including Home and other pages
that are open to any role, like the org-wide Dashboard and Transactions, all of
which show every committee -- is left out of their navigation, and Streamlit only serves pages
that are in the navigation, so a page that is not listed cannot be reached by
typing its URL either.

Kept out of `app.py` so it can be tested without running the whole app, and so
edits to the full `PAGES` list there do not conflict with edits here.
"""

from __future__ import annotations

from .auth import Identity, Role

DASHBOARD_PATH = "ais_fmd/views/Dashboard.py"
# Dashboard was merged into Home on 2026-11-08; where it is absent, the
# President's My Committee follows Home instead.
HOME_PATH = "ais_fmd/views/Home.py"
MY_COMMITTEE_PATH = "ais_fmd/views/Officer.py"

# (path, title, icon, minimum role) -- the same shape as `PAGES` in app.py.
VP_PAGES: list[tuple[str, str, str, Role]] = [
    # No Home: it shows every committee's budget pace and the bank balance, and the
    # treasurer's rule is that a VP sees only their own committee (2026-10-16). My
    # Committee is first, so it is where a VP lands after signing in.
    ("ais_fmd/views/Officer.py", "My Committee", ":material/badge:", Role.OFFICER),
    ("ais_fmd/views/MyTransactions.py", "My Transactions", ":material/table_rows:", Role.OFFICER),
    ("ais_fmd/views/MyPlan.py", "My Plan", ":material/event_note:", Role.OFFICER),
    # The club calendar; money on it is limited to their own committee (Events.py).
    ("ais_fmd/views/Events.py", "Events", ":material/calendar_month:", Role.MEMBER),
    # Under My Transactions, as asked. Unlike the two pages above, the Assistant is the
    # shared page, so the scoping is not done here: `assistant.visible_to_asker` gives a
    # VP's tools only their own committee's rows, and `run_tool` refuses questions that
    # name another committee.
    ("ais_fmd/views/Assistant.py", "Assistant", ":material/smart_toy:", Role.MEMBER),
]


Page = tuple[str, str, str, Role]


def _filtered(identity: Identity, entries: list[Page]) -> list[Page]:
    visible = [page for page in entries if identity.can(page[3])]
    return _my_committee_after_dashboard(visible) if identity.read_only else visible


def visible_pages(identity: Identity, pages: list[Page] | dict[str, list[Page]]):
    """
    The pages `identity` may see: the VP set for a VP, else `pages` filtered by role.

    `pages` is either a plain list or the `{section: [page, ...]}` mapping that
    `PAGES` in app.py uses; the answer has the same shape. A section left with no
    pages is dropped.
    """
    if isinstance(pages, dict):
        source = {"": VP_PAGES} if identity.committee_scoped else pages
        visible = {section: _filtered(identity, entries) for section, entries in source.items()}
        return {section: entries for section, entries in visible.items() if entries}
    return _filtered(identity, VP_PAGES if identity.committee_scoped else pages)


def _my_committee_after_dashboard(
    pages: list[tuple[str, str, str, Role]],
) -> list[tuple[str, str, str, Role]]:
    """
    The President's own budget is the first thing they look at after the org-wide
    Dashboard, so My Committee sits directly under it. Only the order changes.
    """
    mine = [page for page in pages if page[0] == MY_COMMITTEE_PATH]
    paths = [page[0] for page in pages]
    overview = DASHBOARD_PATH if DASHBOARD_PATH in paths else HOME_PATH
    if not mine or overview not in paths:
        return pages
    rest = [page for page in pages if page[0] != MY_COMMITTEE_PATH]
    at = next(i for i, page in enumerate(rest) if page[0] == overview) + 1
    return rest[:at] + mine + rest[at:]
