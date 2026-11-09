"""
The President: sees everything the Treasurer sees, changes nothing.

Three layers are tested separately, because each one is meant to hold even if the
others fail: who the role is (pure), that the repository refuses every write for a
read-only identity (so a page that forgot to hide a button still cannot change
data), and what the real pages show and disable.
"""

from __future__ import annotations

import inspect

import pytest
from streamlit.testing.v1 import AppTest

from ais_fmd import auth, nav
from ais_fmd.data import repositories as repo
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.ui.flags import VERDICT_KEEP

# Fixtures and helpers shared with the other VP tests.
from tests.test_flags import NOTE, VP, _flagged, charge_in, snapshot  # noqa: F401
from tests.test_views import ROOT, TIMEOUT, VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import FULL_PAGES, run_as  # noqa: F401

PRESIDENT = auth.Role.PRESIDENT


def _identity(role: auth.Role, committee_id: int | None = None) -> auth.Identity:
    return auth.Identity(email="someone@sandbox.local", role=role, committee_id=committee_id)


# --- The role itself (pure) ----------------------------------------------------


def test_the_president_reads_everything_the_treasurer_reads_but_cannot_write():
    president = _identity(PRESIDENT)
    assert president.can(auth.Role.TREASURER)
    assert president.read_only
    assert not president.can_write


@pytest.mark.parametrize(
    "role, writes",
    [
        (auth.Role.MEMBER, False),
        (auth.Role.OFFICER, False),
        (auth.Role.TREASURER, True),
        (PRESIDENT, False),
        (auth.Role.ADMIN, True),
    ],
)
def test_only_the_treasurer_and_admin_can_write(role, writes):
    assert _identity(role).can_write is writes


def test_only_the_president_is_read_only():
    assert {role for role in auth.Role if _identity(role).read_only} == {PRESIDENT}


def test_the_president_is_not_given_the_short_vp_navigation():
    assert not _identity(PRESIDENT).committee_scoped
    assert nav.visible_pages(_identity(PRESIDENT), FULL_PAGES) == FULL_PAGES


def test_the_president_sits_between_the_treasurer_and_admin():
    assert auth.Role.TREASURER < PRESIDENT < auth.Role.ADMIN
    assert PRESIDENT in auth.ROLE_DESCRIPTIONS and PRESIDENT.label == "President"


def test_a_president_profile_signs_in_as_president():
    profile = {"role": "President", "committee_id": None}
    identity = auth._identity_for_google_user(
        {"email": "p@sandbox.local", "email_verified": True}, fetch_profile=lambda email: profile
    )
    assert identity == auth.Identity("p@sandbox.local", PRESIDENT, None)


def test_the_president_can_be_saved_in_the_access_list(seeded_db, use_db):
    use_db(seeded_db)
    result = SqliteBackend().upsert_profile("p@sandbox.local", "president", None, "Pres", "t")
    assert result.ok
    assert SqliteBackend().upsert_profile("p@sandbox.local", "emperor", None, "", "t").error


# --- The repository refuses every write ----------------------------------------

WRITE_SCRIPT = """
import streamlit as st
from ais_fmd import auth
from ais_fmd.data import repositories as repo

calls = {
    "insert_transactions": lambda: repo.insert_transactions([], "f.csv", "a"),
    "update_transactions": lambda: repo.update_transactions([], "a"),
    "upsert_budgets": lambda: repo.upsert_budgets("T", {5: 1.0}, "a"),
    "insert_term": lambda: repo.insert_term("T", "Fall 2099", "2099-08-01", "2099-12-31", "a"),
    "upsert_merchants": lambda: repo.upsert_merchants([], "a"),
    "record_statement_balance": lambda: repo.record_statement_balance({}, "a"),
    "create_reimbursement": lambda: repo.create_reimbursement({}, "a"),
    "decide_reimbursement": lambda: repo.decide_reimbursement(1, "approved", "a"),
    "link_reimbursement": lambda: repo.link_reimbursement(1, 1, "a"),
    "store_receipt": lambda: repo.store_receipt({}, "a")[1],
    "set_term_lock": lambda: repo.set_term_lock("T", True, "a"),
    "set_term_dues_rates": lambda: repo.set_term_dues_rates("T", "10", True, "a"),
    "record_labels": lambda: repo.record_labels([], "a"),
    "create_flag": lambda: repo.create_flag(1, (7,), "a note here", "a"),
    "resolve_flag": lambda: repo.resolve_flag(1, "dismissed", "a"),
    "move_flagged_charge": lambda: repo.move_flagged_charge(1, 9, "a"),
    "replace_members": lambda: repo.replace_members("T", [], "f.csv", "a"),
    "add_member_alias": lambda: repo.add_member_alias("T", "a", "b", "a"),
    "upsert_profile": lambda: repo.upsert_profile("x@sandbox.local", "admin", None, "X", "a"),
    "remove_profile": lambda: repo.remove_profile("treasurer@sandbox.local"),
}
st.session_state["errors"] = {name: call().error for name, call in calls.items()}
"""


