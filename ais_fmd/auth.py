"""
Roles and access control.

FINDING F2. The original had open registration -- anyone reaching the URL could
create an account and read the organisation's complete financial history -- and
gated the treasury portal, which grants unrestricted service-role writes, behind
a single shared password compared with `==`. There were no roles, so there was
no way to give a committee chair sight of their own budget without giving them
everything.

The model here is role-based: every page declares the role it needs, and the
check happens in one place. In production `current_user()` would read a Supabase
session and look the role up in a `profiles` table with RLS enforcing it
server-side; the sandbox lets you switch roles from the profile menu so the gating
is actually testable without standing up an auth provider.

The important part is the *shape*: authorisation is a property of the user, not
a secret that gets shared around a committee and never rotated.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum

import streamlit as st

from . import settings


class Role(IntEnum):
    """Ordered, so `>=` expresses "at least this much access"."""

    MEMBER = 10
    OFFICER = 20  # a VP
    TREASURER = 30
    # Ranked above Treasurer so `can(TREASURER)` is true: the President sees every
    # page the Treasurer does. What they cannot do is write -- see `Identity.read_only`.
    PRESIDENT = 35
    ADMIN = 40

    @property
    def label(self) -> str:
        return {
            Role.MEMBER: "Member",
            Role.OFFICER: "VP",
            Role.TREASURER: "Treasurer",
            Role.PRESIDENT: "President",
            Role.ADMIN: "Admin",
        }[self]


ROLE_DESCRIPTIONS = {
    Role.MEMBER: "Read-only dashboards and reports.",
    Role.OFFICER: "Adds their own committee's detail and reimbursement requests.",
    Role.TREASURER: "Uploads statements, edits transactions, sets budgets.",
    Role.PRESIDENT: "Sees everything the Treasurer sees, but cannot change anything.",
    Role.ADMIN: "Everything, plus data-quality and audit tooling.",
}

SESSION_KEY = "ais_identity"


@dataclass(frozen=True)
class Identity:
    email: str
    role: Role
    committee_id: int | None = None

    def can(self, required: Role) -> bool:
        return self.role >= required

    @property
    def read_only(self) -> bool:
        """
        True for the President: every page opens, nothing can be changed.

        Pages show or hide their edit controls with `can_write`, and the write
        functions in `data/repositories.py` refuse a read-only identity
        themselves, so a page that forgets to check still cannot change data.
        """
        return self.role == Role.PRESIDENT

    @property
    def can_write(self) -> bool:
        """Whether this identity may change transactions, budgets, terms or the roster."""
        return self.role >= Role.TREASURER and not self.read_only

    @property
    def committee_scoped(self) -> bool:
        """
        True for a VP: an Officer who is not also a Treasurer or Admin.

        A scoped identity gets its own short navigation (`nav.VP_PAGES`) instead
        of the full one, because most pages are open to any role at or above
        MEMBER and show every committee's data -- ranking an Officer above
        MEMBER would otherwise hand a VP the whole ledger.
        """
        return self.role == Role.OFFICER


def _default_identity() -> Identity:
    return Identity(email="treasurer@sandbox.local", role=Role.TREASURER)


def current_user() -> Identity:
    """
    The active identity.

    Sandbox fabricates one so the app is usable with no setup. Production never
    fabricates: it returns whatever `login_gate` put in the session, and if
    nothing is there the caller has skipped the gate -- which is a programming
    error, not a state to paper over with a default.
    """
    if _sandbox_open():
        if SESSION_KEY not in st.session_state:
            st.session_state[SESSION_KEY] = _default_identity()
            _restore_person_from_url()
        return st.session_state[SESSION_KEY]

    identity = st.session_state.get(SESSION_KEY)
    if identity is None:
        raise RuntimeError(
            "No authenticated identity. app.py must call auth.login_gate() "
            "before rendering any page."
        )
    return identity


def _sandbox_open() -> bool:
    """Sandbox without the login gate: identity fabricated, roles switchable."""
    return settings.is_sandbox() and not settings.sandbox_login_enabled()


# --- Production login --------------------------------------------------------
#
# One operator, one shared password, matching the pattern the original app
# already used in production (`st.secrets["treasury"]["password"]`) so the same
# secret carries over.
#
# This is deliberately NOT Supabase Auth. With a single trusted operator there
# are no per-user roles to enforce, so a login provider, a profiles table and
# RLS policies would all be machinery serving one account. The `Identity`/`Role`
# shape below is still the real one, so adding genuine multi-user auth later
# means replacing this function -- not rewriting every page.

_PASSWORD_ATTEMPT_KEY = "ais_login_attempts"
MAX_LOGIN_ATTEMPTS = 10


def _configured_password() -> str | None:
    """The operator password from secrets, or None if none is configured."""
    try:
        section = st.secrets.get("treasury", {})
    except Exception:  # noqa: BLE001 - no secrets file at all is a valid state
        return None
    password = str(section.get("password", "") or "").strip()
    return password or None


def _operator_email() -> str:
    try:
        section = st.secrets.get("treasury", {})
        email = str(section.get("operator_email", "") or "").strip()
    except Exception:  # noqa: BLE001
        email = ""
    return email or "treasurer"


def login_gate() -> None:
    """
    Require a real identity before anything renders, in production only.

    Two independent paths land in the same `Identity` shape, so nothing past
    this function knows or cares which one ran:

      1. **Google sign-in**, for VPs. `st.login()` is Streamlit's own OIDC
         flow (Authlib) -- Supabase Auth is not involved, because the app
         talks to Supabase with its own service/anon key regardless of who is
         using it. A signed-in Google identity is looked up by email in
         `profiles`; a hit becomes that person's real role and committee, a
         miss is refused rather than silently downgraded to MEMBER, because a
         wrong email in `profiles` should look like an error, not like reduced
         access.
      2. **The shared treasury password**, unchanged from before. Kept as the
         fallback that cannot be broken by a typo in `profiles` or by Google
         being unconfigured -- the treasurer is never locked out by this
         feature landing.

    Halts the script when not signed in, so a page cannot render for an
    unauthenticated visitor even if it forgets to call `require`.

    Skipped in sandbox unless `AIS_FMD_SANDBOX_LOGIN=1`.
    """
    if _sandbox_open():
        return
    if SESSION_KEY in st.session_state:
        return

    st.title("UF AIS Financial Management")

    denied_google_email: str | None = None
    if _google_auth_configured():
        if getattr(st.user, "is_logged_in", False):
            lookup_failed = False
            try:
                identity = _identity_for_google_user(dict(st.user))
            except Exception:  # noqa: BLE001 - fail closed, whatever went wrong
                # The access list could not be read at all -- most likely the
                # `profiles` table has not been created yet. Refuse, say what
                # actually happened (not "you are not on the list", which would
                # send a VP to the treasurer for the wrong reason), and leave the
                # treasury password form below working. An unhandled error here
                # would show a signed-in VP a stack trace instead.
                identity = None
                lookup_failed = True
                st.warning(
                    "Signed in with Google, but access could not be checked right "
                    "now. Try again shortly, or ask the treasurer."
                )
            if identity is not None:
                st.session_state[SESSION_KEY] = identity
                st.rerun()
            # Logged into Google, but no matching profiles row: refused, not
            # silently downgraded. Falls through to the password form below
            # rather than stopping here, so a treasurer whose own email is
            # mistyped in `profiles` still has a way in.
            if not lookup_failed:
                denied_google_email = str(st.user.get("email") or "unknown address")
        else:
            st.caption("VP access")
            if st.button("Sign in with Google", type="primary", key="google_login_button"):
                st.login()
            st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)

    if denied_google_email:
        st.error(
            f"**{denied_google_email}** is not set up for access. Ask the "
            "treasurer to add it under Officer Access, or sign in below with "
            "the treasury password."
        )

    st.caption("Treasury access")

    expected = _configured_password()
    if expected is None:
        # Fail closed, loudly. A missing password in production must never mean
        # "let everyone in" -- which is what a falsy default would do.
        st.error(
            "**Not configured.** No treasury password is set, so the app cannot "
            "verify anyone. Add it to the deployment's secrets:\n\n"
            "```toml\n[treasury]\npassword = \"...\"\n```"
        )
        st.stop()

    attempts = st.session_state.get(_PASSWORD_ATTEMPT_KEY, 0)
    if attempts >= MAX_LOGIN_ATTEMPTS:
        st.error("Too many attempts. Reload the page to try again.")
        st.stop()

    with st.form("login"):
        supplied = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        # Constant-time: a plain `==` leaks the length of the shared prefix
        # through timing. Cheap to do correctly.
        # Trimmed like `expected` already is: a password pasted from a notes
        # app or password manager often carries a trailing space or newline.
        if hmac.compare_digest(supplied.strip(), expected):
            st.session_state[SESSION_KEY] = Identity(
                email=_operator_email(), role=Role.ADMIN
            )
            st.session_state[_PASSWORD_ATTEMPT_KEY] = 0
            st.rerun()
        else:
            st.session_state[_PASSWORD_ATTEMPT_KEY] = attempts + 1
            st.error("Incorrect password.")

    st.stop()


# --- Google sign-in (VP portal) -----------------------------------------------
#
# Split deliberately into a thin Streamlit-facing half (`_google_auth_configured`,
# the `st.user` reads inside `login_gate`) and a pure half (`_identity_for_google_user`,
# `_role_from_string`) that takes a plain dict and a profile row and returns an
# Identity. `st.user` is backed by Streamlit's real OIDC session machinery, which
# `AppTest` has no hook to fake, so the pure half is what `tests/test_auth_gate.py`
# can actually exercise; the glue half can only be verified against a real Google
# sign-in once `[auth]` secrets exist. See HANDOFF.md for that outstanding step.

_ROLE_BY_NAME: dict[str, Role] = {
    "member": Role.MEMBER,
    "officer": Role.OFFICER,
    "vp": Role.OFFICER,
    "president": Role.PRESIDENT,
    "treasurer": Role.TREASURER,
    "admin": Role.ADMIN,
}


def _role_from_string(value: object) -> Role | None:
    return _ROLE_BY_NAME.get(str(value or "").strip().lower())


def google_login_available() -> bool:
    """Public wrapper -- pages that mention Google sign-in check this, not the private flag."""
    return _google_auth_configured()


def _google_auth_configured() -> bool:
    """
    Whether `st.login()` has anything to do.

    Checked before rendering the button or touching `st.user`, so a deployment
    that has not set up Google yet behaves exactly as it did before this
    feature existed -- password only, no button, no change in flow.
    """
    try:
        section = st.secrets.get("auth", {})
    except Exception:  # noqa: BLE001 - no secrets file at all is a valid state
        return False
    return bool(section.get("client_id")) and bool(section.get("server_metadata_url"))


_ROSTER_PATH = settings.PROJECT_ROOT / "ais_fmd" / "config" / "officer_roster.json"


def _roster_profile(email: str) -> dict | None:
    """The hard-coded officer mailbox for this email, shaped like a `profiles` row."""
    import json

    roster = json.loads(_ROSTER_PATH.read_text(encoding="utf-8"))
    entry = roster.get(email.strip().lower())
    if not isinstance(entry, dict):
        return None
    return {"role": entry.get("role"), "committee_id": entry.get("committee")}


def _default_profile_lookup(email: str) -> dict | None:
    """officer_roster.json first; then anyone added under Officer Access."""
    profile = _roster_profile(email)
    if profile is not None:
        return profile

    from .data import repositories as repo

    try:
        return repo.backend().fetch_profile(email)
    except Exception:  # noqa: BLE001 - no `profiles` table yet must mean "not listed", not a crash
        return None


def _identity_for_google_user(
    user_info: dict, *, fetch_profile=_default_profile_lookup
) -> Identity | None:
    """
    A signed-in Google identity, resolved against `profiles`, or None on a miss.

    Pure given `user_info` (an OIDC claims dict -- at minimum `email`) and a
    lookup function. `fetch_profile` is injected rather than imported at call
    time specifically so this -- the part that actually decides who gets in as
    what -- is unit-testable with a fake roster and no real database, let alone
    a real OIDC session. `login_gate` calls this with the real default.
    """
    email = str(user_info.get("email") or "").strip()
    if not email:
        return None
    # Google puts `email_verified` in the identity token. An address the provider
    # has not verified proves nothing about who is typing it, so it cannot be
    # matched against an access list. Absent is treated as fine (a plain dict in
    # a test, or a provider that omits it); explicitly false is refused.
    if user_info.get("email_verified") is False:
        return None
    profile = fetch_profile(email)
    if profile is None:
        return None
    role = _role_from_string(profile.get("role"))
    if role is None:
        return None
    committee_id = profile.get("committee_id")
    return Identity(
        email=email,
        role=role,
        committee_id=int(committee_id) if committee_id is not None else None,
    )


def sign_out() -> None:
    st.session_state.pop(SESSION_KEY, None)
    if _google_auth_configured() and getattr(st.user, "is_logged_in", False):
        st.logout()


def set_identity(identity: Identity) -> None:
    st.session_state[SESSION_KEY] = identity


def require(required: Role) -> Identity:
    """
    Gate a page. Renders an explanation and halts when the role is insufficient.

    Because every page calls this, adding a page cannot accidentally skip the
    check the way a copy-pasted password prompt could.
    """
    identity = current_user()
    if identity.can(required):
        return identity

    st.warning(
        f"**{required.label} access required.**\n\n"
        f"You are signed in as **{identity.role.label}**. "
        f"{ROLE_DESCRIPTIONS[required]}"
    )
    if _sandbox_open():
        st.caption("Switch roles from the profile menu (top right) to explore this page.")
    st.stop()
    raise AssertionError("unreachable")  # pragma: no cover


def refuse_read_only(identity: Identity) -> None:
    """
    Turn a read-only identity away from a page that exists only to change things.

    Called straight after `require(...)`, which every page must do first.
    """
    if not identity.read_only:
        return

    st.warning(
        f"**Read-only access.** You are signed in as **{identity.role.label}**. "
        "This page only changes settings, so there is nothing here to view."
    )
    st.stop()
    raise AssertionError("unreachable")  # pragma: no cover


def read_only_notice(identity: Identity) -> None:
    """A one-line banner for pages a read-only identity can open but not change."""
    if identity.read_only:
        st.info("You are signed in as President: this page is read-only for you.", icon=":material/lock:")


_DEMO_NONE = "—"


def _demo_profile_emails() -> list[str]:
    """The fake profiles in the sandbox's access list, for the demo sign-in picker."""
    try:
        from .data import repositories as repo

        frame = repo.load_profiles()
    except Exception:  # noqa: BLE001 - a sandbox with no profiles simply has no demo sign-in
        return []
    if frame is None or frame.empty or "email" not in frame.columns:
        return []
    return sorted(str(email) for email in frame["email"])


