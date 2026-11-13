"""
Design tokens, the Plotly template, and the app stylesheet -- in light and dark.

PEBBLE. Soft cards on a quiet canvas. Ink carries the structure and one orange
marks the figure that matters most on each screen. Cards fade and rise in
reading order; charts reveal once, on first render.

Two palettes since 2026-11-09: `LIGHT` (the original Pebble) and `DARK`. Which
one a session gets follows Streamlit's own theme -- `.streamlit/config.toml`
defines a `[theme.light]` and a `[theme.dark]`, Streamlit picks by the device
setting or the person's choice, and `st.context.theme.type` tells this module
which is on. Everything that draws colour asks `tokens()` at draw time, so two
people on different themes never share a value. The module-level constants
(`INK`, `ACCENT`, ...) are the light tokens, kept for the name-level API and
for code that runs outside a Streamlit session; a test refuses their use in
views and ui modules.

  INCOME      ink        money in reads as the primary series
  EXPENSE     grey       money out sits behind it
  OVER        orange     the one state that needs attention
  APPROACHING ink        80%+ of budget used
  ON_TRACK    grey       nothing to do

Semantic colour is still separate from the accent: ACCENT identifies the
product, the status colours identify meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import plotly.graph_objects as go
import plotly.io as pio

# --- Tokens ------------------------------------------------------------------


@dataclass(frozen=True)
class Tokens:
    """One palette. Field names match the module constants they replace."""

    name: str
    CANVAS: str
    CARD: str
    SUBTLE: str
    ACCENT: str          # AIS orange
    ACCENT_DIM: str      # orange text on tint
    ACCENT_TINT: str
    POSITIVE: str        # delta pills only
    POSITIVE_TINT: str
    WARNING: str         # approaching: amber, so it reads apart from "over"
    WARNING_TINT: str
    INK: str
    INK_SOFT: str
    INK_MUTE: str
    GRID: str
    LINE_EDGE: str       # a grey dark enough to read as a bar against the canvas
    ON_INK: str          # text on an ink-coloured surface (the active nav pill)
    EXPENSE: str
    BUDGET: str
    ON_TRACK: str
    UNBUDGETED: str
    SHADOW: str
    SHADOW_HOVER: str
    HISTORY: tuple[str, ...]      # past terms in comparison charts, newest first
    CATEGORICAL: tuple[str, ...]  # ordered palette: ink, orange, then greys
    SEGMENTS: tuple[str, ...]     # Pareto segments, biggest first

    # Semantic -- these encode meaning, not brand.
    @property
    def INCOME(self) -> str:
        return self.INK

    @property
    def OVER(self) -> str:
        return self.ACCENT

    @property
    def APPROACHING(self) -> str:
        return self.INK

    @property
    def SURFACE(self) -> str:
        return self.CARD

    @property
    def STATUS_COLORS(self) -> dict[str, str]:
        return {
            "over": self.OVER,
            "approaching": self.APPROACHING,
            "on track": self.ON_TRACK,
            "unbudgeted": self.UNBUDGETED,
            "no budget": self.UNBUDGETED,
        }

    @property
    def dark(self) -> bool:
        return self.name == "dark"


LIGHT = Tokens(
    name="light",
    CANVAS="#F5F5F3",
    CARD="#FFFFFF",
    SUBTLE="#F3F3F0",
    ACCENT="#D6400E",
    ACCENT_DIM="#A8320B",
    ACCENT_TINT="#FFE9E0",
    POSITIVE="#157F46",
    POSITIVE_TINT="#E3F3EA",
    WARNING="#A15C07",
    WARNING_TINT="#FDF0D8",
    INK="#111111",
    INK_SOFT="#55565A",
    INK_MUTE="#86878B",
    GRID="#ECECE9",
    LINE_EDGE="#BDBDB7",
    ON_INK="#FFFFFF",
    EXPENSE="#9A9B9F",
    BUDGET="#ECECE9",
    ON_TRACK="#BDBDB7",
    UNBUDGETED="#D9D9D4",
    SHADOW="0 1px 2px rgba(17,17,17,.04), 0 8px 24px rgba(17,17,17,.05)",
    SHADOW_HOVER="0 2px 4px rgba(17,17,17,.05), 0 18px 40px rgba(17,17,17,.10)",
    HISTORY=("#9A9B9F", "#BDBDB7", "#D9D9D4"),
    CATEGORICAL=(
        "#111111", "#D6400E", "#86878B", "#BDBDB7", "#A8320B",
        "#55565A", "#F08A5D", "#D9D9D4", "#2A2A2A", "#FFB59A",
    ),
    SEGMENTS=("#111111", "#3A3B3E", "#5F6064", "#86878B", "#A9AAAD", "#C9C9C4"),
)

# Dark keeps the same roles: a warm near-black canvas, cards a step lighter,
# pale ink. The orange is lifted so it still carries on a dark ground, and the
# tints are deep washes of their colour so a toned tile reads as a tint, not a
# light patch.
DARK = Tokens(
    name="dark",
    CANVAS="#161615",
    CARD="#1F1F1E",
    SUBTLE="#2A2A28",
    ACCENT="#F0552A",
    ACCENT_DIM="#FF9A74",
    ACCENT_TINT="#3A2017",
    POSITIVE="#5CCB86",
    POSITIVE_TINT="#17301F",
    WARNING="#E8AC4A",
    WARNING_TINT="#352912",
    INK="#F2F2EF",
    INK_SOFT="#B9BAB6",
    INK_MUTE="#8C8D90",
    GRID="#333331",
    LINE_EDGE="#5C5D5A",
    ON_INK="#161615",
    EXPENSE="#8C8D90",
    BUDGET="#333331",
    ON_TRACK="#5C5D5A",
    UNBUDGETED="#3E3E3C",
    SHADOW="0 1px 2px rgba(0,0,0,.30), 0 8px 24px rgba(0,0,0,.35)",
    SHADOW_HOVER="0 2px 4px rgba(0,0,0,.35), 0 18px 40px rgba(0,0,0,.50)",
    HISTORY=("#8C8D90", "#5C5D5A", "#3E3E3C"),
    CATEGORICAL=(
        "#F2F2EF", "#F0552A", "#9A9B9F", "#6B6C6A", "#FF9A74",
        "#C5C6C2", "#F08A5D", "#4A4A48", "#DADAD6", "#FFB59A",
    ),
    SEGMENTS=("#F2F2EF", "#C5C6C2", "#9A9B9F", "#7A7B7E", "#5C5D5A", "#45453F"),
)

# The light tokens by their old names, for code that runs outside a session
# (report builders, tests) and for callers that only need the name-level API.
CANVAS, CARD, SUBTLE = LIGHT.CANVAS, LIGHT.CARD, LIGHT.SUBTLE
ACCENT, ACCENT_DIM, ACCENT_TINT = LIGHT.ACCENT, LIGHT.ACCENT_DIM, LIGHT.ACCENT_TINT
POSITIVE, POSITIVE_TINT = LIGHT.POSITIVE, LIGHT.POSITIVE_TINT
WARNING, WARNING_TINT = LIGHT.WARNING, LIGHT.WARNING_TINT
INCOME, EXPENSE, BUDGET = LIGHT.INCOME, LIGHT.EXPENSE, LIGHT.BUDGET
OVER, APPROACHING, ON_TRACK, UNBUDGETED = LIGHT.OVER, LIGHT.APPROACHING, LIGHT.ON_TRACK, LIGHT.UNBUDGETED
STATUS_COLORS = LIGHT.STATUS_COLORS
INK, INK_SOFT, INK_MUTE, GRID, SURFACE = LIGHT.INK, LIGHT.INK_SOFT, LIGHT.INK_MUTE, LIGHT.GRID, LIGHT.CARD
HISTORY = list(LIGHT.HISTORY)
CATEGORICAL = list(LIGHT.CATEGORICAL)
SHADOW, SHADOW_HOVER = LIGHT.SHADOW, LIGHT.SHADOW_HOVER

TEMPLATE_NAME = "ais_fmd"
DARK_TEMPLATE_NAME = "ais_fmd_dark"

FONT = "Onest, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
_FONT = FONT  # kept for callers that imported the old private name
# Figures stay in Onest with tabular numerals; the old monospace face is retired.
_MONO = FONT

EASE = "cubic-bezier(.2,.8,.2,1)"


def is_dark() -> bool:
    """Whether this session's Streamlit theme is the dark one. False outside a session."""
    try:
        import streamlit as st

        return (st.context.theme.type or "light") == "dark"
    except Exception:  # noqa: BLE001 - no script context (tests, report builders)
        return False


