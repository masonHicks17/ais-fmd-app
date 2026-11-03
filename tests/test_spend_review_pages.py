"""
The spend review panel on the Review Queue, driven through the real page.

Rendering the page must never call the model -- it reruns on every click -- and
the button must run one metered review, after which a second click sends
nothing. The model is a stub; the sandbox cannot reach the real one.
"""

from __future__ import annotations

from datetime import date, timedelta

from ais_fmd import auth
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain.dedupe import assign_natural_keys
from tests.test_model_guardrails import model  # noqa: F401 - fixture
from tests.test_views import VIEWS, assert_clean, seeded_db, use_db  # noqa: F401
from tests.test_vp_portal import run_as


def _add_fresh_charges(backend: SqliteBackend) -> int:
    """Two charges dated this week, inside a fresh open term, so they are eligible."""
    today = date.today()
    with backend._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT OR REPLACE INTO terms (TermID, Semester, start_date, end_date, locked) "
            "VALUES ('NOW', 'Current Test Term', ?, ?, 0)",
            ((today - timedelta(days=30)).isoformat(), (today + timedelta(days=30)).isoformat()),
        )
        connection.commit()
    day = (today - timedelta(days=2)).isoformat()
    records = [
        {"transaction_date": day, "amount": -16.10, "details": "LOWE'S #2365 CARD 0594",
         "account": "Wells Fargo", "budget_category": 5, "purpose": None},
        {"transaction_date": day, "amount": -21.00, "details": "SALTY DOG SALOON CARD 3526",
         "account": "Wells Fargo", "budget_category": 4, "purpose": None},
    ]
    receipt = backend.insert_transactions(assign_natural_keys(records), "Checking-test.csv", "t")
    assert receipt.ok, receipt.error
    return receipt.inserted


def _panel_text(app) -> str:
    return " ".join(
        str(getattr(element, "value", "") or getattr(element, "label", ""))
        for element in [*app.markdown, *app.caption, *app.button]
    )


def test_the_page_renders_and_sends_nothing_in_sandbox(seeded_db, use_db):
    use_db(seeded_db)
    _add_fresh_charges(SqliteBackend())
    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "Review Queue with the spend review panel")
    assert "switched off" in _panel_text(app)
    assert SqliteBackend().fetch_llm_runs().empty


def test_one_click_reviews_once_and_a_second_sends_nothing(seeded_db, use_db, model):
    use_db(seeded_db)
    backend = SqliteBackend()
    _add_fresh_charges(backend)

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    assert_clean(app, "Review Queue with the model on")
    assert model.calls == [], "rendering the page never calls the model"
    button = app.button(key="spend_review_run")
    assert "at most $" in button.label

    button.click().run()
    assert_clean(app, "after running the spend review")
    sent = len(model.calls)
    assert sent >= 1
    runs = SqliteBackend().fetch_llm_runs()
    assert list(runs["status"]) == ["done"]

    # Everything eligible is now reviewed: the button is gone, and rerunning
    # the page sends nothing more.
    app.run()
    assert not [b for b in app.button if b.key == "spend_review_run"]
    assert len(model.calls) == sent


def test_the_treasurer_closes_a_flag(seeded_db, use_db, model):
    use_db(seeded_db)
    _add_fresh_charges(SqliteBackend())
    model.state["verdict"] = "question"

    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.TREASURER)
    app.button(key="spend_review_run").click().run()
    open_reviews = SqliteBackend().fetch_spend_reviews()
    open_reviews = open_reviews[open_reviews["status"] == "open"]
    assert len(open_reviews) >= 2
    review_id = int(open_reviews.iloc[0]["review_id"])

    app.button(key=f"spend_dismiss_{review_id}").click().run()
    assert_clean(app, "after closing a spend review flag")
    stored = SqliteBackend().fetch_spend_reviews().set_index("review_id")
    assert stored.loc[review_id, "status"] == "dismissed"


def test_read_only_access_cannot_start_a_run(seeded_db, use_db, model):
    """The President opens every page but changes nothing -- including spending."""
    use_db(seeded_db)
    _add_fresh_charges(SqliteBackend())
    app = run_as(VIEWS / "ReviewQueue.py", auth.Role.PRESIDENT)
    assert_clean(app, "Review Queue as the President")
    assert app.button(key="spend_review_run").disabled
    assert model.calls == []