def _apply_demo_profile() -> None:
    """
    Sign in as the chosen fake profile, via the same lookup Google sign-in uses.

    A callback, so it runs before the page reruns. It also sets the role and
    committee pickers to match: otherwise their remembered values would be read
    next and quietly put the identity back to whatever was chosen before.
    """
    email = st.session_state.get("ais_demo_profile")
    st.session_state["ais_demo_profile"] = _DEMO_NONE  # back to the prompt; the identity stays
    if not email or email == _DEMO_NONE:
        return
    identity = _identity_for_google_user({"email": email, "email_verified": True})
    if identity is None:
        return
    set_identity(identity)
    st.session_state.pop("ais_signin_picker", None)  # re-derived from the identity on the next run


def sandbox_people() -> list[tuple[str, Role, int | None]]:
    """Who can sign in: (label, role, committee) for the President, Treasurer and each VP."""
    from .config.vp_committees import PRESIDENT_LINE_ID, VP_COMMITTEES

    return [
        ("President", Role.PRESIDENT, PRESIDENT_LINE_ID),
        ("Treasurer", Role.TREASURER, None),
        *[(f"{committee.title} VP", Role.OFFICER, committee.budget_ids[0]) for committee in VP_COMMITTEES],
    ]


def role_switcher() -> None:
    """
    Sandbox-only control for exercising the gating.

    Renders into whatever container is active -- the profile menu, since the
    sidebar was retired on 2026-11-08 (`shell.profile_menu`).

    Deliberately absent in production -- there, the role comes from the
    authenticated session and cannot be chosen by the client.
    """
    if not _sandbox_open():
        return

    identity = current_user()

    demo_emails = _demo_profile_emails()
    if demo_emails:
        st.selectbox(
            "Demo sign-in",
            [_DEMO_NONE] + demo_emails,
            key="ais_demo_profile",
            on_change=_apply_demo_profile,
            help=(
                "Sandbox only. Signs in as one of the fake profiles, through the same "
                "lookup Google sign-in will use, so you see exactly what that person would."
            ),
        )

    # The people who sign in are the committee heads: the President, the
    # Treasurer (who heads Treasury) and one VP per committee. Members and
    # admins never sign in, so they are not offered here (2026-11-09).
    people = sandbox_people()
    labels = [label for label, _role, _committee in people]
    current = next(
        (label for label, role, committee in people
         if role == identity.role and (role != Role.OFFICER or committee == identity.committee_id)),
        None,
    )
    # Changing the identity happens only when someone picks, never because the
    # picker rendered: an identity not on the list (a test's plain member, a
    # demo profile) is left as it is and the picker shows nothing chosen.
    if st.session_state.get("ais_signin_picker") != current:
        st.session_state["ais_signin_picker"] = current
    st.selectbox(
        "Signed in as",
        labels,
        index=None,
        placeholder="Someone else",
        key="ais_signin_picker",
        on_change=_apply_sandbox_person,
        help="Sandbox only. In production this comes from Google sign-in and Officer Access.",
    )


