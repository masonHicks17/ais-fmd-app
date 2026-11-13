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

Calendar gestures (2026-11-13), for markup that opts in with attributes:
  * double-click an element with `data-day` (not on a chip) -> `add:<day>`
  * drag an element with `data-drag` (and `data-from`, its day) onto another
    `data-day` -> `move:<data-drag>:<day>`
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
  const send = (value) => setTriggerValue('picked', value + '|' + Date.now());
  root.onclick = (event) => pick(event.target);
  root.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); pick(event.target); }
  };
  root.ondblclick = (event) => {
    if (event.target.closest('[data-pick]')) { return; }
    const day = event.target.closest('[data-day]');
    if (day && root.contains(day)) { send('add:' + day.dataset.day); }
  };
  const clearDrop = () => root.querySelectorAll('.drop').forEach((el) => el.classList.remove('drop'));
  root.ondragstart = (event) => {
    const chip = event.target.closest && event.target.closest('[data-drag]');
    if (!chip) { return; }
    event.dataTransfer.setData('text/plain', chip.dataset.drag + '@' + (chip.dataset.from || ''));
    event.dataTransfer.effectAllowed = 'move';
    chip.classList.add('dragging');
  };
  root.ondragend = (event) => {
    clearDrop();
    root.querySelectorAll('.dragging').forEach((el) => el.classList.remove('dragging'));
  };
  root.ondragover = (event) => {
    const day = event.target.closest('[data-day]');
    if (!day) { return; }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    if (!day.classList.contains('drop')) { clearDrop(); day.classList.add('drop'); }
  };
  root.ondragleave = (event) => {
    const day = event.target.closest('[data-day]');
    if (day && !day.contains(event.relatedTarget)) { day.classList.remove('drop'); }
  };
  root.ondrop = (event) => {
    const day = event.target.closest('[data-day]');
    clearDrop();
    if (!day) { return; }
    event.preventDefault();
    const [id, from] = (event.dataTransfer.getData('text/plain') || '').split('@');
    if (id && day.dataset.day !== from) { send('move:' + id + ':' + day.dataset.day); }
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
