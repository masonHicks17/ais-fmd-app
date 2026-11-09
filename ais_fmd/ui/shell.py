"""
Page shell: header, banner, metrics, empty states.

FINDING (consistency). Every page in the original invented its own introduction,
its own filter placement, and its own way of saying "no data". One shell means
a new page is consistent by default rather than by discipline.

FINDING (performance). `animated_typing_title` slept 20ms per character and
every page called it at the top. Streamlit re-runs the whole script on every
widget interaction, so each filter change cost about half a second of
deliberate delay plus one markdown re-render per character. It was first cut
down to once per session per title, and is now gone entirely: a title that
types itself is the single loudest tell that a piece of software was generated
rather than designed, and it bought nothing that the CSS entrance animation in
`theme` does not do in 400ms without blocking the script.

PEBBLE. Every header, card and metric fades and rises once via CSS
(ui/theme.py). Helpers added for it:

  card(name, order=)    a white rounded card; order sets the entrance delay
  heading(title, right) the title row inside a card
  legend(items)         dot-legend HTML for a heading
  html(markup)          inline HTML with dollar signs made LaTeX-safe
  hub(key, views)       several existing view files under one nav entry
  chart(..., reveal=)   charts that wipe in once on first render
  metric(..., bars=)    same-day values for recent terms, drawn in the tile
"""

from __future__ import annotations

import re
import runpy
from urllib.parse import quote
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import auth, settings
from ..domain.money import format_currency, format_delta
from . import appearance, theme

_HUB_PENDING_KEY = "_ais_hub_pending"
_VIEWS_DIR = Path(__file__).resolve().parent.parent / "views"


def bootstrap(page_title: str = "UF AIS Financial Management") -> None:
    """Call once at the top of the entry point."""
    st.set_page_config(
        page_title=page_title,
        # A Material glyph rather than an emoji. The notebook emoji rendered as
        # whatever each OS happened to draw -- a different picture on Windows,
        # macOS and Android -- and reads as a placeholder either way.
        page_icon=":material/account_balance:",
        layout="wide",
        # Pebble navigates from the top bar, and the profile menu took over
        # what the sidebar held. Nothing renders into the sidebar any more.
        initial_sidebar_state="collapsed",
    )
    theme.register()
    st.markdown(theme.app_css(), unsafe_allow_html=True)


def environment_banner() -> None:
    """Impossible to mistake the sandbox for the real system."""
    sandbox = settings.is_sandbox()
    label = "SANDBOX" if sandbox else "PRODUCTION"
    css_class = "ais-banner" if sandbox else "ais-banner ais-banner-prod"
    st.markdown(
        f'<div class="{css_class}"><b>{label}</b> — {settings.banner_text()}</div>',
        unsafe_allow_html=True,
    )


def page_header(title: str, subtitle: str = "") -> None:
    """
    Title and one line of context.

    When the page is running inside a hub, the hub's view switcher renders
    here, beside the title, so every merged page gets it without changes.
    """
    pending = st.session_state.pop(_HUB_PENDING_KEY, None)
    # Two views fit beside the title. Three or more get their own row under it:
    # beside the title, the pills scrolled the first view's label out of sight
    # at laptop widths.
    if pending and len(pending[1]) > 2:
        title_column, switch_column = st.container(), st.container()
    elif pending:
        title_column, switch_column = st.columns([3, 2], vertical_alignment="bottom")
    else:
        title_column, switch_column = st.container(), None
    with title_column:
        body = f"<h1>{title}</h1>" + (f"<p>{subtitle}</p>" if subtitle else "")
        html(f'<div class="ais-head">{body}</div>')
    if pending and switch_column is not None:
        key, labels = pending
        with switch_column:
            st.segmented_control(
                "View", labels, key=key, selection_mode="single", label_visibility="collapsed"
            )
    st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)


def section_header(title: str, subtitle: str = "", *, anchor: str | None = None) -> None:
    """A second title further down a page, e.g. Home's "This term in detail"."""
    id_attr = f' id="{anchor}"' if anchor else ""
    body = f"<h2>{title}</h2>" + (f"<p>{subtitle}</p>" if subtitle else "")
    html(f'<div class="ais-section"{id_attr}>{body}</div>')


def html(markup: str) -> None:
    """
    Render inline HTML. Dollar signs become `&#36;`, which the browser shows
    as `$` but Streamlit's math parser never sees -- the HTML-safe form of
    `money_safe`.
    """
    st.markdown(markup.replace("$", "&#36;"), unsafe_allow_html=True)