def _run_writes(identity: auth.Identity) -> dict:
    app = AppTest.from_string(WRITE_SCRIPT, default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = identity
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app.session_state["errors"]


def test_every_write_is_refused_for_the_president(seeded_db, use_db):
    use_db(seeded_db)
    errors = _run_writes(_identity(PRESIDENT))
    assert set(errors.values()) == {repo.READ_ONLY_MESSAGE}, errors


def test_a_refused_write_changes_nothing(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    before = (
        len(backend.fetch_transactions()),
        len(backend.fetch_profiles()),
        backend.fetch_budgets().to_dict("records"),
    )
    _run_writes(_identity(PRESIDENT))
    after = (
        len(backend.fetch_transactions()),
        len(backend.fetch_profiles()),
        backend.fetch_budgets().to_dict("records"),
    )
    assert after == before


def test_the_same_writes_are_not_blocked_for_a_treasurer(seeded_db, use_db):
    """Proves the refusals above come from the role, not from the calls being malformed."""
    use_db(seeded_db)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd.data import repositories as repo\n"
        "st.session_state['saved'] = repo.upsert_profile('new.vp@sandbox.local', 'officer', 7, 'New VP', 't').error\n",
        default_timeout=TIMEOUT,
    )
    app.session_state[auth.SESSION_KEY] = _identity(auth.Role.TREASURER)
    app.run()
    assert app.session_state["saved"] is None
    assert SqliteBackend().fetch_profile("new.vp@sandbox.local") is not None


def test_a_charge_cannot_be_moved_by_the_president_even_with_an_open_flag(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd import auth\n"
        "from ais_fmd.data import repositories as repo\n"
        f"st.session_state['error'] = repo.move_flagged_charge({flag_id}, 9, 'p@sandbox.local').error\n",
        default_timeout=TIMEOUT,
    )
    app.session_state[auth.SESSION_KEY] = _identity(PRESIDENT)
    app.run()
    assert app.session_state["error"] == repo.READ_ONLY_MESSAGE
    assert snapshot(backend, charge)["budget_category"] == 7
    assert backend.fetch_flags().iloc[0]["status"] == "open"


def test_no_write_in_the_repository_is_left_unguarded():
    """A new write added later must be wrapped, or this fails and says which one."""
    unguarded = []
    for name, function in inspect.getmembers(repo, inspect.isfunction):
        if name.startswith("_") or name.startswith("load") or function.__module__ != repo.__name__:
            continue
        if "backend()." in inspect.getsource(function) and not hasattr(function, "__wrapped__"):
            unguarded.append(name)
    # `invalidate` and the evaluators touch no data; `backend` is the accessor itself.
    assert unguarded == [], f"writes that skip the read-only check: {unguarded}"


# --- The pages -----------------------------------------------------------------

ALL_PAGES = [
    "Home.py", "Dashboard.py", "Transactions.py", "Officer.py", "Reimbursements.py",
    "Reconciliation.py", "ReviewQueue.py", "Roster.py", "Planner.py", "Treasury.py",
    "DataQuality.py", "AuditLog.py",
]


@pytest.mark.parametrize("page", ALL_PAGES)
def test_every_page_the_treasurer_has_opens_for_the_president(page, seeded_db, use_db):
    use_db(seeded_db)
    if not (VIEWS / page).exists():
        pytest.skip(f"{page} is not in this checkout")
    app = run_as(VIEWS / page, PRESIDENT)
    assert_clean(app, f"{page} as the President")
    turned_away = [w.value for w in app.warning if "access" in w.value.lower()]
    assert not turned_away, f"{page} turned the President away: {turned_away}"


@pytest.mark.parametrize("page", ["Transactions.py", "Treasury.py", "Reimbursements.py", "Roster.py"])
def test_pages_tell_the_president_they_are_read_only(page, seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / page, PRESIDENT)
    assert any("read-only" in block.value for block in app.info), f"{page} shows no notice"


def test_the_treasurer_is_never_shown_the_read_only_notice(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Treasury.py", auth.Role.TREASURER)
    assert not any("read-only" in block.value for block in app.info)


def _buttons(app) -> dict[str, bool]:
    """Every button on the page, by label, mapped to whether it is disabled."""
    return {button.label: button.disabled for button in app.button}


def test_the_presidents_treasury_page_has_no_working_save_buttons(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Treasury.py", PRESIDENT)
    saves = {label: off for label, off in _buttons(app).items() if label in {"Save budgets", "Add term", "Save rates"}}
    assert saves, "the Treasury page shows none of the save buttons this test looks for"
    assert all(saves.values()), saves

    treasurer = run_as(VIEWS / "Treasury.py", auth.Role.TREASURER)
    assert not any(
        off for label, off in _buttons(treasurer).items() if label in {"Save budgets", "Add term"}
    )


def test_the_president_cannot_edit_the_ledger_table(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Transactions.py", PRESIDENT)
    assert_clean(app, "Transactions as the President")
    assert not [b for b in app.button if "save" in b.label.lower()], "a Save button is offered"


def test_the_president_sees_the_flags_but_cannot_decide_them(seeded_db, use_db):
    use_db(seeded_db)
    backend = SqliteBackend()
    charge, flag_id = _flagged(backend, 7)
    app = run_as(VIEWS / "ReviewQueue.py", PRESIDENT)
    assert_clean(app, "Review Queue as the President")
    assert any(NOTE in block.value for block in app.markdown), "the open flag is not shown"

    app.radio(key=f"flag_verdict_{flag_id}").set_value(VERDICT_KEEP).run()
    assert app.button(key=f"flag_apply_{flag_id}").disabled
    assert backend.fetch_flags().iloc[0]["status"] == "open"


def test_the_president_can_open_any_committees_dashboard(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "Officer.py", PRESIDENT)
    assert_clean(app, "My Committee as the President")
    assert "Committee" in [box.label for box in app.selectbox]


def test_the_president_cannot_open_the_access_list(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "OfficerAccess.py", PRESIDENT)
    assert_clean(app, "Officer Access as the President")
    assert any("Read-only access" in block.value for block in app.warning)
    assert not app.button


def test_the_treasurer_still_opens_the_access_list(seeded_db, use_db):
    use_db(seeded_db)
    app = run_as(VIEWS / "OfficerAccess.py", auth.Role.TREASURER)
    assert_clean(app, "Officer Access as the Treasurer")
    assert not app.warning


def test_the_sandbox_role_picker_offers_the_president(seeded_db, use_db):
    use_db(seeded_db)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT).run()
    assert "President" in app.selectbox(key="ais_signin_picker").options


def test_only_the_committee_heads_can_sign_in(seeded_db, use_db):
    """Committees are their heads: the President, the Treasurer and one VP per committee."""
    use_db(seeded_db)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT).run()
    assert app.selectbox(key="ais_signin_picker").options == [
        "President", "Treasurer", "Professional Development VP", "Consulting VP",
        "Marketing VP", "Membership VP", "Operations VP",
    ]


def test_picking_a_vp_signs_in_as_that_committee(seeded_db, use_db):
    use_db(seeded_db)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=TIMEOUT).run()
    app.selectbox(key="ais_signin_picker").set_value("Membership VP").run()
    identity = app.session_state[auth.SESSION_KEY]
    assert (identity.role, identity.committee_id) == (auth.Role.OFFICER, 5)


def test_the_president_has_their_own_budget_line_and_opens_on_it(seeded_db, use_db):
    use_db(seeded_db)
    from ais_fmd.config import vp_committees
    from ais_fmd.config.categories import BUDGETED_COMMITTEE_IDS, committee_name

    line = vp_committees.PRESIDENT_LINE_ID
    assert committee_name(line) == "President" and line in BUDGETED_COMMITTEE_IDS

    app = run_as(VIEWS / "Officer.py", PRESIDENT, line)
    assert_clean(app, "My Committee as the President")
    picker = next(box for box in app.selectbox if box.label == "Committee")
    assert picker.value == "President"
    assert "Consulting" in picker.options  # still free to look at the others


# --- Navigation order ----------------------------------------------------------

ORDERED_PAGES = [
    ("ais_fmd/views/Home.py", "Home", "i", auth.Role.MEMBER),
    ("ais_fmd/views/Dashboard.py", "Dashboard", "i", auth.Role.MEMBER),
    ("ais_fmd/views/Transactions.py", "Transactions", "i", auth.Role.MEMBER),
    ("ais_fmd/views/ReviewQueue.py", "Review Queue", "i", auth.Role.TREASURER),
    ("ais_fmd/views/Officer.py", "My Committee", "i", auth.Role.OFFICER),
    ("ais_fmd/views/Treasury.py", "Treasury", "i", auth.Role.TREASURER),
]


def _titles(identity):
    return [page[1] for page in nav.visible_pages(identity, ORDERED_PAGES)]


def test_the_president_sees_my_committee_right_under_the_dashboard():
    assert _titles(_identity(PRESIDENT)) == [
        "Home", "Dashboard", "My Committee", "Transactions", "Review Queue", "Treasury",
    ]


def test_moving_my_committee_loses_and_adds_no_pages():
    president = nav.visible_pages(_identity(PRESIDENT), ORDERED_PAGES)
    assert sorted(president) == sorted(ORDERED_PAGES)


def test_the_treasurer_keeps_the_original_order():
    assert nav.visible_pages(_identity(auth.Role.TREASURER), ORDERED_PAGES) == ORDERED_PAGES
