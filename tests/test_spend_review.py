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


@pytest.fixture
def stub_model(monkeypatch):
    """Route review_spend to a fake client; returns (set_reply, calls)."""
    calls: list[dict] = []
    state = {"reply": json.dumps({"results": []})}

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        message = SimpleNamespace(content=state["reply"])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake = types.ModuleType("openai")
    fake.OpenAI = lambda api_key: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setattr(settings, "llm_enabled", lambda: True)
    monkeypatch.setattr(settings, "assert_external_call_allowed", lambda what: None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub-not-real")

    def set_reply(value):
        state["reply"] = value

    return set_reply, calls


def test_review_makes_no_call_in_sandbox(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    outcome = review_spend([(0, _record(5))])
    assert outcome.batches_attempted == 0
    assert outcome.verdicts == {}
    assert outcome.skipped_reason is not None


def test_rows_without_a_committee_are_left_to_the_categorizer(stub_model):
    _, calls = stub_model
    outcome = review_spend([(3, _record(None)), (4, _record(float("nan"))), (5, _record(99))])
    assert outcome.not_reviewed == [3, 4, 5]
    assert calls == []


def test_review_sends_the_prompt_file_and_the_dues_baseline(stub_model):
    set_reply, calls = stub_model
    set_reply(json.dumps({"results": [{"index": 1, "verdict": "fits", "confidence": 0.9,
                                        "reason": "ok", "message_to_vp": ""}]}))
    review_spend([(7, _record(5))], dues_baseline="DUES BASELINE -- Fall 2026 (160 payments so far)")

    messages = calls[0]["messages"]
    assert messages[0] == {"role": "system", "content": load_spend_review_prompt()}
    user = messages[1]["content"]
    assert "Booked_To: 5 Membership" in user
    assert "Purchased: 09/22 (Tuesday)" in user
    assert user.rstrip().endswith("DUES BASELINE -- Fall 2026 (160 payments so far)")


def test_review_maps_verdicts_back_and_drops_what_it_cannot_trust(stub_model):
    set_reply, _ = stub_model
    set_reply("```json\n" + json.dumps({"results": [
        {"index": 1, "verdict": "question", "suggested_committee_id": 4, "confidence": 1.7,
         "reason": "Hardware is odd for Membership.", "message_to_vp": "Hi! What was the Lowe's charge for?"},
        {"index": 2, "verdict": "other_committee", "suggested_committee_id": 5, "confidence": 0.9,
         "reason": "Bar tab on the President's card in week 1.", "message_to_vp": "Hi! Membership social?"},
        {"index": 3, "verdict": "other_committee", "suggested_committee_id": 42, "confidence": 0.6,
         "reason": "Unknown committee id.", "message_to_vp": "Hi!"},
        {"index": 4, "verdict": "fits", "confidence": 0.9, "reason": "ok", "message_to_vp": "should be dropped"},
        {"index": 5, "verdict": "approve", "confidence": 0.9},
        {"index": 99, "verdict": "fits"},
    ]}) + "\n```")
    rows = [(10, _record(5)), (11, _record(4)), (12, _record(4)), (13, _record(5)), (14, _record(5))]
    outcome = review_spend(rows)

    assert set(outcome.verdicts) == {10, 11, 12, 13}
    assert outcome.verdicts[10] == SpendVerdict("question", None, 1.0, "Hardware is odd for Membership.",
                                                "Hi! What was the Lowe's charge for?")
    assert outcome.verdicts[11].suggested_committee_id == 5
    assert outcome.verdicts[12].suggested_committee_id is None
    assert outcome.verdicts[13].message_to_vp == ""
    assert set(outcome.flagged) == {10, 11, 12}


def test_review_never_changes_a_category(stub_model):
    set_reply, _ = stub_model
    set_reply(json.dumps({"results": [{"index": 1, "verdict": "other_committee",
                                        "suggested_committee_id": 5, "confidence": 0.9,
                                        "reason": "x", "message_to_vp": "y"}]}))
    rows = [(0, _record(4))]
    before = copy.deepcopy(rows)
    outcome = review_spend(rows)

    assert rows == before
    assert outcome.verdicts[0].suggested_committee_id == 5
    # A verdict can suggest a committee; it has nowhere to hold a booking.
    assert {f.name for f in fields(SpendVerdict)} == {
        "verdict", "suggested_committee_id", "confidence", "reason", "message_to_vp"}


def test_a_failed_batch_is_reported_not_swallowed(stub_model, monkeypatch):
    set_reply, calls = stub_model
    set_reply(RuntimeError("rate limited"))
    monkeypatch.setattr(settings, "llm_batch_size", lambda: 1)
    outcome = review_spend([(0, _record(5)), (1, _record(5))])
    assert (outcome.batches_attempted, outcome.batches_failed) == (2, 2)
    assert outcome.errors == ["RuntimeError: rate limited"] * 2
    assert len(calls) == 2


def test_classify_residual_still_skips_in_sandbox_with_its_own_message(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    outcome = llm.classify_residual([(0, {"details": "anything", "amount": -10.0})])
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