def card(name: str, *, order: int = 1, hero: bool = False):
    """
    A Pebble card: white, 24px radius, soft shadow. `hero=True` fills it orange
    -- use once per page, for the figure that matters most. `order` (1-9) is
    the reading-order position; cards rise in that sequence.

        with shell.card("runway", order=1):
            ...
    """
    prefix = "hero" if hero else "card"
    return st.container(key=f"{prefix}-o{min(max(int(order), 1), 9)}-{name}")


def heading(title: str, right: str = "") -> None:
    """Card title row, with optional right-aligned HTML (a pill, a legend)."""
    html(f'<div class="ais-card-head"><span>{title}</span>{right}</div>')


def legend(items: list[tuple[str, str]]) -> str:
    """Dot legend HTML for `heading(right=...)`. items: (label, colour)."""
    dots = "".join(f'<span><i style="background:{color}"></i>{label}</span>' for label, color in items)
    return f'<span class="ais-legend">{dots}</span>'


def hub(key: str, views: list[tuple[str, str, "auth.Role"]]) -> None:
    """
    Several existing pages under one navigation entry.

    CONSOLIDATION. Sixteen nav entries came down to eleven without rewriting
    the merged pages: each stays its own file, owned by whoever owned it, and
    the hub runs exactly one of them per rerun. Running one (rather than all
    of them inside `st.tabs`) matters because the views call `st.stop()` for
    empty states -- inside tabs, the first view's empty state would blank
    every later tab.

    `views` is (label, file name in ais_fmd/views, minimum role). Views the
    user cannot open are left out of the switcher; if none are left, the
    strictest role's gate renders its usual warning.
    """
    identity = auth.current_user()
    allowed = [view for view in views if identity.can(view[2])]
    if not allowed:
        auth.require(max(view[2] for view in views))
        return
    labels = [label for label, _, _ in allowed]
    if st.session_state.get(key) not in labels:
        st.session_state[key] = labels[0]
    choice = st.session_state[key]
    st.session_state[_HUB_PENDING_KEY] = (key, labels) if len(labels) > 1 else None
    file_name = next(file for label, file, _ in allowed if label == choice)
    runpy.run_path(str(_VIEWS_DIR / file_name), run_name="__main__")


def money_safe(text: str) -> str:
    """
    Escape dollar signs for Streamlit markdown.

    Streamlit treats `$...$` as LaTeX math delimiters, so a sentence containing
    two currency figures -- which is most sentences in this app -- silently
    renders the text between them as an equation. Every markdown surface that
    can carry a formatted amount routes through here.
    """
    return re.sub(r"\$", r"\\$", text)


def say(text: str, *, caption: bool = False) -> None:
    """Render prose that may contain currency, without LaTeX mangling it."""
    if caption:
        st.caption(money_safe(text))
    else:
        st.markdown(money_safe(text))


def empty_state(headline: str, detail: str = "") -> None:
    st.markdown(
        f'<div class="ais-empty"><strong>{headline}</strong>{detail}</div>',
        unsafe_allow_html=True,
    )


def error_state(headline: str, detail: str = "") -> None:
    """
    An error that explains itself.

    FINDING F8's UI half: the original swallowed categorization failures
    entirely, so the treasurer had no reason to retry. Failures get a visible,
    specific message.
    """
    body = f"**{headline}**\n\n{detail}" if detail else f"**{headline}**"
    st.error(money_safe(body))


def notify(kind: str, text: str) -> None:
    """success / warning / info that is safe to contain currency."""
    {"success": st.success, "warning": st.warning, "info": st.info}[kind](money_safe(text))