def tokens() -> Tokens:
    """The palette for this session: ask at draw time, never at import."""
    return DARK if is_dark() else LIGHT


def build_template(palette: "Tokens | Palette | None" = None) -> go.layout.Template:
    """The Pebble Plotly template for `palette` (default: this session's tokens)."""
    t = palette if isinstance(palette, Tokens) else tokens()
    axis = dict(
        gridcolor=t.GRID,
        zerolinecolor=t.GRID,
        linecolor=t.GRID,
        showline=False,
        tickfont=dict(color=t.INK_MUTE, size=12),
        title=dict(font=dict(color=t.INK_MUTE, size=12)),
        automargin=True,
    )
    return go.layout.Template(
        layout=go.Layout(
            font=dict(family=FONT, size=13, color=t.INK_SOFT),
            title=dict(font=dict(size=16, color=t.INK), x=0, xanchor="left"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            colorway=list(t.CATEGORICAL),
            xaxis=dict(axis, showgrid=False),
            yaxis=axis,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=t.INK_SOFT, size=12),
                bgcolor="rgba(0,0,0,0)",
            ),
            hoverlabel=dict(
                bgcolor=t.CARD,
                bordercolor=t.GRID,
                font=dict(family=FONT, size=12, color=t.INK),
            ),
            barcornerradius=8,
            bargap=0.35,
            margin=dict(l=8, r=8, t=28, b=8),
            separators=".,",
        )
    )


def register() -> None:
    """
    Register both templates; the light one is the Plotly default. Safe to call
    repeatedly. The default is process-wide, so a dark session never relies on
    it: `shell._transparent` attaches the session's template to every figure.
    """
    pio.templates[TEMPLATE_NAME] = build_template(LIGHT)
    pio.templates[DARK_TEMPLATE_NAME] = build_template(DARK)
    pio.templates.default = TEMPLATE_NAME


