"""
The production login gate.

This is the only thing standing between the internet and the organisation's
financial history, so the tests that matter are the ones asserting it **fails
closed**: no password configured, wrong password, and blank password must all
deny. A gate that opens when it is misconfigured is worse than no gate, because
it looks like security.

The sandbox path is asserted separately -- it must stay open and role-switchable
so the app is still usable with no setup at all.
"""

from __future__ import annotations

import pytest
import streamlit as st
from streamlit import config as st_config
from streamlit.testing.v1 import AppTest

from ais_fmd import auth, settings

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path):
    """
    Point Streamlit's secrets loader at a file this test owns.

    `st.secrets` is process-global and reads the developer's real
    `.streamlit/secrets.toml` regardless of what `AppTest.secrets` is set to --
    `AppTest.secrets` merges on top of the file rather than replacing it. On any
    machine that actually has a secrets file, which is every machine set up to
    run the production path, the real treasury password leaks in and
    `test_no_password_configured_denies_everyone` becomes unrunnable: the gate
    finds a password and renders a login form instead of the configuration
    error, so the assertion fails for a reason that has nothing to do with the
    gate. Same family of trap as the process-global caches in `test_views.py`.

    `_reset()` is private, but the alternative is leaving the single
    fails-closed property of the login gate untestable.
    """
    original = st_config.get_option("secrets.files")
    path = tmp_path / "secrets.toml"
    st_config.set_option("secrets.files", [str(path)])
    st.secrets._reset()
    yield path
    st_config.set_option("secrets.files", original)
    st.secrets._reset()


def _as_toml(secrets: dict) -> str:
    """Serialize the one shape these tests use: {section: {key: str}}."""
    lines: list[str] = []
    for section, values in secrets.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
    return "\n".join(lines) + "\n"


def gate_script() -> str:
    """A minimal app that is nothing but the gate, plus a marker after it."""
    return """
import streamlit as st
from ais_fmd import auth

auth.login_gate()
st.write("PAST_THE_GATE")
st.write(auth.current_user().email)
"""


def run_gate(monkeypatch, isolated_secrets, *, production: bool, secrets: dict) -> AppTest:
    monkeypatch.setenv(settings.ENV_VAR, "production" if production else "sandbox")
    if secrets:
        isolated_secrets.write_text(_as_toml(secrets), encoding="utf-8")
    st.secrets._reset()
    return AppTest.from_string(gate_script()).run()


# --- Fails closed ------------------------------------------------------------

def test_no_password_configured_denies_everyone(monkeypatch, isolated_secrets):
    """A missing secret must not read as 'no password required'."""
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={})
    assert not any("PAST_THE_GATE" in str(m.value) for m in app.markdown)
    assert app.error, "expected a configuration error to be shown"
    assert "Not configured" in app.error[0].value


def test_blank_password_configured_denies_everyone(monkeypatch, isolated_secrets):
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": "   "}})
    assert app.error
    assert "Not configured" in app.error[0].value


def test_gate_blocks_before_any_page_renders(monkeypatch, isolated_secrets):
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": PASSWORD}})
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


def test_wrong_password_is_rejected(monkeypatch, isolated_secrets):
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.text_input[0].set_value("not-the-password")
    app.button[0].click().run()
    assert app.error
    assert "Incorrect" in app.error[0].value
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


def test_empty_submission_is_rejected(monkeypatch, isolated_secrets):
    """Submitting nothing must not match a configured password."""
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.button[0].click().run()
    assert app.error
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered


# --- Opens for the right password --------------------------------------------

def test_correct_password_admits(monkeypatch, isolated_secrets):
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": PASSWORD}})
    # Type and submit in one run. The password box is inside `st.form`, and
    # from Streamlit 1.66 a run without the submit button discards the typed
    # value -- as a real browser does -- so `.set_value(...).run()` first would
    # submit an empty password.
    app.text_input[0].set_value(PASSWORD)
    app.button[0].click().run()
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" in rendered


def test_operator_email_is_used_when_configured(monkeypatch, isolated_secrets):
    app = run_gate(
        monkeypatch,
        isolated_secrets,
        production=True,
        secrets={"treasury": {"password": PASSWORD, "operator_email": "nic@example.edu"}},
    )
    app.text_input[0].set_value(PASSWORD)
    app.button[0].click().run()
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "nic@example.edu" in rendered


def test_pasted_password_with_stray_whitespace_admits(monkeypatch, isolated_secrets):
    """A password pasted from Notepad or a password manager often carries a space."""
    app = run_gate(monkeypatch, isolated_secrets, production=True, secrets={"treasury": {"password": PASSWORD}})
    app.text_input[0].set_value(f"  {PASSWORD} ")
    app.button[0].click().run()
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" in rendered


def test_signed_in_operator_is_admin(monkeypatch):
    monkeypatch.setenv(settings.ENV_VAR, "production")
    identity = auth.Identity(email="x@example.edu", role=auth.Role.ADMIN)
    assert identity.can(auth.Role.TREASURER)
    assert identity.can(auth.Role.ADMIN)


# --- Sandbox stays open ------------------------------------------------------

# --- Google sign-in (VP portal) -----------------------------------------------
#
# `st.user` is backed by real OIDC session machinery that AppTest has no hook
# to fake, so these test the pure resolution logic directly -- the part that
# decides who gets in as what -- rather than the `st.login()`/`st.user` glue
# in `login_gate`. That glue can only be verified against a real Google
# sign-in once `[auth]` secrets exist; see HANDOFF.md.