def pace_rows(table: pd.DataFrame, fraction: float, *, rows: int = 3) -> str:
    """
    "Committees to watch" as progress rows (2026-11-09): the committee's name
    and its share of budget used on one line, a bar beneath, and a tick on the
    bar at how far the term has gone. Replaced a dot chart whose tick and dot
    sat on top of each other in a narrow card.

    `table` is `pace.committee_pace(...)`: Committee_Name, Budget, Spent, Used,
    Runs out, Status. `fraction` is the share of the term elapsed (0-1).
    """
    from html import escape

    t = theme.tokens()
    mark = max(0.0, min(fraction * 100, 100.0))
    out = []
    for row in table.head(rows).itertuples():
        used = float(row.Used)
        color = t.STATUS_COLORS.get(str(row.Status), t.ON_TRACK)
        detail = f"{format_currency(float(row.Spent))} of {format_currency(float(row.Budget))}"
        runs_out = getattr(row, "_7", None) if not hasattr(row, "Runs_out") else None
        runs_out = row[7] if runs_out is None and len(row) > 7 else runs_out
        if runs_out is not None and not pd.isna(runs_out) and used < 100:
            detail += f" · runs out {pd.Timestamp(runs_out):%b %d}"
        if used >= 100:
            detail += " · over budget"
        out.append(
            '<div class="ais-pace-row">'
            f'<div class="ais-pace-head"><span>{escape(str(row.Committee_Name))}</span><b>{used:.0f}%</b></div>'
            f'<div class="ais-meter-track"><div class="ais-meter-fill" style="width:{min(used, 100):.1f}%;background:{color}"></div>'
            f'<div class="ais-meter-mark" style="left:{mark:.1f}%"></div></div>'
            f'<small>{detail}</small></div>'
        )
    return '<div class="ais-pace-rows">' + "".join(out) + "</div>"


def pill(label: str, kind: str = "muted") -> str:
    mapping = {
        "over": "ais-over",
        "approaching": "ais-approaching",
        "on track": "ais-ontrack",
        "warn": "ais-warn",
    }
    css = mapping.get(kind, "ais-muted")
    return f'<span class="ais-pill {css}">{label}</span>'


def metric(
    container,
    label: str,
    value: object,
    *,
    delta: object = None,
    inverse: bool = False,
    currency: bool = True,
    trend: list[float] | None = None,
    color: str | None = None,
    bars: list[float] | None = None,
    bar_labels: list[str] | None = None,
    bar_hover: list[str] | None = None,
    bar_note: str | None = None,
    tone: str | None = None,
) -> None:
    """
    One KPI. `bar_labels` captions each mini bar ("F24"), `bar_hover` is each
    bar's full reading, `bar_note` one line under the bars saying what they are.

    `tone` ("ok", "warn", "over") tints the tile and colours its value by status.

    FINDING F14: the delta is preformatted, so it no longer renders as a bare
    1234.56 beneath a value formatted as $1,234.56.

    FINDING F11: `inverse=True` on expenses, so spending more than last semester
    reads as negative rather than being coloured as an improvement.

    PEBBLE: `bars` draws the same-day-of-term value for recent terms as a
    small bar chart under the figure: past terms faded, this term solid in
    `color` (ink by default; pass white inside a hero card).

    Not `st.metric(chart_data=...)`, though Streamlit 1.62 has it: its bars
    take the delta's red/green and sit on a filled panel, and cannot be given
    per-bar colours, so they could not follow "past terms grey, this term ink".
    """
    t = theme.tokens()
    # A string is already formatted -- a percentage, a ratio, a count with a
    # unit. Passing one through `f"{value:,}"` raises, so a caller with anything
    # that is not a bare number had no way to use this at all.
    if tone:
        slug = "".join(ch if ch.isalnum() else "-" for ch in label.lower())
        container = container.container(key=f"tone-{tone}-{slug}")

    if currency:
        display_value = format_currency(value)
    elif isinstance(value, str):
        display_value = value
    else:
        display_value = f"{value:,}"

    if currency:
        display_delta = format_delta(delta)
    elif isinstance(delta, str):
        display_delta = delta
    else:
        display_delta = f"{delta:+,}" if delta else None
    container.metric(
        label=label,
        value=display_value,
        delta=display_delta,
        delta_color="inverse" if inverse else "normal",
    )
    if bars:
        trend = [float(v or 0) for v in bars]
    if trend:
        from . import charts

        # FINDING (visual). The sparkline took the brand accent for an ordinary
        # metric and the over-budget red for any `inverse` one, so the income
        # trend was drawn in UF orange -- the app's expense colour -- and the
        # expense trend in the colour that means "over budget" everywhere else.
        # Two of the four tiles were saying something false. A caller that
        # knows what the figure means passes `color`; the old behaviour stays
        # as the fallback for callers that do not.
        spark_color = color or t.INK
        container.plotly_chart(
            _transparent(charts.sparkline(trend, color=spark_color, labels=bar_labels, hover=bar_hover)),
            width="stretch",
            theme=None,
            config={"displayModeBar": False},
            key=f"spark_{label}",
        )
        if bar_note:
            container.caption(bar_note)