# Card containers are keyed `card-o<N>-<name>` / `hero-o<N>-<name>` by
# shell.card(); N (1-9) sets the entrance delay so cards rise in reading order.
_STAGGER = "\n".join(
    f'  [class*="st-key-card-o{n}-"], [class*="st-key-hero-o{n}-"] '
    f"{{ animation-delay: {80 + (n - 1) * 70}ms; }}"
    for n in range(1, 10)
)


def app_css(palette: "Tokens | Palette | None" = None) -> str:
    """The stylesheet for `palette` (default: this session's tokens)."""
    t = palette if isinstance(palette, Tokens) else tokens()
    # The hero card keeps the brand orange in both modes: white text clears AA
    # on it (4.6:1), not on the lifted dark-mode orange (3.5:1).
    hero, on_accent = LIGHT.ACCENT, "#FFFFFF"
    return f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap');

  @keyframes ais-rise {{ from {{ opacity: 0; transform: translateY(16px); }} to {{ opacity: 1; transform: none; }} }}
  @keyframes ais-fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @keyframes ais-wipe-left {{ from {{ clip-path: inset(0 100% 0 0); }} to {{ clip-path: inset(0 0 0 0); }} }}
  @keyframes ais-wipe-up {{ from {{ clip-path: inset(100% 0 0 0); }} to {{ clip-path: inset(0 0 0 0); }} }}

  :root {{ color-scheme: {"dark" if t.dark else "light"}; }}

  /* Onest everywhere except icon glyphs and code. */
  .stApp, .stApp p, .stApp span, .stApp div, .stApp label, .stApp button,
  .stApp input, .stApp textarea, .stApp h1, .stApp h2, .stApp h3, .stApp h4 {{
    font-family: {FONT};
  }}
  .stApp code, .stApp pre {{ font-family: revert; }}
  /* Icon glyphs keep their own font: the nav icons (stIconMaterial), the
     alert icons (stAlertDynamicIcon -- st.info's lock showed as the word
     "lock") and our own spans by class. */
  .stApp [data-testid="stIconMaterial"], .stApp [data-testid$="DynamicIcon"],
  .stApp .material-symbols-rounded {{
    font-family: 'Material Symbols Rounded';
  }}

  .stApp {{ background: {t.CANVAS}; }}
  /* The top nav header is 60px tall and fixed, so content starts below it. */
  .block-container {{ padding-top: 5rem; padding-bottom: 4rem; max-width: 1440px; }}

  /* Top navigation: pill links; the active page is an ink pill.
     Selectors verified against Streamlit 1.62: each link is
     a[data-testid="stTopNavLink"] inside a stTopNavLinkContainer, and the
     icon and label spans carry their own inline colours, so the active
     state has to recolour the children, not just the link. The container's
     default padding is what pushed pages into the "N more" overflow. */
  header[data-testid="stHeader"] {{ background: {t.CANVAS}; }}
  header [data-testid="stTopNavLinkContainer"] {{ padding: 0 2px; }}
  header [data-testid="stTopNavLink"] {{
    border-radius: 999px; padding: 0 14px; height: 44px; display: inline-flex;
    align-items: center; color: {t.INK_SOFT}; font-weight: 500; transition: background .2s;
  }}
  header [data-testid="stTopNavLink"]:hover {{ background: {t.SUBTLE}; color: {t.INK}; }}
  header [data-testid="stTopNavLink"][aria-current="page"] {{ background: {t.INK}; color: {t.ON_INK}; }}
  header [data-testid="stTopNavLink"][aria-current="page"] * {{ color: {t.ON_INK} !important; }}

  /* Profile menu (shell.profile_menu): rendered in the page body, pinned over
     the right of the fixed header. toolbarMode is "minimal" in
     .streamlit/config.toml, which removes Streamlit's Deploy button and ⋮ menu,
     so nothing else sits there. */
  .st-key-ais-profile {{
    position: fixed; top: 8px; right: 16px; z-index: 999991; width: auto !important;
  }}
  /* Leave room for it: the nav moves pages into "N more" before reaching it.
     With no toolbar the header justifies the nav to the right; order and
     margin put it back on the left. */
  header [data-testid="stTopNav"] {{
    order: -1; margin-right: auto; max-width: calc(100vw - 180px);
  }}
  .stApp .st-key-ais-profile button {{
    border-radius: 999px; height: 44px; padding: 0 16px 0 10px;
    background: {t.CARD}; border: 1px solid {t.GRID}; color: {t.INK}; font-weight: 500;
  }}
  .st-key-ais-profile button:hover {{ background: {t.SUBTLE}; border-color: {t.GRID}; }}
  .st-key-ais-profile [data-testid="stIconMaterial"] {{ font-size: 1.5rem; }}
  /* The menu itself: narrow and tight. The body is portalled out of
     .st-key-ais-profile, so it is found by what it contains. */
  [data-testid="stPopoverBody"]:has(.ais-profile-who) {{
    width: 260px !important; min-width: 260px !important; padding: 12px 14px !important;
  }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) [data-testid="stVerticalBlock"] {{ gap: 6px; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) hr.ais-rule {{
    margin: 6px 0 !important; border-top: 1px solid {t.GRID} !important;
  }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) [data-testid="stPageLink"] a {{ padding: 4px 6px; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) label {{ min-height: 0; margin-bottom: 0; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) label p {{ font-size: .8rem; color: {t.INK_SOFT}; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) button {{ min-height: 34px; padding: 4px 10px; }}
  .ais-profile-who {{
    display: flex; flex-direction: column; gap: 0; padding: 0; line-height: 1.3;
    margin-bottom: 1rem;  /* offsets Streamlit's -1rem under markdown blocks */
  }}
  .ais-profile-who b {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .ais-profile-who b {{ color: {t.INK}; font-weight: 600; }}
  .ais-profile-who span {{ color: {t.INK_SOFT}; font-size: .85rem; }}

  /* Sandbox banner: a quiet pill, still impossible to miss. */
  .ais-banner {{
    display: inline-flex; align-items: center; gap: .5rem;
    background: {t.ACCENT_TINT}; color: {t.ACCENT_DIM};
    padding: .35rem .9rem; border-radius: 999px;
    font-size: .82rem; margin-bottom: .9rem;
  }}
  .ais-banner b {{ color: {t.ACCENT_DIM}; letter-spacing: .04em; font-weight: 600; }}
  .ais-banner-prod {{ background: {t.INK}; color: {t.ON_INK}; }}
  .ais-banner-prod b {{ color: {t.ON_INK}; }}

  /* Page header */
  .ais-head {{ margin-bottom: .2rem; animation: ais-rise .56s {EASE} both; }}
  .ais-head h1 {{
    font-size: 2.5rem; font-weight: 600; letter-spacing: -.035em; line-height: 1.05;
    margin: 0 0 .35rem; padding: 0; color: {t.INK};
  }}
  a.ais-todo-link {{
    color: {t.INK}; font-weight: 500; text-decoration: none;
    display: inline-flex; align-items: flex-start; gap: .55rem;
  }}
  a.ais-todo-link .material-symbols-rounded {{
    font-family: 'Material Symbols Rounded'; font-size: 1.2rem; line-height: 1.4;
  }}
  a.ais-todo-link:hover {{ text-decoration: underline; }}
  .ais-section {{ margin: 2.4rem 0 .2rem; scroll-margin-top: 80px; }}
  .ais-section h2 {{
    font-size: 1.6rem; font-weight: 600; letter-spacing: -.025em; line-height: 1.1;
    margin: 0 0 .3rem; padding: 0; color: {t.INK};
  }}
  .ais-section p {{ color: {t.INK_SOFT}; font-size: .95rem; margin: 0; }}
  .ais-head p {{ color: {t.INK_SOFT}; font-size: 1rem; margin: 0; max-width: 70ch; }}
  .ais-rule {{ height: 0; background: none; margin: 1.2rem 0; border: 0; }}

  /* "?" tooltips (shell.tip): the description behind a title, on hover or focus. */
  .ais-tip {{
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; margin-left: 8px; border-radius: 50%; vertical-align: middle;
    border: 1.5px solid {t.INK_MUTE}; color: {t.INK_SOFT}; font-size: 11px; font-weight: 700;
    letter-spacing: 0; line-height: 1; cursor: help; flex: none;
  }}
  h1 .ais-tip {{ width: 22px; height: 22px; font-size: 13px; margin-left: 12px; vertical-align: 0.35em; }}
  h2 .ais-tip {{ width: 20px; height: 20px; font-size: 12px; margin-left: 10px; vertical-align: 0.25em; }}
  .ais-tip:hover, .ais-tip:focus {{ border-color: {t.INK}; color: {t.INK}; outline: none; }}
  .ais-tip::after {{
    content: attr(data-tip); position: absolute; left: 50%; top: calc(100% + 8px); transform: translateX(-50%);
    width: max-content; max-width: 320px; padding: 9px 12px; border-radius: 12px;
    background: {t.INK}; color: {t.ON_INK}; font-size: 13px; font-weight: 400; line-height: 1.45;
    letter-spacing: 0; text-align: left; white-space: normal; box-shadow: {t.SHADOW_HOVER};
    opacity: 0; visibility: hidden; transition: opacity .15s ease; z-index: 1000; pointer-events: none;
  }}
  .ais-tip:hover::after, .ais-tip:focus::after {{ opacity: 1; visibility: visible; }}
  [class*="st-key-hero-"] .ais-tip {{ border-color: {on_accent}; }}
  [class*="st-key-hero-"] .ais-tip::after {{ background: {t.CARD}; color: {t.INK}; }}
  /* Cards may clip; let a tooltip out of its card. */
  [class*="st-key-card-"], [class*="st-key-hero-"], .ais-card-head, .ais-head, .ais-section {{ overflow: visible; }}
  /* Cards rise in and lift on hover, so each is its own stacking layer and a
     later card would paint over an open tooltip. While a tip is open, lift
     every block around it above its siblings. */
  .stApp :is([data-testid="stVerticalBlock"], [data-testid="stHorizontalBlock"], [data-testid="stColumn"],
    [data-testid="stElementContainer"], [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlockBorderWrapper"],
    [data-testid="stMarkdownContainer"], [data-testid="stHeading"], [class*="st-key-"], .ais-clickable)
    :is(:has(.ais-tip:hover), :has(.ais-tip:focus)) {{ position: relative; z-index: 1000; }}
  .stApp :is([data-testid="stVerticalBlock"], [data-testid="stHorizontalBlock"], [data-testid="stColumn"],
    [data-testid="stElementContainer"], [data-testid="stLayoutWrapper"], [data-testid="stVerticalBlockBorderWrapper"],
    [class*="st-key-"]):is(:has(.ais-tip:hover), :has(.ais-tip:focus)) {{ overflow: visible; }}

  /* Bigger targets (2026-11-13): buttons, view switchers and pickers were
     small to hit. Pickers are capped in width so a lone one doesn't run the
     width of the page. */
  .stApp .stButton > button:not([data-testid="stBaseButton-tertiary"]), .stApp .stDownloadButton > button,
  .stApp .stFormSubmitButton > button, .stMain [data-testid="stPopover"]:not(.st-key-ais-profile *) > div > button {{
    min-height: 44px; padding: 8px 20px; border-radius: 999px; font-size: 15px;
  }}
  .stApp [data-testid="stButtonGroup"] button {{ min-height: 44px; padding: 6px 20px; font-size: 15px; }}
  .stApp [data-testid="stSelectbox"] [data-baseweb="select"] > div {{ min-height: 46px; }}
  .stMain [data-testid="stSelectbox"] {{ max-width: 440px; }}
  [role="dialog"] [data-testid="stSelectbox"] {{ max-width: none; }}

  /* Home header: the term, once, with a slim progress bar. */
  .ais-term-bar {{
    display: inline-block; width: 120px; height: 6px; margin: 0 10px 0 12px; vertical-align: middle;
    background: {t.GRID}; border-radius: 999px; overflow: hidden;
  }}
  .ais-term-bar i {{ display: block; height: 100%; background: {t.ACCENT}; border-radius: 999px; }}

  /* Income − Expenses = Net, one card (term_analysis). */
  .st-key-net-figure [data-testid="stMetricValue"] {{ color: {t.ACCENT_DIM}; }}
  .ais-op {{ font-size: 2rem; font-weight: 300; color: {t.INK_MUTE}; text-align: center; line-height: 1; }}

  /* Dues summary card. */
  .ais-dues-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }}
  .ais-dues-head b {{ font-size: 1.9rem; font-weight: 600; letter-spacing: -.02em; color: {t.INK}; }}
  .ais-dues-head span {{ font-size: 1.9rem; font-weight: 600; color: {t.ACCENT_DIM}; font-variant-numeric: tabular-nums; }}
  .ais-dues-head span.muted {{ font-size: .95rem; font-weight: 400; color: {t.INK_SOFT}; }}
  .ais-dues-bar {{ height: 12px; border-radius: 999px; background: {t.GRID}; overflow: hidden; margin: 10px 0 18px; }}
  .ais-dues-bar i {{ display: block; height: 100%; background: {t.ACCENT}; border-radius: 999px; }}
  .ais-stat {{ display: flex; flex-direction: column; gap: 2px; padding-left: 18px; border-left: 1px solid {t.GRID}; }}
  .ais-stat.first {{ padding-left: 0; border-left: 0; }}
  .ais-stat span {{ font-size: 13.5px; color: {t.INK_SOFT}; display: inline-flex; align-items: center; }}
  .ais-stat b {{ font-size: 1.35rem; font-weight: 600; color: {t.INK}; font-variant-numeric: tabular-nums; }}
  .ais-stat small {{ font-size: 12.5px; color: {t.INK_MUTE}; }}

  /* Cards */
  [class*="st-key-card-"], [class*="st-key-hero-"] {{
    background: {t.CARD}; border-radius: 24px; padding: 22px 24px;
    box-shadow: {t.SHADOW};
    transition: transform .3s {EASE}, box-shadow .3s ease;
    animation: ais-rise .56s {EASE} both;
  }}
  [class*="st-key-card-"]:hover, [class*="st-key-hero-"]:hover {{
    transform: translateY(-3px); box-shadow: {t.SHADOW_HOVER};
  }}
  [class*="st-key-hero-"] {{ background: {hero}; color: {on_accent}; }}
  [class*="st-key-hero-"] * {{ color: {on_accent}; }}
{_STAGGER}

  .ais-card-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    min-height: 36px; font-size: 16.5px; font-weight: 600; letter-spacing: -.01em; color: {t.INK};
  }}
  .ais-legend {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; font-weight: 400; color: {t.INK_SOFT}; }}
  .ais-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .ais-legend i {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}

  /* Metrics: each one is a card unless it already sits inside one. */
  [data-testid="stMetric"] {{
    background: {t.CARD}; border-radius: 24px; padding: 20px 22px; box-shadow: {t.SHADOW};
    animation: ais-rise .56s {EASE} both;
  }}
  [class*="st-key-card-"] [data-testid="stMetric"],
  [class*="st-key-hero-"] [data-testid="stMetric"] {{
    background: transparent; box-shadow: none; padding: 0; animation: none;
  }}
  [data-testid="stMetricLabel"] p {{ font-size: 14.5px; font-weight: 500; color: {t.INK_SOFT}; }}
  [data-testid="stMetricValue"] {{
    font-size: 28px; font-weight: 600; letter-spacing: -.035em; color: {t.INK};
    font-variant-numeric: tabular-nums;
  }}
  [data-testid="stMetricDelta"] {{
    display: inline-flex; width: auto; align-items: center; gap: 2px;
    padding: 2px 10px 2px 7px; border-radius: 999px; font-size: 13px; font-weight: 600;
    background: color-mix(in srgb, currentColor 12%, transparent);
  }}
  [class*="st-key-hero-"] [data-testid="stMetricLabel"] p,
  [class*="st-key-hero-"] [data-testid="stMetricValue"] {{ color: {on_accent}; }}
  [class*="st-key-hero-"] [data-testid="stMetricDelta"] {{ background: {on_accent}; }}
  /* Colour only: the delta icon is an SVG whose first path is a transparent
     bounding box (fill="none"); a blanket `fill` paints it as a square. */
  [class*="st-key-hero-"] [data-testid="stMetricDelta"] * {{ color: {LIGHT.ACCENT_DIM} !important; }}

  /* Charts reveal once: lines wipe in from the left, bars grow from the base. */
  [class*="st-key-reveal-left-"] .stPlotlyChart {{ animation: ais-wipe-left .9s {EASE} .3s both; }}
  [class*="st-key-reveal-up-"] .stPlotlyChart {{ animation: ais-wipe-up .7s {EASE} .3s both; }}
  [data-testid="stProgress"] {{ animation: ais-wipe-left .8s {EASE} .3s both; }}

  /* Status pills */
  .ais-pill {{
    display: inline-flex; align-items: center; height: 26px; padding: 0 10px;
    border-radius: 999px; font-size: 13px; font-weight: 600; line-height: 1;
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }}
  .ais-over {{ background: {t.ACCENT_TINT}; color: {t.ACCENT_DIM}; }}
  .ais-approaching {{ background: {t.SUBTLE}; color: {t.INK}; }}
  .ais-ontrack {{ background: {t.POSITIVE_TINT}; color: {t.POSITIVE}; }}
  .ais-muted {{ background: {t.SUBTLE}; color: {t.INK_SOFT}; }}

  /* Action list rows (Home "Needs you"): one line each, the detail in the tooltip. */
  [class*="st-key-todo-"] button {{
    justify-content: flex-start; width: 100%; padding: 3px 8px; min-height: 0; border-radius: 12px;
    font-weight: 500; color: {t.INK};
  }}
  [class*="st-key-todo-"] button > div {{ min-width: 0; overflow: hidden; }}
  [class*="st-key-todo-"] button p {{
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 14px;
  }}
  [class*="st-key-todo-"] .ais-todo-link {{
    font-weight: 500; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    display: flex; align-items: center; padding: 3px 8px;
  }}
  [class*="st-key-card-"][class*="-todo"] [data-testid="stVerticalBlock"] {{ gap: 2px; }}
  [class*="st-key-card-"][class*="-todo"] [data-testid="stPageLink"] a {{ padding: 4px 8px; font-size: 14px; }}

  /* Home at laptop widths under 1000px: the side column (Dues, Needs you) goes
     under the bank card instead of squeezing beside it. */
  @media (max-width: 1000px) {{
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-card-"][class*="-runway"]) {{
      flex-wrap: wrap;
    }}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-card-"][class*="-runway"])
      > [data-testid="stColumn"] {{ flex: 1 1 100%; min-width: 100%; }}
    /* The four KPI tiles go two by two: four across squeezed "$11,201.93" to "$11,2…". */
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-hero-"][class*="-net"]) {{
      flex-wrap: wrap;
    }}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-hero-"][class*="-net"])
      > [data-testid="stColumn"] {{ flex: 1 1 45%; min-width: 45%; }}
  }}

  /* My Plan: the planned-events table is for wide screens; phones get cards. */
  .ais-plan-cards {{ display: none; }}
  .ais-plan-card {{
    background: {t.CARD}; border-radius: 14px; padding: 10px 12px 10px 14px; margin-bottom: 8px;
    border-left: 4px solid var(--c, {t.ACCENT}); box-shadow: {t.SHADOW};
  }}
  .ais-plan-card b {{ display: block; color: {t.INK}; font-weight: 600; }}
  .ais-plan-card span {{ display: block; color: {t.INK_SOFT}; font-size: .85rem; margin-top: 2px; }}
  .ais-plan-card em {{ display: block; color: {t.INK_MUTE}; font-size: .8rem; font-style: normal; margin-top: 2px; }}
  @media (max-width: 640px) {{
    .ais-plan-cards {{ display: block; }}
    .st-key-events_now_table, .st-key-events_next_table {{ display: none; }}
  }}
  [class*="st-key-todo-"] button:hover {{ background: {t.SUBTLE}; color: {t.INK}; }}
  /* Streamlit centres the button's inner wrappers too, not just the button. */
  [class*="st-key-todo-"] button > div, [class*="st-key-todo-"] button > div > span {{
    justify-content: flex-start; text-align: left;
  }}
  [class*="st-key-todo-"] button p {{ text-align: left; }}

  /* Segmented controls and tabs: ink pill for the active choice. */
  /* Streamlit 1.62 renders segmented-control options as role="radio" buttons
     in a stButtonGroup; the selected one carries aria-checked="true". */
  [data-testid="stButtonGroup"] button[role="radio"] {{ border-radius: 999px; }}
  [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"],
  [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"]:hover {{
    background: {t.INK} !important; border-color: {t.INK} !important; color: {t.ON_INK};
  }}
  [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] * {{ color: {t.ON_INK} !important; }}
  .stTabs [data-baseweb="tab-list"] {{ gap: 2px; background: {t.SUBTLE}; padding: 4px; border-radius: 999px; width: fit-content; }}
  .stTabs [data-baseweb="tab"] {{ border-radius: 999px; padding: 6px 16px; height: auto; }}
  .stTabs [aria-selected="true"] {{ background: {t.CARD}; box-shadow: 0 1px 3px rgba(0,0,0,.18); }}
  .stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {{ display: none; }}

  /* Status-toned KPI tiles (My Committee): the value and a wash take the
     status colour, so on track / approaching / over read at a glance. */
  [class*="st-key-tone-ok-"] [data-testid="stMetric"] {{ background: {t.POSITIVE_TINT}; }}
  [class*="st-key-tone-ok-"] [data-testid="stMetricValue"] {{ color: {t.POSITIVE}; }}
  [class*="st-key-tone-warn-"] [data-testid="stMetric"] {{ background: {t.WARNING_TINT}; }}
  [class*="st-key-tone-warn-"] [data-testid="stMetricValue"] {{ color: {t.WARNING}; }}
  [class*="st-key-tone-over-"] [data-testid="stMetric"] {{ background: {t.ACCENT_TINT}; }}
  [class*="st-key-tone-over-"] [data-testid="stMetricValue"] {{ color: {t.ACCENT_DIM}; }}
  .ais-warn {{ background: {t.WARNING_TINT}; color: {t.WARNING}; }}

  /* Vertical spend bar beside the KPI tiles. */
  .ais-vbar {{ display: flex; gap: 18px; align-items: stretch; min-height: 196px; }}
  .ais-vbar-track {{
    position: relative; width: 46px; flex: none; border-radius: 14px;
    background: {t.SUBTLE}; box-shadow: inset 0 0 0 1px {t.GRID}; overflow: visible;
  }}
  .ais-vbar-fill {{ position: absolute; left: 0; right: 0; bottom: 0; border-radius: 14px; }}
  .ais-vbar-pace {{ position: absolute; left: -6px; right: -6px; border-top: 2px dashed {t.INK}; }}
  .ais-vbar-text {{ display: flex; flex-direction: column; justify-content: center; gap: 2px; }}
  .ais-vbar-pct {{ font-size: 40px; font-weight: 700; letter-spacing: -.04em; line-height: 1.05; margin-top: 8px; }}
  .ais-vbar-label {{ font-size: 13.5px; color: {t.INK_SOFT}; }}
  .ais-vbar-money {{ font-size: 14.5px; color: {t.INK}; margin-top: 8px; font-variant-numeric: tabular-nums; }}
  .ais-vbar-term {{ font-size: 12.5px; color: {t.INK_MUTE}; margin-top: 4px; }}
  .ais-tone-ok .ais-vbar-fill {{ background: {t.POSITIVE}; }}   .ais-tone-ok .ais-vbar-pct {{ color: {t.POSITIVE}; }}
  .ais-tone-warn .ais-vbar-fill {{ background: {t.WARNING}; }}  .ais-tone-warn .ais-vbar-pct {{ color: {t.WARNING}; }}
  .ais-tone-over .ais-vbar-fill {{ background: {t.ACCENT}; }}   .ais-tone-over .ais-vbar-pct {{ color: {t.ACCENT_DIM}; }}
  .ais-tone-muted .ais-vbar-fill {{ background: {t.INK_MUTE}; }}

  /* Stacked Pareto: one bar across the budget, biggest payee first. */
  .ais-stack {{ position: relative; display: flex; height: 34px; border-radius: 10px; overflow: hidden; background: {t.SUBTLE}; }}
  .ais-stack span {{ display: block; height: 100%; border-right: 2px solid {t.CARD}; }}
  .ais-stack .ais-stack-rest {{
    background: repeating-linear-gradient(135deg, {t.SUBTLE} 0 6px, {t.BUDGET} 6px 12px); border-right: 0;
  }}
  .ais-stack-mark {{ position: absolute; top: -4px; bottom: -4px; border-left: 2px solid {t.ACCENT}; }}
  .ais-stack-scale {{ display: flex; justify-content: space-between; font-size: 12px; color: {t.INK_MUTE}; margin: 6px 0 14px; }}

  /* Readable tables drawn as HTML (Pareto list, past-term comparison). */
  table.ais-table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }}
  table.ais-table th {{
    text-align: left; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
    color: {t.INK_MUTE}; padding: 0 10px 8px; border-bottom: 1px solid {t.GRID};
  }}
  table.ais-table td {{ padding: 9px 10px; border-bottom: 1px solid {t.GRID}; color: {t.INK}; vertical-align: middle; }}
  table.ais-table tr:last-child td {{ border-bottom: 0; }}
  table.ais-table .num {{ text-align: right; white-space: nowrap; }}
  table.ais-table .muted {{ color: {t.INK_MUTE}; }}
  table.ais-table tr.ais-now td {{ background: {t.SUBTLE}; font-weight: 600; }}
  table.ais-table tr.ais-now td:first-child {{ border-radius: 10px 0 0 10px; }}
  table.ais-table tr.ais-now td:last-child {{ border-radius: 0 10px 10px 0; }}
  .ais-swatch {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
  .ais-chip {{
    display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
    font-size: 11px; font-weight: 600; background: {t.SUBTLE}; color: {t.INK_SOFT}; vertical-align: 1px;
  }}
  .ais-chip-now {{ background: {t.INK}; color: {t.ON_INK}; }}
  .ais-meter {{ display: flex; align-items: center; gap: 10px; min-width: 170px; }}
  .ais-meter-track {{ position: relative; flex: 1; height: 10px; border-radius: 999px; background: {t.SUBTLE}; box-shadow: inset 0 0 0 1px {t.GRID}; }}
  .ais-meter-fill {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: {t.EXPENSE}; }}
  .ais-meter-mark {{ position: absolute; top: -4px; bottom: -4px; border-left: 2px solid {t.INK}; }}
  .ais-meter-value {{ width: 42px; text-align: right; }}
  /* Committees to watch: one progress row per committee. */
  .ais-stat-row {{ display:flex; flex-wrap:wrap; gap:14px 0; margin:.35rem 0 .9rem; }}
  .ais-stat-row .ais-stat {{ padding-right:18px; }}
  .ais-stat b.over {{ color:{t.ACCENT_DIM}; }}
  .ais-event-facts {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; color:{t.INK_SOFT}; font-size:.9rem; margin-bottom:.25rem; }}

  /* Tables, expanders */
  [data-testid="stDataFrame"] {{ border-radius: 16px; overflow: hidden; }}
  [data-testid="stExpander"] details {{ border: 0; background: {t.SUBTLE}; border-radius: 18px; }}

  /* Empty / error states */
  .ais-empty {{
    background: {t.SUBTLE}; border-radius: 20px;
    padding: 1.6rem; text-align: center; color: {t.INK_SOFT}; font-size: .92rem;
  }}
  .ais-empty strong {{ display: block; color: {t.INK}; margin-bottom: .3rem; }}

  /* The appearance switcher's helper iframe takes no room. */
  [data-testid="stElementContainer"]:has(> iframe[title="ais_fmd.ui.appearance.appearance"]) {{
    display: none;
  }}

  @media (prefers-reduced-motion: reduce) {{
    .stApp *, .stApp *::before, .stApp *::after {{ animation: none !important; transition: none !important; }}
  }}
