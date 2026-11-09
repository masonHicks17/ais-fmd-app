"""
HTML whose items can be clicked (2026-11-09).

`st.html` draws markup but can't tell Python what was clicked, and a plain
link would reload the page into a new session. This mounts the same markup
through an inline component (`st.components.v2`, no iframe, app styles apply)
and reports the `data-pick` value of whatever element the person clicks or
presses Enter on.

Each click is stamped (`value|time`) and a stamp is answered once: the
component can hand the same trigger back on a later full rerun -- closing a
dialog reruns the page -- and that must not reopen what was just closed.

Used by the Events calendar: each chip carries its event id, and a click opens
that event's panel.
"""

from __future__ import annotations

import streamlit as st

_JS = """
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let root = parentElement.querySelector(':scope > .ais-clickable');
  if (!root) {
    root = document.createElement('div');
    root.className = 'ais-clickable';
    parentElement.appendChild(root);
  }
  if (root.dataset.html !== data) {
    root.innerHTML = data;
    root.dataset.html = data;
  }
  const pick = (target) => {
    const item = target.closest('[data-pick]');
    if (item && root.contains(item)) { setTriggerValue('picked', item.getAttribute('data-pick') + '|' + Date.now()); }
  };
  root.onclick = (event) => pick(event.target);
  root.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); pick(event.target); }
  };
}
"""

try:
    _component = st.components.v2.component("ais_clickable_html", js=_JS, isolate_styles=False)
except Exception:  # noqa: BLE001 - very old Streamlit: fall back to plain HTML
    _component = None


def html(markup: str, *, key: str) -> str | None:
    """Draw `markup`; return the `data-pick` value clicked this run, if any."""
    if _component is None:
        st.html(markup)
        return None
    try:
        result = _component(data=markup, key=key, on_picked_change=lambda: None)
    except Exception:  # noqa: BLE001 - e.g. a test runner without components
        st.html(markup)
        return None
    picked = getattr(result, "picked", None)
    if picked in (None, ""):
        return None
    seen_key = f"_clickable_seen_{key}"
    if st.session_state.get(seen_key) == picked:
        return None
    st.session_state[seen_key] = picked
    return str(picked).rsplit("|", 1)[0]