def test_a_profile_hit_becomes_the_recorded_role_and_committee():
    identity = auth._identity_for_google_user(
        {"email": "vp@ufl.edu"},
        fetch_profile=lambda email: {"role": "officer", "committee_id": 8},
    )
    assert identity == auth.Identity(email="vp@ufl.edu", role=auth.Role.OFFICER, committee_id=8)


def test_a_profile_with_no_committee_still_resolves():
    """Treasurer/admin profiles have no single committee -- that must not refuse them."""
    identity = auth._identity_for_google_user(
        {"email": "treasurer@ufl.edu"},
        fetch_profile=lambda email: {"role": "treasurer", "committee_id": None},
    )
    assert identity.role is auth.Role.TREASURER
    assert identity.committee_id is None


def test_no_matching_profile_is_refused_not_downgraded():
    """
    A miss must return None, not a fabricated MEMBER identity.

    Falling back to a default role would mean anyone with any Google account
    gets in as *something* -- exactly the open-registration hole FINDING F2
    documents at the top of this module.
    """
    identity = auth._identity_for_google_user(
        {"email": "stranger@gmail.com"}, fetch_profile=lambda email: None
    )
    assert identity is None


def test_an_unrecognised_role_string_is_refused():
    """A typo or a future role value in the data must not silently grant access."""
    identity = auth._identity_for_google_user(
        {"email": "vp@ufl.edu"},
        fetch_profile=lambda email: {"role": "chair", "committee_id": 5},
    )
    assert identity is None


def test_role_matching_is_case_insensitive():
    assert auth._role_from_string("Officer") is auth.Role.OFFICER
    assert auth._role_from_string("ADMIN") is auth.Role.ADMIN
    assert auth._role_from_string("") is None
    assert auth._role_from_string(None) is None


def test_a_blank_email_claim_is_refused():
    identity = auth._identity_for_google_user(
        {"email": ""}, fetch_profile=lambda email: {"role": "admin"}
    )
    assert identity is None


def test_google_auth_reports_unconfigured_with_no_auth_section(monkeypatch, isolated_secrets):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    isolated_secrets.write_text('[treasury]\npassword = "x"\n', encoding="utf-8")
    st.secrets._reset()
    assert not auth.google_login_available()


def test_google_auth_reports_configured_once_the_auth_section_exists(monkeypatch, isolated_secrets):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    isolated_secrets.write_text(
        "[auth]\n"
        'client_id = "x"\n'
        'client_secret = "y"\n'
        'server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"\n'
        'redirect_uri = "http://localhost:8501/oauth2callback"\n'
        'cookie_secret = "z"\n',
        encoding="utf-8",
    )
    st.secrets._reset()
    assert auth.google_login_available()


# --- Sandbox stays open ------------------------------------------------------

def test_sandbox_needs_no_password(monkeypatch, isolated_secrets):
    app = run_gate(monkeypatch, isolated_secrets, production=False, secrets={})
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" in rendered


def test_sandbox_identity_is_still_fabricated(monkeypatch):
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd import auth\n"
        "st.write(auth.current_user().role.label)\n"
    ).run()
    assert "Treasurer" in " ".join(str(m.value) for m in app.markdown)


def test_sandbox_login_switch_runs_the_real_gate(monkeypatch, isolated_secrets):
    """AIS_FMD_SANDBOX_LOGIN=1 closes the sandbox, so Google sign-in can be tried locally."""
    monkeypatch.setenv("AIS_FMD_SANDBOX_LOGIN", "1")
    app = run_gate(monkeypatch, isolated_secrets, production=False, secrets={"treasury": {"password": PASSWORD}})
    rendered = " ".join(str(m.value) for m in app.markdown)
    assert "PAST_THE_GATE" not in rendered
    app.text_input[0].set_value(PASSWORD)
    app.button[0].click().run()
    assert "PAST_THE_GATE" in " ".join(str(m.value) for m in app.markdown)


def test_sandbox_login_switch_has_no_effect_in_production(monkeypatch):
    monkeypatch.setenv(settings.ENV_VAR, "production")
    monkeypatch.setenv("AIS_FMD_SANDBOX_LOGIN", "1")
    assert not settings.sandbox_login_enabled()


# --- current_user contract ---------------------------------------------------

def test_current_user_refuses_to_fabricate_in_production(monkeypatch):
    """
    Skipping the gate must raise, not silently mint an identity.

    A default here would mean forgetting one call in app.py hands every visitor
    a working session.
    """
    monkeypatch.setenv(settings.ENV_VAR, "production")
    app = AppTest.from_string(
        "from ais_fmd import auth\n"
        "auth.current_user()\n"
    ).run()
    assert app.exception, "expected current_user() to raise without a gate"


def test_role_switcher_is_absent_in_production(monkeypatch):
    """The client must never be able to choose its own role in production."""
    monkeypatch.setenv(settings.ENV_VAR, "production")
    app = AppTest.from_string(
        "import streamlit as st\n"
        "from ais_fmd import auth\n"
        "auth.set_identity(auth.Identity('x@example.edu', auth.Role.ADMIN))\n"
        "auth.role_switcher()\n"
        "st.write('done')\n"
    ).run()
    # It renders in the profile menu now, not the sidebar, so look everywhere.
    assert not app.selectbox, "role switcher must not render in production"
