"""
Appearance: System, Light or Dark (2026-11-09).

Streamlit decides the theme in the browser -- from the device setting, or from
a choice it keeps in localStorage, one key per page path -- and tells Python
which is on through `st.context.theme.type`. Python cannot set it. So the
switch is a tiny component (`appearance/index.html`) that writes Streamlit's
keys for every page and reloads; `control()` is the segmented control in the
profile menu that drives it.

A reload starts a new Streamlit session. Google sign-in survives that (its
cookie does); the sandbox identity would not, so the caller passes
`reload_query` ("as=Membership VP"), it goes on the reload URL, and
`auth.current_user` reads it back.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

CHOICES = ("System", "Light", "Dark")
STATE_KEY = "ais_appearance"            # the control's value
_STORED_KEY = "ais_appearance_stored"   # what the browser reported
_WANT_KEY = "_ais_appearance_want"      # a choice just made, not yet sent
_TOUCHED_KEY = "_ais_appearance_touched"

_component = components.declare_component("appearance", path=str(Path(__file__).parent / "appearance"))


def sync(paths: list[str], *, reload_query: str | None = None) -> str:
    """
    Run the helper frame. Returns the stored choice ("System" until the browser
    has answered). `paths` are every page's URL path, so a choice applies to all
    of them at once rather than page by page.
    """
    want = st.session_state.pop(_WANT_KEY, None)
    try:
        reported = _component(paths=paths, want=want, reload_query=reload_query, key="ais_appearance_frame", default=None)
    except Exception:  # noqa: BLE001 - component unavailable (tests): behave as System
        reported = None
    if reported in CHOICES:
        st.session_state[_STORED_KEY] = reported
        # The control shows what the browser has, until the person touches it.
        if not st.session_state.get(_TOUCHED_KEY):
            st.session_state[STATE_KEY] = reported
    return st.session_state.get(_STORED_KEY, "System")


def _changed() -> None:
    st.session_state[_TOUCHED_KEY] = True
    st.session_state[_WANT_KEY] = st.session_state.get(STATE_KEY) or "System"


def control() -> None:
    """The System / Light / Dark control, for the profile menu."""
    st.session_state.setdefault(STATE_KEY, st.session_state.get(_STORED_KEY, "System"))
    st.segmented_control(
        "Appearance", list(CHOICES), key=STATE_KEY, selection_mode="single", on_change=_changed,
        help="System follows your device. Changing this reloads the page.",
    )