def person_label(identity: Identity) -> str | None:
    """The sign-in picker label for `identity` ("Membership VP"), or None if it isn't on the list."""
    return next(
        (label for label, role, committee in sandbox_people()
         if role == identity.role and (role != Role.OFFICER or committee == identity.committee_id)),
        None,
    )


def _restore_person_from_url() -> None:
    """
    A page reload starts a new session, which in the sandbox means back to the
    Treasurer. The Appearance switch reloads on purpose (ui/appearance.py), so
    it puts the current person in the URL as `?as=Membership VP`; this reads it
    once and drops it. Sandbox only: in production the identity never comes
    from the client.
    """
    try:
        chosen = st.query_params.get("as")
    except Exception:  # noqa: BLE001 - no query params outside a page run
        return
    if not chosen:
        return
    person = next((p for p in sandbox_people() if p[0] == chosen), None)
    if person is not None:
        _label, role, committee_id = person
        st.session_state[SESSION_KEY] = Identity(email=current_user().email, role=role, committee_id=committee_id)
    try:
        del st.query_params["as"]
    except Exception:  # noqa: BLE001
        pass


def _apply_sandbox_person() -> None:
    chosen = st.session_state.get("ais_signin_picker")
    person = next((p for p in sandbox_people() if p[0] == chosen), None)
    if person is None:
        return
    _label, role, committee_id = person
    set_identity(Identity(email=current_user().email, role=role, committee_id=committee_id))