def metric_row(specs: Iterable[dict]) -> None:
    """Lay out KPI tiles. Falls back to a single column on narrow screens."""
    specs = list(specs)
    if not specs:
        return
    columns = st.columns(len(specs))
    for column, spec in zip(columns, specs):
        metric(
            column,
            spec["label"],
            spec["value"],
            delta=spec.get("delta"),
            inverse=spec.get("inverse", False),
            currency=spec.get("currency", True),
            trend=spec.get("trend"),
            color=spec.get("color"),
            bars=spec.get("bars"),
            bar_labels=spec.get("bar_labels"),
            bar_hover=spec.get("bar_hover"),
            bar_note=spec.get("bar_note"),
            tone=spec.get("tone"),
        )


def linked_slider(
    label: str,
    *,
    min_value,
    max_value,
    value,
    step,
    key: str,
    help: str | None = None,
    format: str | None = None,
):
    """
    A slider paired with a number input for exact entry, kept in sync.

    Dragging a slider is fast but imprecise; typing a number is precise but
    slow for exploring a range. Pairing them costs one extra widget and gives
    both -- and it bounds the value to `[min_value, max_value]` either way,
    which a bare `st.number_input` with no `max_value` does not: that gap is
    what let "expected members" on the Dues and Planner pages be typed or
    computed into values with no relationship to a real roster size.

    `key` must be unique per call on a page, same as any Streamlit widget key.
    Int-typed `min_value`/`max_value`/`value`/`step` produce an int slider;
    float-typed ones produce a float slider -- same type inference Streamlit's
    own `st.slider` uses, since this delegates to it.

    Syncing works by writing the *other* widget's session-state key directly
    from each one's `on_change` callback -- passing `value=` again on a later
    rerun would not do it, since Streamlit ignores `value` for a widget whose
    key already holds state and uses only what's in `st.session_state` for
    that key. Writing both keys in the callback is what makes the second
    widget actually move.

    `value=` is passed to each widget **only on the render where its key does
    not exist yet** (the very first one). Passing it on every render, even
    with the correct value, trips Streamlit's own policy check -- it logs
    "created with a default value but also had its value set via the Session
    State API" and warns that a future version may turn this into a hard
    error. The check exists for exactly this shape of bug, so the fix is to
    stop doing the thing it's warning about, not to ignore the warning.
    """
    state_key = f"_linked_{key}"
    slider_key = f"{key}_slider"
    number_key = f"{key}_number"

    if state_key not in st.session_state:
        st.session_state[state_key] = value

    def _sync(source_key: str) -> None:
        new_value = st.session_state[source_key]
        st.session_state[state_key] = new_value
        st.session_state[slider_key] = new_value
        st.session_state[number_key] = new_value

    slider_col, number_col = st.columns([4, 1])
    with slider_col:
        st.slider(
            label,
            min_value=min_value,
            max_value=max_value,
            step=step,
            format=format,
            help=help,
            key=slider_key,
            on_change=_sync,
            args=(slider_key,),
            **({"value": st.session_state[state_key]} if slider_key not in st.session_state else {}),
        )
    with number_col:
        st.number_input(
            label,
            min_value=min_value,
            max_value=max_value,
            step=step,
            key=number_key,
            label_visibility="collapsed",
            on_change=_sync,
            args=(number_key,),
            **({"value": st.session_state[state_key]} if number_key not in st.session_state else {}),
        )
    return st.session_state[state_key]


def _transparent(figure: go.Figure) -> go.Figure:
    """
    Force the figure's own grounds to transparent.

    FINDING (visual). The page draws a blueprint ruling across the whole main
    area and every chart is meant to sit *on* it. The registered template asks
    for transparent grounds and serialises correctly -- but Streamlit writes
    `paper_bgcolor` and `plot_bgcolor` from `.streamlit/config.toml` onto the
    figure anyway, and it does so even when `theme=None` says to leave the
    figure alone. Measured on every chart on the dashboard: `plot_bgcolor`
    arrived as #0A0C0F, so each chart punched an opaque rectangle through the
    ruling and the drawing surface was interrupted seven times on one page.

    Setting the values on the figure's own layout, rather than only on the
    template, is what survives that merge.

    The template is attached here too, per figure. Pebble has one palette, so
    `theme.register()` also installs it as the Plotly default; attaching it
    per figure keeps charts right even where that default was not set.
    """
    return figure.update_layout(
        template=theme.build_template(),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )


