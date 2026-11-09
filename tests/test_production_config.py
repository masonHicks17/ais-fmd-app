"""
The configuration path that decides whether this is the real app or the demo.

WHY THIS FILE EXISTS. The entire test suite runs in sandbox mode, deliberately.
That is the right default -- but it meant the one path that has to work on a
real deployment was the one path nothing exercised, and a launch-blocking bug
sat there undetected behind 583 green tests:

`settings.get_env()` read `os.environ` only. Streamlit copies top-level
secrets.toml values into `os.environ`, but *lazily*, on first access to
`st.secrets` -- and `app.py` called `is_sandbox()` before anything had touched
it. On Streamlit Community Cloud, where secrets.toml is the only channel there
is, `AIS_FMD_ENV` therefore read empty and the fail-closed default returned
SANDBOX. The deployed app would have seeded generated demo data, skipped the
login gate entirely (sandbox mode returns from `login_gate` immediately), and
served that to anyone with the URL. Silently.

The fail-closed property is still the most important thing in the module and is
covered by `test_sandbox_safety.py`. These tests cover the other half: that
production is actually *reachable* from each kind of host.
"""

from __future__ import annotations

import pytest
import streamlit as st

from ais_fmd import settings


class FakeSecrets:
    """
    Stands in for `st.secrets`.

    Not `AppTest.secrets`, and not the real file: `st.secrets` is process-global
    and merges over whatever `.streamlit/secrets.toml` the developer happens to
    have, so a test that relied on it would pass or fail based on a file that is
    gitignored. See the same warning in `tests/test_auth_gate.py`.
    """

    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture
def secrets(monkeypatch):
    """Install a fake `st.secrets` and clear the env vars these tests care about."""

    def _install(values: dict):
        monkeypatch.setattr(st, "secrets", FakeSecrets(values), raising=False)

    for name in ("AIS_FMD_ENV", "SUPABASE_URL", "ANTHROPIC_API_KEY", "AIS_FMD_LLM_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return _install


# --- The launch bug -----------------------------------------------------------

def test_production_is_reachable_from_a_secrets_file_alone(secrets):
    """
    The regression. A host with no settable environment variables must still be
    able to leave sandbox mode.
    """
    secrets({"AIS_FMD_ENV": "production"})
    assert settings.get_env() is settings.Env.PRODUCTION
    assert settings.is_production()
    assert not settings.is_sandbox()


def test_production_is_reachable_from_an_environment_variable_alone(secrets, monkeypatch):
    """The other kind of host: real env vars, no secrets file."""
    secrets({})
    monkeypatch.setenv("AIS_FMD_ENV", "production")
    assert settings.get_env() is settings.Env.PRODUCTION


def test_an_environment_variable_beats_a_secrets_file(secrets, monkeypatch):
    """
    Setting a real environment variable is the more deliberate act, so it wins.

    Pinned because the precedence is otherwise invisible, and a deployment that
    sets both while believing one of them is being read is exactly the kind of
    thing that produces a very confusing afternoon.
    """
    secrets({"AIS_FMD_ENV": "sandbox"})
    monkeypatch.setenv("AIS_FMD_ENV", "production")
    assert settings.is_production()


# --- Fail-closed still holds through the new path -----------------------------

@pytest.mark.parametrize(
    "value", ["", " ", "prod", "PRODUCTION ", "1", "true", "yes", "sandbox", "produktion"]
)
def test_only_the_exact_token_leaves_sandbox_via_secrets(secrets, value):
    """
    Reading a second source must not have widened what counts as production.

    `"PRODUCTION "` is in this list on purpose: it *does* resolve to production
    (the check trims and lowercases), so if this parametrisation ever starts
    failing on that one, read the assertion below before "fixing" it.
    """
    secrets({"AIS_FMD_ENV": value})
    expected_production = value.strip().lower() == "production"
    assert settings.is_production() is expected_production


def test_no_secrets_file_and_no_env_var_is_sandbox(secrets):
    """The state a fresh clone is in. Must be sandbox, and must not raise."""
    secrets({})
    assert settings.is_sandbox()


def test_a_broken_secrets_file_falls_back_rather_than_crashing(monkeypatch):
    """
    `st.secrets` raises when there is no secrets file, and can raise on a
    malformed one. Configuration lookup must treat that as "not configured",
    not as a crash on the first line of the app.
    """

    class Exploding:
        def get(self, key, default=None):
            raise RuntimeError("no secrets file")

    monkeypatch.setattr(st, "secrets", Exploding(), raising=False)
    monkeypatch.delenv("AIS_FMD_ENV", raising=False)
    assert settings.is_sandbox()
    assert settings.secret_section("supabase") == {}


# --- Sections ------------------------------------------------------------------

def test_a_secrets_section_is_readable(secrets):
    """
    The other half of the same bug. Streamlit copies only top-level scalars into
    `os.environ`, never section members -- so `[supabase]` in secrets.toml could
    never satisfy code reading `os.environ["SUPABASE_URL"]`, which is exactly
    what `secrets.toml.example` documented.
    """
    secrets({"supabase": {"url": "https://x.supabase.co", "secret_key": "sb_secret_x"}})
    section = settings.secret_section("supabase")
    assert section["url"] == "https://x.supabase.co"
    assert section["secret_key"] == "sb_secret_x"


def test_a_missing_section_is_empty_not_an_error(secrets):
    secrets({})
    assert settings.secret_section("supabase") == {}


# --- The example file has to match what the code reads -------------------------

def test_the_example_secrets_file_documents_keys_the_code_can_actually_read():
    """
    `secrets.toml.example` is the only instruction anyone deploying this will
    follow, and it previously described a `[supabase]` section whose keys the
    code could not read under any circumstances. Pin the two together.
    """
    example = (settings.PROJECT_ROOT / ".streamlit" / "secrets.toml.example").read_text(
        encoding="utf-8"
    )
    assert "[supabase]" in example
    assert "[treasury]" in example

    # Whatever key names the example teaches, SupabaseBackend must accept them.
    accepted = {
        "SUPABASE_URL", "url",
        "SUPABASE_ANON_KEY", "publishable_key", "anon_key", "key",
        "SUPABASE_SERVICE_KEY", "secret_key", "service_key",
    }
    documented = {
        line.split("=")[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    supabase_keys = documented & {
        "url", "publishable_key", "secret_key", "anon_key", "key", "service_key"
    }
    assert supabase_keys, "the example no longer documents any Supabase keys"
    assert supabase_keys <= accepted, (
        f"secrets.toml.example documents Supabase keys the backend does not read: "
        f"{sorted(supabase_keys - accepted)}"
    )