</style>
"""


# The light stylesheet under the old name, for code that imported it.
APP_CSS = app_css(LIGHT)


# --- Compatibility: the old `active()` Palette, now in both modes -------------
#
# Several chart modules read their colours through `theme.active()`, a Palette
# with the field names of an earlier dark theme. It is built from the session's
# tokens now, so those modules follow the theme without changes. New code
# should use `tokens()`.


@dataclass(frozen=True)
class Palette:
    name: str
    void: str
    surface: str
    raised: str
    line: str
    line_lit: str
    line_edge: str
    rule: str
    tile_bg: str
    tile_bg_hover: str
    ink: str
    ink_soft: str
    ink_mute: str
    ink_mute_raised: str
    accent: str
    accent_dim: str
    accent_alt: str
    accent_wash: str
    income: str
    expense: str
    budget: str
    over: str
    over_wash: str
    approaching: str
    approaching_wash: str
    on_track: str
    on_track_wash: str
    unbudgeted: str
    fill_scale: float = 1.0
    categorical: tuple[str, ...] = field(default=())

    @property
    def status_colors(self) -> dict[str, str]:
        return {
            "over": self.over,
            "approaching": self.approaching,
            "on track": self.on_track,
            "unbudgeted": self.unbudgeted,
            "no budget": self.unbudgeted,
        }


def _palette(t: Tokens) -> Palette:
    return Palette(
        name="pebble" if not t.dark else "pebble-dark",
        void=t.CANVAS,
        surface=t.CARD,
        raised=t.SUBTLE,
        line=t.GRID,
        line_lit=t.UNBUDGETED,
        line_edge=t.LINE_EDGE,
        rule=t.GRID,
        tile_bg=t.CARD,
        tile_bg_hover=t.SUBTLE,
        ink=t.INK,
        ink_soft=t.INK_SOFT,
        ink_mute=t.INK_MUTE,
        ink_mute_raised=t.INK_MUTE,
        accent=t.ACCENT,
        accent_dim=t.ACCENT_DIM,
        accent_alt=t.INK,
        accent_wash=t.ACCENT_TINT,
        income=t.INCOME,
        expense=t.EXPENSE,
        budget=t.BUDGET,
        over=t.OVER,
        over_wash=t.ACCENT_TINT,
        approaching=t.APPROACHING,
        approaching_wash=t.SUBTLE,
        on_track=t.ON_TRACK,
        on_track_wash=t.SUBTLE,
        unbudgeted=t.UNBUDGETED,
        fill_scale=1.6 if t.dark else 1.0,
        categorical=t.CATEGORICAL,
    )


PEBBLE = _palette(LIGHT)
PEBBLE_DARK = _palette(DARK)


def active() -> Palette:
    """The palette for this run, in the old field names."""
    return PEBBLE_DARK if is_dark() else PEBBLE


TOKEN_NAMES = frozenset(f.name for f in fields(Tokens) if f.name != "name") | {
    "INCOME", "OVER", "APPROACHING", "SURFACE", "STATUS_COLORS",
}
