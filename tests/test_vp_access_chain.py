"""
From a stored profile to the right pages -- the whole chain except Google itself.

When Google sign-in is switched on, a VP's access is decided by this sequence:
their `profiles` row (role + committee) -> `auth._identity_for_google_user` ->
the navigation in `app.py` -> the pages' own scoping. The OIDC round-trip that
supplies the email cannot be run headlessly, but everything after it can, so
these tests run the real `app.py` as the identity a profile produces and check
which pages open and whose rows appear.

All emails are invented `@sandbox.local` addresses.
"""

from __future__ import annotations

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from ais_fmd import auth
from ais_fmd.config import vp_committees
from ais_fmd.data.backend import normalize_email
from ais_fmd.data.sqlite_backend import SqliteBackend

# Fixtures and helpers shared with the other VP tests.
from tests.test_views import ROOT, TIMEOUT, VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import _committee_rows, membership_db  # noqa: F401

VIEW = "ais_fmd/views/{}.py"
VP_PAGES = ["Officer", "MyTransactions", "MyPlan", "Assistant"]
ORG_WIDE_PAGES = [
    "Home", "Dashboard", "Transactions", "Dues", "Reports", "Reconciliation",
    "Runbook", "Treasury", "ReviewQueue", "Roster", "Planner", "DataQuality", "AuditLog",
    # The Pebble hubs, which are what the full navigation actually lists.
    "DuesHub", "TransactionsHub", "AlertsHub", "SettingsHub",
]


def sign_in(typed_email: str, *, role: str, committee_id: int | None, stored_as: str | None = None):
    """Store a profile, then resolve it the way a Google sign-in would."""
    backend = SqliteBackend()
    result = backend.upsert_profile(stored_as or typed_email, role, committee_id, "Test person", "tests")
    assert result.ok, result.error
    return auth._identity_for_google_user(
        {"email": typed_email, "email_verified": True}, fetch_profile=backend.fetch_profile
    )


def open_app(identity: auth.Identity) -> AppTest:
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = identity
    return app.run()


def opens(app: AppTest, page: str) -> bool:
    """Whether the navigation lets this identity reach the page at all."""
    try:
        app.switch_page(VIEW.format(page))
    except ValueError:
        return False
    app.run()
    return True


# --- Emails ----------------------------------------------------------------------


def test_emails_are_compared_ignoring_case_and_padding():
    assert normalize_email("  Vp.Marketing@Sandbox.Local ") == "vp.marketing@sandbox.local"
    assert normalize_email(None) == ""


def test_an_underscore_is_a_character_not_a_wildcard():
    """The Supabase lookup used ILIKE, where `_` matches any single character."""
    assert normalize_email("a_b@sandbox.local") != normalize_email("a.b@sandbox.local")


def test_two_addresses_differing_only_at_an_underscore_stay_two_people(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.upsert_profile("a_b@sandbox.local", "officer", 5, "One", "tests")
    backend.upsert_profile("a.b@sandbox.local", "officer", 7, "Two", "tests")
    assert backend.fetch_profile("a_b@sandbox.local")["committee_id"] == 5
    assert backend.fetch_profile("a.b@sandbox.local")["committee_id"] == 7


# --- Who is let in ---------------------------------------------------------------


def test_a_profile_resolves_to_its_role_and_committee_whatever_the_capitalisation(seeded_db, use_db):
    use_db(seeded_db)
    identity = sign_in("VP.Marketing@Sandbox.Local", role="officer", committee_id=9,
                       stored_as="vp.marketing@sandbox.local")
    assert identity == auth.Identity("VP.Marketing@Sandbox.Local", auth.Role.OFFICER, 9)


def test_someone_with_no_profile_is_refused_not_downgraded(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    assert auth._identity_for_google_user(
        {"email": "stranger@sandbox.local", "email_verified": True},
        fetch_profile=backend.fetch_profile,
    ) is None


def test_an_unverified_google_email_is_refused_even_with_a_profile(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.upsert_profile("vp@sandbox.local", "officer", 9, "VP", "tests")
    assert auth._identity_for_google_user(
        {"email": "vp@sandbox.local", "email_verified": False},
        fetch_profile=backend.fetch_profile,
    ) is None


def test_removing_a_profile_removes_the_way_in(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    backend.upsert_profile("vp@sandbox.local", "officer", 9, "VP", "tests")
    backend.remove_profile("VP@sandbox.local")
    assert auth._identity_for_google_user(
        {"email": "vp@sandbox.local"}, fetch_profile=backend.fetch_profile
    ) is None


# --- Which pages open ------------------------------------------------------------


def test_a_vp_reaches_exactly_their_own_pages_and_no_other_org_wide_page(membership_db):
    app = open_app(sign_in("vp@sandbox.local", role="officer", committee_id=9))
    assert_clean(app, "app.py as the Marketing VP")
    for page in VP_PAGES:
        assert opens(app, page), f"{page} should open for a VP"
    for page in ORG_WIDE_PAGES:
        assert not opens(app, page), f"{page} opened for a VP but shows every committee"


def test_a_treasurer_profile_keeps_the_full_app(membership_db):
    app = open_app(sign_in("treasurer@sandbox.local", role="treasurer", committee_id=None))
    for page in ["Home", "TransactionsHub", "Treasury", "Officer"]:
        assert opens(app, page), f"{page} should open for the treasurer"


def test_a_member_profile_gets_member_pages_but_no_committee_or_treasury_pages(membership_db):
    """Documents the cost of a wrong role: a VP saved as 'member' sees org-wide pages."""
    app = open_app(sign_in("member@sandbox.local", role="member", committee_id=None))
    assert opens(app, "Home")
    for page in ["Officer", "MyTransactions", "Treasury", "ReviewQueue"]:
        assert not opens(app, page), f"{page} opened for a plain member"


# --- Whose rows appear -----------------------------------------------------------

# Every VP committee's home line, plus the two committees no VP roll-up owns:
# President and Treasury.
PROFILE_COMMITTEES = [
    vp.budget_ids[0] for vp in vp_committees.VP_COMMITTEES
] + [4, 2]


@pytest.mark.parametrize("committee_id", PROFILE_COMMITTEES)
def test_a_vps_transactions_page_holds_only_their_committees_rows(committee_id, membership_db):
    app = open_app(sign_in("vp@sandbox.local", role="officer", committee_id=committee_id))
    assert opens(app, "MyTransactions")
    app.session_state["mytxn_semester"] = "All"
    app.run()
    assert_clean(app, f"My Transactions through app.py as committee {committee_id}")

    expected = _committee_rows(committee_id)
    if not expected:
        assert not app.dataframe
        return
    shown = app.dataframe[0].value
    got = sorted(
        (str(pd.Timestamp(date).date()), round(float(amount), 2), str(details))
        for date, amount, details in zip(shown["Date"], shown["Amount"], shown["Details"])
    )
    assert got == expected
