"""
The guardrails around every model call, tested against a real SQLite ledger.

What these pin down, in the order the treasurer asked for them (2026-10-20):
  * the spend review never sends a charge it does not need to -- history, a
    closed term, one already reviewed, one a VP has flagged;
  * nothing can loop: a second run over the same charges sends nothing, two
    runs cannot overlap, and the day's run count is capped;
  * nothing can overspend: the monthly cap is checked before a run, against a
    ledger that counts a crashed run at its full reservation;
  * a run always closes its ledger row, and a failure keeps what was paid for.

The model is a stub. The sandbox cannot reach the real one.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from ais_fmd import settings
from ais_fmd.config.categories import COMMITTEES
from ais_fmd.data.sqlite_backend import SqliteBackend
from ais_fmd.domain import model_runs
from ais_fmd.domain.categorize import metering
from ais_fmd.domain.dedupe import assign_natural_keys

ACTOR = "treasurer@sandbox.local"
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def backend(tmp_path) -> SqliteBackend:
    db = SqliteBackend(tmp_path / "guardrails.db")
    with db._connect() as connection:  # noqa: SLF001
        connection.executemany(
            "INSERT OR REPLACE INTO committees (CommitteeID, Committee_Name, Committee_Type) "
            "VALUES (?, ?, ?)",
            [(c.id, c.name, c.kind) for c in COMMITTEES],
        )
        connection.executemany(
            "INSERT INTO terms (TermID, Semester, start_date, end_date, locked) VALUES (?, ?, ?, ?, ?)",
            [
                ("FA25", "Fall 2025", "2025-08-15", "2025-12-18", 0),
                ("SP26", "Spring 2026", "2026-01-05", "2026-05-15", 1),
                ("FA26", "Fall 2026", "2026-08-15", "2026-12-18", 0),
            ],
        )
        connection.commit()
    return db


def charge(date, amount, details, committee):
    return {"transaction_date": date, "amount": amount, "details": details,
            "account": "Wells Fargo", "budget_category": committee, "purpose": None}


def load(backend, records, file_name="Checking.csv"):
    receipt = backend.insert_transactions(assign_natural_keys(records), file_name, ACTOR)
    assert receipt.ok, receipt.error
    return backend.fetch_transactions()


@pytest.fixture
def model(monkeypatch):
    """A fake Anthropic client that answers 'fits' (or a set verdict) for every row it is sent."""
    calls: list[dict] = []
    state = {"verdict": "fits", "fail": False}

    def create(**kwargs):
        calls.append(kwargs)
        if state["fail"]:
            raise RuntimeError("overloaded")
        lines = [line for line in kwargs["messages"][0]["content"].splitlines() if line[:1].isdigit()]
        results = []
        for line in lines:
            index = int(line.split(".", 1)[0])
            amount = float(line.split("Amount: ", 1)[1].split(" |", 1)[0])
            results.append({"index": index, "amount": amount, "verdict": state["verdict"],
                            "suggested_committee_id": None, "confidence": 0.8,
                            "reason": "stub", "message_to_vp": "" if state["verdict"] == "fits" else "Hi?"})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps({"results": results}))],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=7000, output_tokens=50 * len(results),
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(settings, "llm_enabled", lambda: True)
    monkeypatch.setattr(settings, "assert_external_call_allowed", lambda what: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub-not-real")
    return SimpleNamespace(calls=calls, state=state)


def review(backend):
    return model_runs.run_spend_review(
        backend, ACTOR, backend.fetch_transactions(), backend.fetch_terms(), now=NOW
    )


LOWES = "PURCHASE AUTHORIZED ON 09/22 LOWE'S #2365 GAINESVILLE FL CARD 0594"


# ---------------------------------------------------------------- what is never sent

def test_only_new_open_term_charges_are_selected(backend):
    transactions = load(backend, [
        charge("2026-09-22", -16.10, LOWES, 5),                        # eligible
        charge("2026-09-23", -21.00, "SALTY DOG SALOON CARD 3526", 4), # eligible
        charge("2026-09-24", -10.00, "SOMETHING", None),               # no committee
        charge("2025-09-10", -30.00, "LAST YEAR", 5),                  # older than the window
        charge("2026-07-01", -12.00, "SUMMER", 5),                     # outside every term
    ])
    selection = model_runs.select_for_review(transactions, backend.fetch_terms(), None, None, today=NOW)
    assert list(selection.eligible["details"]) == [LOWES, "SALTY DOG SALOON CARD 3526"]
    assert selection.skipped["no committee"] == 1
    assert selection.skipped["older than the review window"] == 1
    assert selection.skipped["outside every term"] == 1


def test_a_closed_term_is_never_sent(backend):
    transactions = load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    terms = backend.fetch_terms()
    terms.loc[terms["TermID"] == "FA26", "locked"] = 1
    selection = model_runs.select_for_review(transactions, terms, None, None, today=NOW)
    assert selection.count == 0 and selection.skipped["in a closed term"] == 1


def test_history_from_the_original_app_is_never_sent(backend):
    transactions = load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    transactions["source_file"] = None
    selection = model_runs.select_for_review(transactions, backend.fetch_terms(), None, None, today=NOW)
    assert selection.count == 0 and selection.skipped["from before this app"] == 1


def test_a_charge_a_vp_flagged_is_left_to_the_treasurer(backend):
    transactions = load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    flags = pd.DataFrame({"transaction_id": [int(transactions.iloc[0]["transactionid"])]})
    selection = model_runs.select_for_review(transactions, backend.fetch_terms(), None, flags, today=NOW)
    assert selection.count == 0 and selection.skipped["flagged by a VP"] == 1


# ---------------------------------------------------------------- no repeats, no loops

def test_a_second_run_sends_nothing(backend, model):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5), charge("2026-09-23", -21.0, "BAR", 4)])
    first = review(backend)
    assert first.started and first.saved == 2
    assert len(model.calls) == 1

    second = review(backend)
    assert not second.started and second.refusal == "Nothing new to review."
    assert len(model.calls) == 1, "nothing was sent the second time"


def test_rebooking_a_charge_makes_it_eligible_exactly_once_more(backend, model):
    from ais_fmd.data.backend import TransactionChange

    transactions = load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    review(backend)
    tid = int(transactions.iloc[0]["transactionid"])
    backend.update_transactions([TransactionChange(tid, None, 8)], ACTOR)

    assert review(backend).saved == 1
    assert review(backend).refusal == "Nothing new to review."
    backend.update_transactions([TransactionChange(tid, None, 5)], ACTOR)
    assert review(backend).refusal == "Nothing new to review.", "back to a committee it was reviewed under"
    assert len(model.calls) == 2


def test_the_database_refuses_a_duplicate_verdict(backend):
    transactions = load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    tid = int(transactions.iloc[0]["transactionid"])
    row = {"transaction_id": tid, "booked_committee_id": 5, "verdict": "fits", "status": "cleared"}
    assert backend.insert_spend_reviews([row], ACTOR).updated == 1
    again = backend.insert_spend_reviews([row], ACTOR)
    assert (again.updated, again.unchanged) == (0, 1)


def test_two_runs_cannot_overlap(backend, model):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    held, _ = backend.start_llm_run(purpose="spend_review", model="claude-haiku-5-5", actor="other",
                                    rows_planned=1, calls_planned=1, reserved_usd=0.01,
                                    stale_before=(NOW - timedelta(minutes=15)).isoformat(),
                                    started_at=NOW.isoformat())
    assert held is not None
    report = review(backend)
    assert not report.started and "in progress" in report.refusal
    assert model.calls == []


def test_a_crashed_run_releases_its_lock_but_keeps_counting(backend):
    stale = (NOW - timedelta(hours=1)).isoformat()
    with backend._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO llm_runs (purpose, model, status, started_by, started_at, reserved_usd) "
            "VALUES ('spend_review', 'claude-haiku-5-5', 'running', 'x', ?, 0.5)", (stale,))
        connection.commit()
    run_id, result = backend.start_llm_run(
        purpose="spend_review", model="claude-haiku-5-5", actor=ACTOR, rows_planned=1,
        calls_planned=1, reserved_usd=0.01,
        stale_before=(NOW - timedelta(minutes=15)).isoformat(), started_at=NOW.isoformat())
    assert run_id is not None, result.error
    runs = backend.fetch_llm_runs()
    assert set(runs["status"]) == {"abandoned", "running"}
    assert metering.spent_this_month(runs, NOW) == pytest.approx(0.51)


def test_the_days_run_limit_holds(backend, model, monkeypatch):
    monkeypatch.setattr(settings, "llm_max_runs_per_day", lambda: 1)
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    with backend._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO llm_runs (purpose, model, status, started_by, started_at, reserved_usd) "
            "VALUES ('categorize', 'claude-haiku-5-5', 'done', 'x', ?, 0.01)", (NOW.isoformat(),))
        connection.commit()
    report = review(backend)
    assert not report.started and "limit" in report.refusal
    assert model.calls == []


# ---------------------------------------------------------------- spending

def test_the_monthly_cap_is_checked_before_a_run(backend, model, monkeypatch):
    monkeypatch.setattr(settings, "llm_monthly_cap_usd", lambda: 2.00)
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    with backend._connect() as connection:  # noqa: SLF001
        connection.execute(
            "INSERT INTO llm_runs (purpose, model, status, started_by, started_at, reserved_usd, cost_usd) "
            "VALUES ('spend_review', 'claude-haiku-5-5', 'done', 'x', ?, 2.0, 1.999)",
            ((NOW - timedelta(days=2)).isoformat(),))
        connection.commit()
    report = review(backend)
    assert not report.started and "cap" in report.refusal
    assert model.calls == []


def test_last_months_spend_does_not_count(backend):
    runs = pd.DataFrame([{"status": "done", "cost_usd": 5.0, "reserved_usd": 5.0,
                          "started_at": "2026-09-30T23:00:00+00:00"}])
    assert metering.spent_this_month(runs, NOW) == 0.0


def test_a_switched_off_model_is_never_admitted(backend, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", lambda: False)
    plan = metering.RunPlan("spend_review", rows=1, calls=1, prompt_chars_per_call=30000)
    assert metering.open_run(backend, plan, ACTOR, now=NOW).gate is None
    assert backend.fetch_llm_runs().empty


def test_an_unpriced_model_is_never_admitted(backend, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", lambda: True)
    monkeypatch.setattr(settings, "llm_model", lambda: "some-new-model")
    plan = metering.RunPlan("spend_review", rows=1, calls=1, prompt_chars_per_call=30000)
    admission = metering.open_run(backend, plan, ACTOR, now=NOW)
    assert admission.gate is None and "price" in admission.reason


def test_a_full_run_costs_cents_at_worst():
    """200 charges, five calls, every retry, full output: about five cents on Haiku."""
    plan = metering.RunPlan("spend_review", rows=200, calls=5, prompt_chars_per_call=34000)
    assert plan.reservation_usd("claude-haiku-5-5", 8000) < 0.06


def test_the_ledger_records_what_was_spent(backend, model):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    report = review(backend)
    run = backend.fetch_llm_runs().iloc[0]
    assert run["status"] == "done"
    assert (run["calls_planned"], run["calls_made"]) == (1, 1)
    assert run["cost_usd"] == pytest.approx((7000 * 0.10 + 50 * 0.50) / 1e6)
    assert run["cost_usd"] < run["reserved_usd"]
    assert report.cost_usd == pytest.approx(run["cost_usd"])


# ---------------------------------------------------------------- failures

def test_a_failed_run_closes_its_ledger_row_and_sends_again_next_time(backend, model):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    model.state["fail"] = True
    report = review(backend)
    assert report.started and report.batches_failed == 1 and report.saved == 0
    run = backend.fetch_llm_runs().iloc[0]
    assert run["status"] == "failed" and "overloaded" in run["error"]

    model.state["fail"] = False
    assert review(backend).saved == 1, "an unanswered charge is still eligible"


def test_verdicts_that_cannot_be_saved_stop_the_run(backend, model, monkeypatch):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5), charge("2026-09-23", -9.0, "X", 5)])
    monkeypatch.setattr(settings, "llm_batch_size", lambda: 1)
    monkeypatch.setattr(backend, "insert_spend_reviews",
                        lambda rows, actor: SimpleNamespace(error="disk full", updated=0))
    report = review(backend)
    assert len(model.calls) == 1, "no second call once results cannot be kept"
    assert backend.fetch_llm_runs().iloc[0]["status"] == "failed"
    assert any("could not be saved" in error for error in report.errors)


def test_flags_are_stored_open_and_fits_cleared(backend, model):
    load(backend, [charge("2026-09-22", -16.10, LOWES, 5)])
    model.state["verdict"] = "question"
    report = review(backend)
    stored = backend.fetch_spend_reviews().iloc[0]
    assert report.flagged == 1
    assert (stored["status"], stored["verdict"], stored["message_to_vp"]) == ("open", "question", "Hi?")
    assert stored["prompt_version"] and stored["model"] == "claude-haiku-5-5"

    assert backend.resolve_spend_review(int(stored["review_id"]), "dismissed", ACTOR).updated == 1
    assert backend.fetch_spend_reviews().iloc[0]["status"] == "dismissed"


# ---------------------------------------------------------------- the categorizer

def test_the_categorizer_sends_only_what_the_rules_left(backend, model):
    records = [
        {"transaction_date": "2026-09-22", "amount": -16.10, "details": LOWES, "account": "Wells Fargo"},
        {"transaction_date": "2026-09-01", "amount": -5.00,
         "details": "MONTHLY SERVICE FEE", "account": "Wells Fargo"},
    ]
    dry = model_runs.categorize_records(records)
    expected = len(dry.residual)
    run, report = model_runs.run_categorizer(backend, ACTOR, records, now=NOW)
    if expected == 0:
        assert not report.started and model.calls == []
    else:
        assert report.started and report.rows_sent == expected
        assert len(model.calls) == 1


def test_the_categorizer_without_a_run_returns_the_rules_answer(backend, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", lambda: False)
    records = [{"transaction_date": "2026-09-22", "amount": -16.10, "details": "UNKNOWN PLACE",
                "account": "Wells Fargo"}]
    run, report = model_runs.run_categorizer(backend, ACTOR, records, now=NOW)
    assert not report.started and run is not None
    assert backend.fetch_llm_runs().empty


def test_every_run_gets_a_five_minute_deadline(backend, model):
    import time as _time

    plan = metering.RunPlan("spend_review", rows=1, calls=1, prompt_chars_per_call=30000)
    gate = metering.open_run(backend, plan, ACTOR, now=NOW).gate
    assert 295 <= gate.deadline - _time.monotonic() <= 300


# --- Hard ceilings: a secret can lower a limit, never raise one -------------

@pytest.mark.parametrize("name, value, read, ceiling", [
    ("AIS_FMD_LLM_MONTHLY_CAP_USD", "500", settings.llm_monthly_cap_usd, settings.HARD_MONTHLY_CAP_USD),
    ("AIS_FMD_LLM_MAX_ROWS_PER_RUN", "100000", settings.llm_max_rows_per_run, settings.HARD_MAX_ROWS_PER_RUN),
    ("AIS_FMD_LLM_MAX_CALLS_PER_RUN", "1000", settings.llm_max_calls_per_run, settings.HARD_MAX_CALLS_PER_RUN),
    ("AIS_FMD_LLM_MAX_RUNS_PER_DAY", "1000", settings.llm_max_runs_per_day, settings.HARD_MAX_RUNS_PER_DAY),
    ("AIS_FMD_LLM_RUN_DEADLINE_SECONDS", "86400", settings.llm_run_deadline_seconds, settings.HARD_RUN_DEADLINE_SECONDS),
    ("AIS_FMD_LLM_MAX_OUTPUT_TOKENS", "64000", settings.llm_max_output_tokens, settings.HARD_MAX_OUTPUT_TOKENS),
])
def test_a_secret_cannot_raise_a_limit_past_its_ceiling(monkeypatch, name, value, read, ceiling):
    monkeypatch.setenv(name, value)
    assert read() == ceiling


def test_a_secret_can_still_lower_the_monthly_cap(monkeypatch):
    monkeypatch.setenv("AIS_FMD_LLM_MONTHLY_CAP_USD", "0.50")
    assert settings.llm_monthly_cap_usd() == 0.50


@pytest.mark.parametrize("value", ["nan", "-3", "inf"])
def test_a_nonsense_monthly_cap_never_opens_the_tap(monkeypatch, value):
    monkeypatch.setenv("AIS_FMD_LLM_MONTHLY_CAP_USD", value)
    assert settings.llm_monthly_cap_usd() <= settings.HARD_MONTHLY_CAP_USD
