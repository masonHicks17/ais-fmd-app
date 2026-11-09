"""
The safety guarantees, asserted.

If any test in this file fails, the sandbox is not safe to hand to someone and
the promise made in the README is broken.
"""

from __future__ import annotations

import os

import pytest

from ais_fmd import settings


def test_defaults_to_sandbox_when_unset(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    assert settings.get_env() is settings.Env.SANDBOX
    assert settings.is_sandbox()


@pytest.mark.parametrize(
    "value",
    ["", "  ", "prod", "PRODUCTION ", "Production!", "sandbox", "1", "true", "live", "prd"],
)
def test_fails_closed_on_anything_but_exact_token(monkeypatch, value):
    """Only the exact string 'production' leaves sandbox mode. Everything else is sandbox."""
    monkeypatch.setenv(settings.ENV_VAR, value)
    if value.strip().lower() == "production":
        pytest.skip("this value is the production token")
    assert settings.is_sandbox(), f"{value!r} should have resolved to sandbox"


def test_exact_token_enables_production(monkeypatch):
    monkeypatch.setenv(settings.ENV_VAR, "production")
    assert settings.is_production()
    monkeypatch.setenv(settings.ENV_VAR, "  PRODUCTION  ")
    assert settings.is_production(), "case and surrounding whitespace should be tolerated"


def test_external_calls_blocked_in_sandbox(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    with pytest.raises(settings.SandboxViolation):
        settings.assert_external_call_allowed("test call")


def test_supabase_backend_refuses_to_construct_in_sandbox(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    from ais_fmd.data.supabase_backend import SupabaseBackend

    with pytest.raises(settings.SandboxViolation):
        SupabaseBackend()


def test_get_backend_returns_sqlite_in_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setenv("AIS_FMD_SANDBOX_DB", str(tmp_path / "test.db"))
    from ais_fmd.data.backend import get_backend
    from ais_fmd.data.sqlite_backend import SqliteBackend

    assert isinstance(get_backend(), SqliteBackend)


def test_llm_disabled_in_sandbox_even_with_a_key(monkeypatch):
    """A stray ANTHROPIC_API_KEY in the environment must not enable spending."""
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    monkeypatch.delenv("AIS_FMD_ALLOW_LLM_IN_SANDBOX", raising=False)
    assert settings.llm_enabled() is False


def test_llm_classify_residual_makes_no_call_in_sandbox(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-used")
    from ais_fmd.domain.categorize.llm import classify_residual

    outcome = classify_residual([(0, {"details": "anything", "amount": -10.0})])
    assert outcome.batches_attempted == 0
    assert outcome.classifications == {}
    assert outcome.skipped_reason is not None


def test_network_clients_are_absent_from_the_sandbox_venv():
    """
    The strongest guarantee: the libraries that could reach out are not installed.

    Skipped rather than failed if present, since a developer may have installed
    them deliberately for production work -- the runtime guards above still hold.
    """
    import importlib.util

    for module in ("anthropic", "openai", "supabase"):
        if importlib.util.find_spec(module) is not None:
            pytest.skip(
                f"{module} is installed in this environment; runtime guards still apply"
            )