def chart(figure: go.Figure, *, key: str | None = None, reveal: str | None = None) -> None:
    """
    Render a Plotly figure with consistent options.

    FINDING (visual). `st.plotly_chart` defaults to `theme="streamlit"`, which
    layers Streamlit's own Plotly styling *over* whatever template the figure
    carries. The app registers a template in `theme.build_template` and sets it
    as the Plotly default, but Streamlit was overriding parts of it at render
    time -- axis tick labels came out at #E6EAF1 instead of the muted token, so
    every axis in the app was brighter than its data. Passing `theme=None`
    hands the figure back to its own template, which is the whole point of
    registering one.

    PEBBLE: `reveal="left"` wipes the chart in from the left (lines drawing
    in); `reveal="up"` grows it from the base (bars). Plays once, on first
    render. The mode bar is hidden.
    """
    def _draw() -> None:
        st.plotly_chart(
            _transparent(figure),
            width="stretch",
            theme=None,
            config={"displayModeBar": False, "displaylogo": False},
            key=key,
        )

    if reveal in {"left", "up"}:
        with st.container(key=f"reveal-{reveal}-{key or id(figure)}"):
            _draw()
    else:
        _draw()


def dataframe(df: pd.DataFrame, **kwargs) -> None:
    """Render a table. Wide tables scroll inside their own container."""
    kwargs.setdefault("hide_index", True)
    st.dataframe(df, width="stretch", **kwargs)


# REMOVED (Pebble): `mode_switch()` and its localStorage writer. Pebble is a
# single light palette, so there is no second mode to switch to.


def appearance_sync(pages: Iterable, current_identity_label: str | None = None) -> None:
    """
    Keep the browser's theme choice in step with the Appearance control. Call
    once per run, after `st.navigation`, with every registered st.Page: the
    choice is written for each page's path. In the sandbox the identity label
    rides along in the reload URL so a theme change doesn't sign anyone out.
    """
    paths = [getattr(page, "url_path", "") or "" for page in pages]
    query = f"as={quote(current_identity_label)}" if current_identity_label else None
    appearance.sync(paths, reload_query=query)


def profile_menu(links: Iterable, sandbox_controls=None) -> None:
    """
    The profile button at the top right, which replaced the sidebar (2026-11-08).

    Opens a menu with who is signed in, links to the pages kept out of the top
    bar (`links`: st.Page objects the person may open), the sandbox role
    switcher and `sandbox_controls` (a callable), and Sign out.

    Rendered into the page body and pinned over the header's right side by the
    `st-key-ais-profile` rule in ui/theme.py: Streamlit has no slot in its
    header for app widgets.
    """
    identity = st.session_state.get(auth.SESSION_KEY)
    role_label = identity.role.label if identity is not None else "Account"
    with st.container(key="ais-profile"):
        with st.popover(role_label, icon=":material/account_circle:"):
            if identity is not None:
                committee = ""
                if identity.committee_id is not None:
                    from ..config.categories import committee_name

                    committee = f" · {committee_name(identity.committee_id)}"
                html(
                    '<div class="ais-profile-who">'
                    f"<b>{identity.email}</b><span>{role_label}{committee}</span></div>"
                )

            links = list(links)
            if links:
                st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
                for page in links:
                    st.page_link(page, width="stretch")

            # Light or dark (2026-11-09). Streamlit follows the device unless a
            # choice is made here; see ui/appearance.py for how the choice lands.
            st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
            appearance.control()

            if settings.is_sandbox() and not settings.sandbox_login_enabled():
                st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
                auth.role_switcher()
                if sandbox_controls is not None:
                    sandbox_controls()

            # Sign out, in production only. `auth.sign_out()` was written correct and
            # called by nothing, which meant the app had no way to end a session at all:
            # once an identity was in st.session_state it stayed until the browser
            # session did. On a shared treasury laptop the next person to open the tab
            # was signed in as ADMIN.
            #
            # Absent in the open sandbox on purpose -- there the identity is fabricated
            # and the role switcher above is how you change it.
            if identity is not None and (
                not settings.is_sandbox() or settings.sandbox_login_enabled()
            ):
                st.markdown('<hr class="ais-rule" />', unsafe_allow_html=True)
                if st.button(
                    "Sign out", icon=":material/logout:", width="stretch", key="_ais_sign_out"
                ):
                    auth.sign_out()
                    st.rerun()
