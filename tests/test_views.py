"""
Headless execution tests for every view (P1 from the handoff).

WHY THIS FILE EXISTS. `test_ui_conventions.py` parses the views as text and
never runs them, so 3,148 lines of view code had zero execution coverage.
Every runtime bug in the rebuild was found by hand in a browser: a
`NaN.strip()` crash, currency rendering as LaTeX on three separate pages, a
Plotly `title=None` printing "undefined", pages defaulting to an empty term.
All of those are exceptions or bad output on first render -- exactly what an
`AppTest` run catches.

Three scenarios per view, because the bugs found by hand clustered in two of
them that a happy-path test would miss:

  * populated database -- the normal case
  * empty database     -- no terms, no transactions; where "defaults to an
                          empty term" and "iloc[0] on an empty frame" live
  * reduced role       -- the gated pages must decline, not crash

`AppTest` runs the script exactly as `st.Page` does, so absolute-import and
`st.stop()` behaviour is exercised for real rather than approximated.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ais_fmd import auth

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "ais_fmd" / "views"

# Generous: a cold run builds every chart in the page.
TIMEOUT = 90


def view_paths() -> list[Path]:
    return sorted(p for p in VIEWS.glob("*.py") if p.name != "__init__.py")


VIEW_IDS = [p.stem for p in view_paths()]


# --- Database fixtures -------------------------------------------------------

@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory) -> Path:
    """One seeded database, built once and copied per test."""
    import os

    target = tmp_path_factory.mktemp("seed") / "seeded.db"
    previous = os.environ.get("AIS_FMD_SANDBOX_DB")
    os.environ["AIS_FMD_SANDBOX_DB"] = str(target)
    try:
        from ais_fmd.data.seed import seed
        from ais_fmd.data.sqlite_backend import SqliteBackend

        seed(SqliteBackend(), reset=True)
    finally:
        if previous is None:
            os.environ.pop("AIS_FMD_SANDBOX_DB", None)
        else:
            os.environ["AIS_FMD_SANDBOX_DB"] = previous
    return target


@pytest.fixture(scope="session")
def empty_db(tmp_path_factory) -> Path:
    """Schema, no rows. A brand-new deployment before the first upload."""
    import os

    target = tmp_path_factory.mktemp("empty") / "empty.db"
    previous = os.environ.get("AIS_FMD_SANDBOX_DB")
    os.environ["AIS_FMD_SANDBOX_DB"] = str(target)
    try:
        from ais_fmd.data.sqlite_backend import SqliteBackend

        SqliteBackend()  # constructing it creates the schema
    finally:
        if previous is None:
            os.environ.pop("AIS_FMD_SANDBOX_DB", None)
        else:
            os.environ["AIS_FMD_SANDBOX_DB"] = previous
    return target


@pytest.fixture
def use_db(monkeypatch, tmp_path, isolated_sandbox):
    """
    Point the app at a private copy of a prepared database.

    Depends on `isolated_sandbox` explicitly so this override lands *after*
    conftest's autouse fixture has set its own path, not before.

    Streamlit's caches are process-global, so they are cleared on both sides of
    the test -- otherwise the cached backend from a previous test keeps serving
    the previous test's database.
    """

    def _use(source: Path) -> Path:
        destination = tmp_path / "under_test.db"
        shutil.copy(source, destination)
        monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(destination))
        st.cache_data.clear()
        st.cache_resource.clear()
        return destination

    yield _use
    st.cache_data.clear()
    st.cache_resource.clear()


def run_view(path: Path, *, role: auth.Role = auth.Role.TREASURER, **session) -> AppTest:
    app = AppTest.from_file(str(path), default_timeout=TIMEOUT)
    app.session_state[auth.SESSION_KEY] = auth.Identity(
        email="tester@sandbox.local", role=role
    )
    for key, value in session.items():
        app.session_state[key] = value
    return app.run()


def assert_clean(app: AppTest, context: str) -> None:
    assert not app.exception, (
        f"{context} raised on render:\n"
        + "\n".join(str(exception.value) for exception in app.exception)
    )


# --- Scenario 1: populated database ------------------------------------------

@pytest.mark.parametrize("path", view_paths(), ids=VIEW_IDS)
def test_view_renders_with_data(path: Path, seeded_db, use_db):
    use_db(seeded_db)
    assert_clean(run_view(path), f"{path.name} with a populated database")


# --- Scenario 2: empty database ----------------------------------------------

@pytest.mark.parametrize("path", view_paths(), ids=VIEW_IDS)
def test_view_renders_on_an_empty_database(path: Path, empty_db, use_db):
    """
    No terms and no transactions.

    This is the scenario that produced "pages default to an empty term" and the
    `iloc[0]`-on-an-empty-frame class of crash. A page is allowed to stop early
    with an empty state; it is not allowed to raise.
    """
    use_db(empty_db)
    assert_clean(run_view(path), f"{path.name} against an empty database")


# --- Scenario 3: insufficient role -------------------------------------------

@pytest.mark.parametrize("path", view_paths(), ids=VIEW_IDS)
def test_view_declines_a_member_without_crashing(path: Path, seeded_db, use_db):
    """
    Every page runs as MEMBER, the lowest role.

    Gated pages must halt at `auth.require` with a warning. Ungated ones must
    render. Neither may raise -- a crash here would leak a stack trace to a user
    who is not entitled to the page at all.
    """
    use_db(seeded_db)
    app = run_view(path, role=auth.Role.MEMBER)
    assert_clean(app, f"{path.name} as MEMBER")


def test_gated_pages_actually_stop_for_a_member(seeded_db, use_db):
    """The gate must deny, not merely avoid crashing (the F2 guarantee)."""
    use_db(seeded_db)
    app = run_view(VIEWS / "Treasury.py", role=auth.Role.MEMBER)
    warnings = " ".join(block.value for block in app.warning)
    assert "access required" in warnings.lower(), (
        "Treasury rendered for a MEMBER without an access warning"
    )


def test_treasurer_reaches_a_gated_page(seeded_db, use_db):
    """The counterpart: the gate must also admit."""
    use_db(seeded_db)
    app = run_view(VIEWS / "Treasury.py", role=auth.Role.TREASURER)
    warnings = " ".join(block.value for block in app.warning)
    assert "access required" not in warnings.lower()


# --- Runtime check for the LaTeX trap ----------------------------------------

# Two unescaped '$' in one markdown string is what turns the text between them
# into an equation. One is harmless.
_UNESCAPED_DOLLAR = re.compile(r"(?<!\\)\$")


def _latex_risk(text: str) -> bool:
    return len(_UNESCAPED_DOLLAR.findall(text or "")) >= 2


@pytest.mark.parametrize("path", view_paths(), ids=VIEW_IDS)
def test_rendered_text_does_not_trip_the_latex_trap(path: Path, seeded_db, use_db):
    """
    The runtime counterpart to the AST check in test_ui_conventions.

    The source scan only catches money that is formatted *in the same call*.
    It cannot see a figure that arrives through a variable, or one produced by
    a helper several frames down. This checks what actually reached the page.
    """
    use_db(seeded_db)
    app = run_view(path)

    offenders = [
        block.value
        for group in (app.markdown, app.caption, app.warning, app.info, app.success, app.error)
        for block in group
        if _latex_risk(block.value)
    ]
    assert not offenders, (
        f"{path.name} rendered text with two unescaped '$', which Streamlit "
        f"reads as LaTeX:\n" + "\n".join(f"  {text[:160]}" for text in offenders[:5])
    )


# --- Interaction: the three views with real interaction logic ----------------

def test_dashboard_semester_filter_changes_the_figures(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(VIEWS / "Dashboard.py")
    picker = app.selectbox(key="dash_semester")
    assert len(picker.options) > 1, "need at least two terms to exercise the filter"

    first = [metric.value for metric in app.metric]
    other = next(o for o in picker.options if o != picker.value)
    app = picker.select(other).run()

    assert_clean(app, "Dashboard after changing semester")
    assert [metric.value for metric in app.metric] != first, (
        "changing the semester did not change any headline figure"
    )


def test_dashboard_committee_filter_runs_clean(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(VIEWS / "Dashboard.py")
    picker = app.selectbox(key="dash_committee")
    for option in picker.options[:4]:
        app = app.selectbox(key="dash_committee").select(option).run()
        assert_clean(app, f"Dashboard filtered to {option}")


def test_transactions_page_survives_every_filter_position(seeded_db, use_db):
    """
    Transactions is where the `NaN.strip()` crash lived -- in a filter path,
    not on first render, which is why an import-only test would have missed it.
    """
    use_db(seeded_db)
    app = run_view(VIEWS / "Transactions.py")
    assert_clean(app, "Transactions on first render")

    for picker in list(app.selectbox) + list(app.sidebar.selectbox):
        if len(picker.options) < 2:
            continue
        key = picker.key
        for option in picker.options[:3]:
            app = run_view(VIEWS / "Transactions.py")
            target = next((s for s in list(app.selectbox) + list(app.sidebar.selectbox)
                           if s.key == key), None)
            if target is None:
                break
            app = target.select(option).run()
            assert_clean(app, f"Transactions with {key}={option!r}")


def test_transactions_search_box_accepts_free_text(seeded_db, use_db):
    """Free text is the input most likely to hit a null-handling path."""
    use_db(seeded_db)
    app = run_view(VIEWS / "Transactions.py")
    boxes = list(app.text_input) + list(app.sidebar.text_input)
    if not boxes:
        pytest.skip("Transactions has no text input to exercise")
    for probe in ("zelle", "  ", "'; drop table transactions; --", "ZZZ_no_match"):
        app = run_view(VIEWS / "Transactions.py")
        box = (list(app.text_input) + list(app.sidebar.text_input))[0]
        app = box.set_value(probe).run()
        assert_clean(app, f"Transactions searching for {probe!r}")


def test_review_queue_surfaces_held_back_proposals(seeded_db, use_db):
    """
    The flagged rows are the highest-value ones in the queue -- they carry a
    proposed committee and the reason it is uncertain. They were unreachable
    before the filter existed: sorting by confidence buries them under every
    high-confidence row, and the queue is far longer than any batch size.
    """
    use_db(seeded_db)
    app = run_view(VIEWS / "ReviewQueue.py")
    assert_clean(app, "ReviewQueue on first render")

    options = app.radio(key="queue_filter").options
    flagged = next((o for o in options if o.startswith("Flagged")), None)
    assert flagged, f"no flagged filter among {options}"

    app = app.radio(key="queue_filter").set_value(flagged).run()
    assert_clean(app, "ReviewQueue filtered to flagged proposals")


@pytest.mark.parametrize(
    "filter_label",
    ["Everything", "Flagged: scored but uncertain", "Resolvable now", "No signal at all"],
)
def test_review_queue_every_filter_renders(seeded_db, use_db, filter_label):
    """Including the ones that may legitimately match nothing."""
    use_db(seeded_db)
    app = run_view(VIEWS / "ReviewQueue.py")
    if filter_label not in app.radio(key="queue_filter").options:
        pytest.skip(f"{filter_label} not offered on this dataset")
    app = app.radio(key="queue_filter").set_value(filter_label).run()
    assert_clean(app, f"ReviewQueue showing {filter_label!r}")


@pytest.mark.parametrize(
    "period", ["Past year", "Past 6 months", "Past 90 days", "Everything"]
)
def test_review_queue_every_period_renders(seeded_db, use_db, period):
    use_db(seeded_db)
    app = run_view(VIEWS / "ReviewQueue.py")
    app = app.selectbox(key="queue_period").set_value(period).run()
    assert_clean(app, f"ReviewQueue over {period!r}")


def test_review_queue_coverage_is_not_distorted_by_the_period_filter(seeded_db, use_db):
    """
    REGRESSION. Coverage is a property of the whole ledger. Computing it after
    the period filter counted period-excluded rows as categorized and reported
    87.9% when the truth was 63.5% -- no exception, just a wrong number, which
    is precisely what a render-only test cannot catch.
    """
    use_db(seeded_db)
    app = run_view(VIEWS / "ReviewQueue.py")
    app = app.selectbox(key="queue_period").set_value("Everything").run()
    assert_clean(app, "ReviewQueue over everything")
    baseline = {m.label: m.value for m in app.metric}

    app = run_view(VIEWS / "ReviewQueue.py")
    app = app.selectbox(key="queue_period").set_value("Past 90 days").run()
    assert_clean(app, "ReviewQueue over 90 days")
    narrowed = {m.label: m.value for m in app.metric}

    assert narrowed["Coverage"] == baseline["Coverage"], (
        "coverage must describe the ledger, not the selected period"
    )
    assert narrowed["Already categorized"] == baseline["Already categorized"]


def test_review_queue_period_anchors_on_the_newest_transaction(seeded_db, use_db):
    """
    Anchoring on today rather than on the data would show an empty queue after
    any gap in importing, which reads as "all clear" when it means "stale".
    """
    use_db(seeded_db)
    app = run_view(VIEWS / "ReviewQueue.py")
    app = app.selectbox(key="queue_period").set_value("Past 90 days").run()
    assert_clean(app, "ReviewQueue over the last 90 days")
    # Seeded data is not guaranteed to be recent, so the assertion is that the
    # view still has content to work with, not that a fixed count survives.
    assert not any("Nothing unresolved" in str(e.value) for e in app.error)


def test_review_queue_never_offers_a_row_that_already_has_a_committee(seeded_db, use_db):
    """
    REGRESSION (2026-10-16). Stored categorizations are the record. The queue
    used to list any row missing a committee OR a purpose -- so history from the
    original app, which mostly has a committee and no purpose, appeared with the
    committee box pre-set to the categorizer's suggestion, and one "Apply
    decisions" would have overwritten what Supabase already had.

    Also pins the second half of the rule: blanks carried over from the original
    app (no source_file) stay out of the queue unless the treasurer opts in.
    """
    import sqlite3

    database = use_db(seeded_db)
    with sqlite3.connect(database) as connection:
        ids = [
            row[0]
            for row in connection.execute(
                "SELECT transactionid FROM transactions ORDER BY transactionid LIMIT 2"
            )
        ]
        stored, history_blank = ids
        # The largest amounts in the ledger, so the queue's default "largest
        # first" order puts them in the first batch if they are listed at all.
        connection.execute(
            "UPDATE transactions SET budget_category = 5, purpose = NULL, source_file = NULL, "
            "amount = -99999 WHERE transactionid = ?",
            (stored,),
        )
        connection.execute(
            "UPDATE transactions SET budget_category = NULL, purpose = NULL, source_file = NULL, "
            "amount = -99998 WHERE transactionid = ?",
            (history_blank,),
        )

    def offered(app: AppTest) -> set[int]:
        return {
            int(box.key.rsplit("_", 1)[1])
            for box in app.selectbox
            if box.key and box.key.startswith("queue_committee_")
        }

    app = run_view(VIEWS / "ReviewQueue.py", queue_period="Everything", queue_batch=50)
    assert_clean(app, "ReviewQueue with carried-over history")
    assert stored not in offered(app), "a row with a stored committee reached the queue"
    assert history_blank not in offered(app), "an original-app blank was offered by default"
    by_default = {m.label: m.value for m in app.metric}["Awaiting review"]

    app = run_view(
        VIEWS / "ReviewQueue.py",
        queue_period="Everything",
        queue_batch=50,
        queue_include_history=True,
    )
    assert_clean(app, "ReviewQueue including original-app blanks")
    assert stored not in offered(app), "opting in to history must still never offer a stored committee"
    with_history = {m.label: m.value for m in app.metric}["Awaiting review"]
    assert int(with_history.replace(",", "")) == int(by_default.replace(",", "")) + 1


def test_treasury_tabs_all_render(seeded_db, use_db):
    """Treasury does budgets, terms, export and locking (upload moved to Transactions)."""
    use_db(seeded_db)
    app = run_view(VIEWS / "Treasury.py")
    assert_clean(app, "Treasury on first render")
    assert app.tabs, "Treasury renders no tabs"


def test_planner_inputs_accept_edge_values(seeded_db, use_db):
    """
    The scenario planner divides by member counts and prices.

    Zero is the input that turns a break-even calculation into a division by
    zero, which is the same shape as finding F12.
    """
    use_db(seeded_db)
    app = run_view(VIEWS / "Planner.py")
    assert_clean(app, "Planner on first render")

    for widget in list(app.number_input) + list(app.sidebar.number_input):
        key = widget.key
        for value in (widget.min or 0, 1):
            app = run_view(VIEWS / "Planner.py")
            target = next(
                (w for w in list(app.number_input) + list(app.sidebar.number_input)
                 if w.key == key),
                None,
            )
            if target is None:
                break
            app = target.set_value(value).run()
            assert_clean(app, f"Planner with {key}={value}")


# --- Pebble consolidation: hubs ----------------------------------------------

HUB_VIEWS = {
    "DuesHub.py": ("dues_hub", ["Dues", "Roster"]),
    "TransactionsHub.py": ("transactions_hub", ["Import", "Transactions", "Review Queue"]),
    "AssistantHub.py": ("assistant_hub", ["Assistant", "Help"]),
    "AlertsHub.py": ("alerts_hub", ["Alerts & Reports", "Data Quality"]),
    "SettingsHub.py": ("settings_hub", ["Officer Access", "Audit Log"]),
}


@pytest.mark.parametrize(
    "hub,choice",
    [(hub, label) for hub, (_, labels) in HUB_VIEWS.items() for label in labels],
)
def test_every_hub_view_renders(hub, choice, seeded_db, use_db):
    """Each merged page renders inside its hub, not just on its own."""
    use_db(seeded_db)
    key, _ = HUB_VIEWS[hub]
    app = run_view(VIEWS / hub, **{key: choice})
    assert_clean(app, f"{hub} showing {choice!r}")


def test_hub_hides_views_a_member_cannot_open(seeded_db, use_db):
    """A member on Dues sees Dues only -- no Roster, and no access warning."""
    use_db(seeded_db)
    app = run_view(VIEWS / "DuesHub.py", role=auth.Role.MEMBER, dues_hub="Roster")
    assert_clean(app, "DuesHub as MEMBER")
    warnings = " ".join(block.value for block in app.warning)
    assert "access required" not in warnings.lower()


def test_settings_hub_denies_a_member(seeded_db, use_db):
    use_db(seeded_db)
    app = run_view(VIEWS / "SettingsHub.py", role=auth.Role.MEMBER)
    warnings = " ".join(block.value for block in app.warning)
    assert "access required" in warnings.lower()
