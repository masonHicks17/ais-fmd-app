"""
Spend review: the prompt file, the model pass (against a stub -- the sandbox
cannot call a model), and the dues baseline it is handed.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import types
from dataclasses import fields
from types import SimpleNamespace

import pandas as pd
import pytest

from ais_fmd import settings
from ais_fmd.config.categories import COMMITTEE_BY_ID, RETIRED_LINES
from ais_fmd.domain.categorize import llm
from ais_fmd.domain.categorize.metering import ModelGate
from ais_fmd.domain.categorize.llm import (
    SpendVerdict,
    load_spend_review_prompt,
    review_spend,
)
from ais_fmd.domain.dues import dues_baseline


# ---------------------------------------------------------------- the prompt

def test_prompt_file_loads_without_its_editor_notes():
    prompt = load_spend_review_prompt()
    assert "<!--" not in prompt
    assert prompt.startswith("# Instructions")
    for section in ("## Start-of-term cover", "## Dues baseline", "## Output", "# Committee profiles"):
        assert section in prompt


def test_prompt_has_a_profile_for_every_committee():
    profiles = load_spend_review_prompt().split("# Committee profiles", 1)[1]
    ids = {int(found) for found in re.findall(r"^## (\d+) ", profiles, re.M)}
    assert set(COMMITTEE_BY_ID) <= ids, "a committee or ledger bucket has no profile"
    # The prompt still carries profiles for the retired lines (8, 12-16, 18); any
    # other extra id would be a typo or a stale committee.
    assert ids - set(COMMITTEE_BY_ID) <= set(RETIRED_LINES)


def test_prompt_does_not_hardcode_this_terms_dues_rate():
    """Rates come from `dues_baseline`, so a rate change needs no prompt edit."""
    dues_profile = load_spend_review_prompt().split("## 1 Dues", 1)[1].split("\n## ", 1)[0]
    assert "$" not in dues_profile.split("Money OUT")[0]


# ---------------------------------------------------------------- the model pass

def _record(committee_id, details="PURCHASE AUTHORIZED ON 09/22 LOWE'S #2365 GAINESVILLE FL CARD 0594",
            amount=-16.10, date="2026-09-22"):
    return {"budget_category": committee_id, "details": details, "amount": amount,
            "transaction_date": date, "account": "Wells Fargo"}


def _result(index, verdict="fits", amount=-16.10, suggested=None, confidence=0.9,
            reason="ok", message=""):
    return {"index": index, "amount": amount, "verdict": verdict,
            "suggested_committee_id": suggested, "confidence": confidence,
            "reason": reason, "message_to_vp": message}


def _gate(**overrides):
    values = dict(model="claude-haiku-5-5", budget_usd=1.0, max_calls=10, max_output_tokens=8000)
    values.update(overrides)
    return ModelGate(**values)


@pytest.fixture
def stub_model(monkeypatch):
    """Route the model pass to a fake Anthropic client; returns (set_reply, calls)."""
    calls: list[dict] = []
    state = {"reply": json.dumps({"results": []}), "stop_reason": "end_turn"}

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=state["reply"])],
            stop_reason=state["stop_reason"],
            usage=SimpleNamespace(input_tokens=6000, output_tokens=400,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **kwargs: SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(settings, "llm_enabled", lambda: True)
    monkeypatch.setattr(settings, "assert_external_call_allowed", lambda what: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub-not-real")

    def set_reply(value, stop_reason="end_turn"):
        state["reply"] = value
        state["stop_reason"] = stop_reason

    return set_reply, calls


def test_review_makes_no_call_in_sandbox(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    outcome = review_spend([(0, _record(5))], gate=_gate())
    assert outcome.batches_attempted == 0
    assert outcome.verdicts == {}
    assert outcome.skipped_reason is not None


def test_no_gate_means_no_call_even_in_production(stub_model):
    """Every page that runs the pipeline on load runs it without a gate."""
    _, calls = stub_model
    outcome = review_spend([(0, _record(5))])
    assert calls == []
    assert "metered run" in outcome.skipped_reason
    assert llm.classify_residual([(0, {"details": "x", "amount": -1.0})]).batches_attempted == 0
    assert calls == []


def test_rows_without_a_committee_are_left_to_the_categorizer(stub_model):
    _, calls = stub_model
    outcome = review_spend([(3, _record(None)), (4, _record(float("nan"))), (5, _record(99))], gate=_gate())
    assert outcome.not_reviewed == [3, 4, 5]
    assert calls == []


def test_review_sends_the_prompt_file_the_baseline_and_a_schema(stub_model):
    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [_result(1)]}))
    review_spend([(7, _record(5))], dues_baseline="DUES BASELINE -- Fall 2026 (160 payments so far)",
                 gate=_gate())

    request = calls[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["system"][0]["text"] == load_spend_review_prompt()
    assert "cache_control" not in request["system"][0], "one batch gains nothing from a cache write"
    assert request["thinking"] == {"type": "adaptive"}, "the model reasons before it answers"
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in request, "Haiku 5.5 rejects non-default sampling"
    user = request["messages"][0]["content"]
    assert "Booked_To: 5 Membership" in user
    assert "Purchased: 09/22 (Tuesday)" in user
    assert user.rstrip().endswith("DUES BASELINE -- Fall 2026 (160 payments so far)")


def test_review_maps_verdicts_back_and_drops_what_it_cannot_trust(stub_model):
    set_reply, _ = stub_model
    set_reply("```json\n" + json.dumps({"results": [
        _result(1, "question", confidence=1.7, reason="Hardware is odd for Membership.",
                message="Hi! What was the Lowe's charge for?"),
        _result(2, "other_committee", suggested=5, reason="Bar tab on the President's card in week 1.",
                message="Hi! Membership social?"),
        _result(3, "other_committee", suggested=42, confidence=0.6, reason="Unknown committee id.",
                message="Hi!"),
        _result(4, "fits", message="should be dropped"),
        _result(5, "approve"),
    ]}) + "\n```")
    rows = [(10, _record(5)), (11, _record(4)), (12, _record(4)), (13, _record(5)), (14, _record(5))]
    outcome = review_spend(rows, gate=_gate())

    assert set(outcome.verdicts) == {10, 11, 12, 13}
    assert outcome.verdicts[10] == SpendVerdict("question", None, 1.0, "Hardware is odd for Membership.",
                                                "Hi! What was the Lowe's charge for?")
    assert outcome.verdicts[11].suggested_committee_id == 5
    assert outcome.verdicts[12].suggested_committee_id is None
    assert outcome.verdicts[13].message_to_vp == ""
    assert set(outcome.flagged) == {10, 11, 12}


@pytest.mark.parametrize("results, why", [
    ([_result(1), _result(1)], "twice"),
    ([_result(1), _result(9)], "not sent"),
    ([_result(1), _result(2, amount=-99.0)], "amount"),
])
def test_a_reply_that_lost_track_of_its_rows_is_thrown_away_whole(stub_model, results, why):
    """An answer attached to the wrong charge is worse than no answer."""
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": results}))
    outcome = review_spend([(0, _record(5)), (1, _record(5))], gate=_gate())
    assert outcome.verdicts == {}
    assert outcome.batches_failed == 1
    assert why in outcome.errors[0]


def test_a_skipped_row_is_simply_left_unanswered(stub_model):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [_result(2, "question", message="Hi?")]}))
    outcome = review_spend([(0, _record(5)), (1, _record(5))], gate=_gate())
    assert set(outcome.verdicts) == {1}
    assert outcome.batches_failed == 0


def test_a_flag_with_no_message_gets_the_fallback(stub_model):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [
        _result(1, "question", amount=20.0, message=""),
        _result(2, "question", message="  "),
    ]}))
    dues = _record(1, details="ZELLE FROM A MEMBER ON 08/18 REF # X MEMBERSHIP DUES", amount=20.0,
                   date="2026-08-18")
    outcome = review_spend([(0, dues), (1, _record(5))], gate=_gate())
    # A dues question is the treasurer's own: the prompt leaves it without a
    # VP message, and the fallback does not invent one (nor name the payer).
    assert outcome.verdicts[0].message_to_vp == ""
    assert outcome.verdicts[1].message_to_vp == (
        "Hi! Could you tell us what the $16.10 charge on 09/22 was for?")


def test_the_fallback_wording_for_money_received():
    record = {"amount": 20.0, "details": "ZELLE FROM A MEMBER ON 08/18 REF # X", "transaction_date": "2026-08-18"}
    assert llm.fallback_message(record) == (
        "Hi! Could you tell us what the $20.00 payment we received on 08/18 was for?")


def test_suggesting_the_committee_it_is_already_booked_to_is_no_suggestion(stub_model):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [_result(1, "other_committee", suggested=5, message="Hi?")]}))
    outcome = review_spend([(0, _record(5))], gate=_gate())
    assert outcome.verdicts[0].suggested_committee_id is None


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_a_refused_or_cut_off_reply_fails_its_batch(stub_model, stop_reason):
    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [_result(1)]}), stop_reason=stop_reason)
    outcome = review_spend([(0, _record(5))], gate=_gate())
    assert outcome.verdicts == {}
    assert outcome.batches_failed == 1
    assert len(calls) == 1, "nothing is retried"


def test_the_gate_stops_calls_past_its_limit(stub_model, monkeypatch):
    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [_result(1)]}))
    monkeypatch.setattr(settings, "llm_batch_size", lambda: 1)
    gate = _gate(max_calls=2)
    outcome = review_spend([(i, _record(5)) for i in range(4)], gate=gate)
    assert len(calls) == 2
    assert (outcome.batches_attempted, outcome.batches_failed) == (4, 2)
    assert gate.calls == 2
    assert gate.spent_usd == pytest.approx(2 * (6000 * 0.10 + 400 * 0.50) / 1e6)


def test_every_batch_is_handed_over_as_it_arrives(stub_model, monkeypatch):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [_result(1)]}))
    monkeypatch.setattr(settings, "llm_batch_size", lambda: 1)
    seen = []
    review_spend([(0, _record(5)), (1, _record(5))], gate=_gate(), on_batch=seen.append)
    assert [set(batch) for batch in seen] == [{0}, {1}]


def test_review_never_changes_a_category(stub_model):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [_result(1, "other_committee", suggested=5, reason="x", message="y")]}))
    rows = [(0, _record(4))]
    before = copy.deepcopy(rows)
    outcome = review_spend(rows, gate=_gate())

    assert rows == before
    assert outcome.verdicts[0].suggested_committee_id == 5
    # A verdict can suggest a committee; it has nowhere to hold a booking.
    assert {f.name for f in fields(SpendVerdict)} == {
        "verdict", "suggested_committee_id", "confidence", "reason", "message_to_vp"}


def test_a_failed_batch_is_reported_not_swallowed(stub_model, monkeypatch):
    set_reply, calls = stub_model
    set_reply(RuntimeError("rate limited"))
    monkeypatch.setattr(settings, "llm_batch_size", lambda: 1)
    outcome = review_spend([(0, _record(5)), (1, _record(5))], gate=_gate())
    assert (outcome.batches_attempted, outcome.batches_failed) == (2, 2)
    assert outcome.errors == ["RuntimeError: rate limited"] * 2
    assert len(calls) == 2


def test_classify_residual_still_skips_in_sandbox_with_its_own_message(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    outcome = llm.classify_residual([(0, {"details": "anything", "amount": -10.0})], gate=_gate())
    assert outcome.skipped_reason.endswith("left uncategorized for the review queue.")


# ---------------------------------------------------------------- dues baseline

TERMS = pd.DataFrame([
    {"TermID": "SP26", "Semester": "Spring 2026", "start_date": "2026-01-05", "end_date": "2026-05-15"},
    {"TermID": "FA26", "Semester": "Fall 2026", "start_date": "2026-08-15", "end_date": "2026-12-18"},
])


def _dues(amounts, date, committee=1):
    return pd.DataFrame({"transaction_date": date, "amount": amounts, "budget_category": committee,
                         "details": "ZELLE FROM A MEMBER ON 08/19 REF # X DUES"})


def test_baseline_reads_the_standard_and_established_rates_from_the_term():
    frame = _dues([50.0] * 40 + [65.0] * 4 + [35.0] * 3 + [15.0], "2026-08-19")
    baseline = dues_baseline(frame, TERMS, "Fall 2026")

    assert baseline.standard == 50.0
    assert [amount for amount, _ in baseline.established] == [65.0, 35.0]
    assert baseline.render() == (
        "DUES BASELINE -- Fall 2026 (48 payments so far)\n"
        "standard: $50.00 (83%)\n"
        "also established: $65.00 (8%), $35.00 (6%)\n"
        "source: this term"
    )


def test_five_payments_establish_a_rate_even_under_five_percent():
    frame = _dues([50.0] * 110 + [65.0] * 5, "2026-08-19")
    assert [a for a, _ in dues_baseline(frame, TERMS, "Fall 2026").established] == [65.0]


def test_a_young_term_borrows_the_last_term_with_enough_payments():
    frame = pd.concat([_dues([35.0] * 40, "2026-01-12"), _dues([50.0] * 3, "2026-08-16")])
    baseline = dues_baseline(frame, TERMS, "Fall 2026")

    assert (baseline.source, baseline.payments, baseline.standard) == ("Spring 2026", 3, 35.0)
    assert baseline.render().endswith("source: Spring 2026 (this term has only 3 so far)")


def test_baseline_counts_only_money_in_booked_to_dues():
    frame = pd.concat([
        _dues([50.0] * 30, "2026-08-19"),
        _dues([-35.0] * 10, "2026-08-20"),               # refunds out
        _dues([20.0] * 40, "2026-08-21", committee=5),   # social tickets
    ])
    baseline = dues_baseline(frame, TERMS, "Fall 2026")
    assert (baseline.payments, baseline.standard, baseline.established) == (30, 50.0, ())
    assert "also established: none" in baseline.render()


def test_no_dues_anywhere_means_no_baseline():
    assert dues_baseline(pd.DataFrame(), TERMS, "Fall 2026") is None
    assert dues_baseline(_dues([20.0] * 5, "2026-08-21", committee=5), TERMS, "Fall 2026") is None


def test_a_run_past_its_time_limit_starts_no_more_calls(stub_model):
    """Five minutes is the whole run's budget; past it, nothing new is sent."""
    import time as _time

    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [_result(1)]}))
    gate = _gate(deadline=_time.monotonic() - 1)
    outcome = review_spend([(0, _record(5))], gate=gate)
    assert calls == []
    assert outcome.batches_failed == 1 and "time limit" in outcome.errors[0]


def test_no_call_is_allowed_to_outlive_the_run(stub_model):
    import time as _time

    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [_result(1)]}))
    review_spend([(0, _record(5))], gate=_gate(deadline=_time.monotonic() + 20))
    assert 1.0 <= calls[0]["timeout"] <= 20.0
    review_spend([(0, _record(5))], gate=_gate())
    assert calls[1]["timeout"] == 60.0
