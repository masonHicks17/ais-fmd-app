"""
Design tokens, the registered Plotly template, and the app stylesheet.

PEBBLE. Soft white cards on a warm grey canvas. Black carries the structure and
one orange marks the figure that matters most on each screen. Cards fade and
rise in reading order; charts reveal once, on first render.

The token *names* are unchanged from the dark theme so every existing view and
chart keeps working. Only the values moved:

  INCOME      ink        money in reads as the primary series
  EXPENSE     grey       money out sits behind it
  OVER        orange     the one state that needs attention
  APPROACHING ink        80%+ of budget used
  ON_TRACK    light grey nothing to do

Semantic colour is still separate from the accent: ACCENT identifies the
product, the status colours identify meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import plotly.graph_objects as go
import plotly.io as pio

# --- Tokens ------------------------------------------------------------------

CANVAS = "#F5F5F3"
CARD = "#FFFFFF"
SUBTLE = "#F3F3F0"

ACCENT = "#D6400E"          # AIS orange
ACCENT_DIM = "#A8320B"      # orange text on tint
ACCENT_TINT = "#FFE9E0"

POSITIVE = "#157F46"        # delta pills only
POSITIVE_TINT = "#E3F3EA"
WARNING = "#A15C07"         # approaching: amber, so it reads apart from "over"
WARNING_TINT = "#FDF0D8"

# Semantic -- these encode meaning, not brand.
INCOME = "#111111"
EXPENSE = "#9A9B9F"
BUDGET = "#ECECE9"
OVER = ACCENT
APPROACHING = "#111111"
ON_TRACK = "#BDBDB7"
UNBUDGETED = "#D9D9D4"

STATUS_COLORS = {
    "over": OVER,
    "approaching": APPROACHING,
    "on track": ON_TRACK,
    "unbudgeted": UNBUDGETED,
    "no budget": UNBUDGETED,
}

# Neutrals
INK = "#111111"
INK_SOFT = "#55565A"
INK_MUTE = "#86878B"
GRID = "#ECECE9"
SURFACE = CARD

# Past terms in comparison charts, newest first.
HISTORY = ["#9A9B9F", "#BDBDB7", "#D9D9D4"]

# Ordered categorical palette: ink, orange, then greys. Pebble avoids rainbow
# series; when a chart truly needs many colours it uses these in order.
CATEGORICAL = [
    "#111111", "#D6400E", "#86878B", "#BDBDB7", "#A8320B",
    "#55565A", "#F08A5D", "#D9D9D4", "#2A2A2A", "#FFB59A",
]

TEMPLATE_NAME = "ais_fmd"

FONT = "Onest, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif"
_FONT = FONT  # kept for callers that imported the old private name
# Figures stay in Onest with tabular numerals; the old monospace face is retired.
_MONO = FONT

EASE = "cubic-bezier(.2,.8,.2,1)"
SHADOW = "0 1px 2px rgba(17,17,17,.04), 0 8px 24px rgba(17,17,17,.05)"
SHADOW_HOVER = "0 2px 4px rgba(17,17,17,.05), 0 18px 40px rgba(17,17,17,.10)"


def build_template(palette: "Palette | None" = None) -> go.layout.Template:
    """The Pebble Plotly template. `palette` is accepted for old callers and ignored."""
    axis = dict(
        gridcolor=GRID,
        zerolinecolor=GRID,
        linecolor=GRID,
        showline=False,
        tickfont=dict(color=INK_MUTE, size=12),
        title=dict(font=dict(color=INK_MUTE, size=12)),
        automargin=True,
    )
    return go.layout.Template(
        layout=go.Layout(
            font=dict(family=FONT, size=13, color=INK_SOFT),
            title=dict(font=dict(size=16, color=INK), x=0, xanchor="left"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            colorway=CATEGORICAL,
            xaxis=dict(axis, showgrid=False),
            yaxis=axis,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(color=INK_SOFT, size=12),
                bgcolor="rgba(0,0,0,0)",
            ),
            hoverlabel=dict(
                bgcolor=CARD,
                bordercolor=GRID,
                font=dict(family=FONT, size=12, color=INK),
            ),
            barcornerradius=8,
            bargap=0.35,
            margin=dict(l=8, r=8, t=28, b=8),
            separators=".,",
        )
    )


def register() -> None:
    """Register and activate. Safe to call repeatedly."""
    pio.templates[TEMPLATE_NAME] = build_template()
    pio.templates.default = TEMPLATE_NAME


# Card containers are keyed `card-o<N>-<name>` / `hero-o<N>-<name>` by
# shell.card(); N (1-9) sets the entrance delay so cards rise in reading order.
_STAGGER = "\n".join(
    f'  [class*="st-key-card-o{n}-"], [class*="st-key-hero-o{n}-"] '
    f"{{ animation-delay: {80 + (n - 1) * 70}ms; }}"
    for n in range(1, 10)
)

APP_CSS = f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap');

  @keyframes ais-rise {{ from {{ opacity: 0; transform: translateY(16px); }} to {{ opacity: 1; transform: none; }} }}
  @keyframes ais-fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
  @keyframes ais-wipe-left {{ from {{ clip-path: inset(0 100% 0 0); }} to {{ clip-path: inset(0 0 0 0); }} }}
  @keyframes ais-wipe-up {{ from {{ clip-path: inset(100% 0 0 0); }} to {{ clip-path: inset(0 0 0 0); }} }}

  /* Onest everywhere except icon glyphs and code. */
  .stApp, .stApp p, .stApp span, .stApp div, .stApp label, .stApp button,
  .stApp input, .stApp textarea, .stApp h1, .stApp h2, .stApp h3, .stApp h4 {{
    font-family: {FONT};
  }}
  .stApp [data-testid="stIconMaterial"], .stApp .material-symbols-rounded,
  .stApp code, .stApp pre {{ font-family: revert; }}
  .stApp [data-testid="stIconMaterial"] {{ font-family: 'Material Symbols Rounded'; }}

  .stApp {{ background: {CANVAS}; }}
  /* The top nav header is 60px tall and fixed, so content starts below it. */
  .block-container {{ padding-top: 5rem; padding-bottom: 4rem; max-width: 1440px; }}

  /* Top navigation: pill links; the active page is a black pill.
     Selectors verified against Streamlit 1.62: each link is
     a[data-testid="stTopNavLink"] inside a stTopNavLinkContainer, and the
     icon and label spans carry their own inline colours, so the active
     state has to recolour the children, not just the link. The container's
     default padding is what pushed pages into the "N more" overflow. */
  header[data-testid="stHeader"] {{ background: {CANVAS}; }}
  header [data-testid="stTopNavLinkContainer"] {{ padding: 0 2px; }}
  header [data-testid="stTopNavLink"] {{
    border-radius: 999px; padding: 0 14px; height: 44px; display: inline-flex;
    align-items: center; color: {INK_SOFT}; font-weight: 500; transition: background .2s;
  }}
  header [data-testid="stTopNavLink"]:hover {{ background: {SUBTLE}; color: {INK}; }}
  header [data-testid="stTopNavLink"][aria-current="page"] {{ background: {INK}; color: #fff; }}
  header [data-testid="stTopNavLink"][aria-current="page"] * {{ color: #fff !important; }}

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
  .st-key-ais-profile button {{
    border-radius: 999px; height: 44px; padding: 0 16px 0 10px;
    background: {SURFACE}; border: 1px solid {GRID}; color: {INK}; font-weight: 500;
  }}
  .st-key-ais-profile button:hover {{ background: {SUBTLE}; border-color: {GRID}; }}
  .st-key-ais-profile [data-testid="stIconMaterial"] {{ font-size: 1.5rem; }}
  /* The menu itself: narrow and tight. The body is portalled out of
     .st-key-ais-profile, so it is found by what it contains. */
  [data-testid="stPopoverBody"]:has(.ais-profile-who) {{
    width: 260px !important; min-width: 260px !important; padding: 12px 14px !important;
  }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) [data-testid="stVerticalBlock"] {{ gap: 6px; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) hr.ais-rule {{
    margin: 6px 0 !important; border-top: 1px solid {GRID} !important;
  }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) [data-testid="stPageLink"] a {{ padding: 4px 6px; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) label {{ min-height: 0; margin-bottom: 0; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) label p {{ font-size: .8rem; color: {INK_SOFT}; }}
  [data-testid="stPopoverBody"]:has(.ais-profile-who) button {{ min-height: 34px; padding: 4px 10px; }}
  .ais-profile-who {{
    display: flex; flex-direction: column; gap: 0; padding: 0; line-height: 1.3;
    margin-bottom: 1rem;  /* offsets Streamlit's -1rem under markdown blocks */
  }}
  .ais-profile-who b {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .ais-profile-who b {{ color: {INK}; font-weight: 600; }}
  .ais-profile-who span {{ color: {INK_SOFT}; font-size: .85rem; }}

  /* Sandbox banner: a quiet pill, still impossible to miss. */
  .ais-banner {{
    display: inline-flex; align-items: center; gap: .5rem;
    background: {ACCENT_TINT}; color: {ACCENT_DIM};
    padding: .35rem .9rem; border-radius: 999px;
    font-size: .82rem; margin-bottom: .9rem;
  }}
  .ais-banner b {{ color: {ACCENT_DIM}; letter-spacing: .04em; font-weight: 600; }}
  .ais-banner-prod {{ background: {INK}; color: #fff; }}
  .ais-banner-prod b {{ color: #fff; }}

  /* Page header */
  .ais-head {{ margin-bottom: .2rem; animation: ais-rise .56s {EASE} both; }}
  .ais-head h1 {{
    font-size: 2.5rem; font-weight: 600; letter-spacing: -.035em; line-height: 1.05;
    margin: 0 0 .35rem; padding: 0; color: {INK};
  }}
  a.ais-todo-link {{
    color: {INK}; font-weight: 500; text-decoration: none;
    display: inline-flex; align-items: flex-start; gap: .55rem;
  }}
  a.ais-todo-link .material-symbols-rounded {{
    font-family: 'Material Symbols Rounded'; font-size: 1.2rem; line-height: 1.4;
  }}
  a.ais-todo-link:hover {{ text-decoration: underline; }}
  .ais-section {{ margin: 2.4rem 0 .2rem; scroll-margin-top: 80px; }}
  .ais-section h2 {{
    font-size: 1.6rem; font-weight: 600; letter-spacing: -.025em; line-height: 1.1;
    margin: 0 0 .3rem; padding: 0; color: {INK};
  }}
  .ais-section p {{ color: {INK_SOFT}; font-size: .95rem; margin: 0; }}
  .ais-head p {{ color: {INK_SOFT}; font-size: 1rem; margin: 0; max-width: 70ch; }}
  .ais-rule {{ height: 0; background: none; margin: 1.2rem 0; border: 0; }}

  /* Cards */
  [class*="st-key-card-"], [class*="st-key-hero-"] {{
    background: {CARD}; border-radius: 24px; padding: 22px 24px;
    box-shadow: {SHADOW};
    transition: transform .3s {EASE}, box-shadow .3s ease;
    animation: ais-rise .56s {EASE} both;
  }}
  [class*="st-key-card-"]:hover, [class*="st-key-hero-"]:hover {{
    transform: translateY(-3px); box-shadow: {SHADOW_HOVER};
  }}
  [class*="st-key-hero-"] {{ background: {ACCENT}; color: #fff; }}
  [class*="st-key-hero-"] * {{ color: #fff; }}
{_STAGGER}

  .ais-card-head {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    min-height: 36px; font-size: 16.5px; font-weight: 600; letter-spacing: -.01em; color: {INK};
  }}
  .ais-legend {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; font-weight: 400; color: {INK_SOFT}; }}
  .ais-legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .ais-legend i {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}

  /* Metrics: each one is a card unless it already sits inside one. */
  [data-testid="stMetric"] {{
    background: {CARD}; border-radius: 24px; padding: 20px 22px; box-shadow: {SHADOW};
    animation: ais-rise .56s {EASE} both;
  }}
  [class*="st-key-card-"] [data-testid="stMetric"],
  [class*="st-key-hero-"] [data-testid="stMetric"] {{
    background: transparent; box-shadow: none; padding: 0; animation: none;
  }}
  [data-testid="stMetricLabel"] p {{ font-size: 14.5px; font-weight: 500; color: {INK_SOFT}; }}
  [data-testid="stMetricValue"] {{
    font-size: 28px; font-weight: 600; letter-spacing: -.035em; color: {INK};
    font-variant-numeric: tabular-nums;
  }}
  [data-testid="stMetricDelta"] {{
    display: inline-flex; width: auto; align-items: center; gap: 2px;
    padding: 2px 10px 2px 7px; border-radius: 999px; font-size: 13px; font-weight: 600;
    background: color-mix(in srgb, currentColor 12%, transparent);
  }}
  [class*="st-key-hero-"] [data-testid="stMetricLabel"] p,
  [class*="st-key-hero-"] [data-testid="stMetricValue"] {{ color: #fff; }}
  [class*="st-key-hero-"] [data-testid="stMetricDelta"] {{ background: #fff; }}
  /* Colour only: the delta icon is an SVG whose first path is a transparent
     bounding box (fill="none"); a blanket `fill` paints it as a square. */
  [class*="st-key-hero-"] [data-testid="stMetricDelta"] * {{ color: {ACCENT_DIM} !important; }}

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
  .ais-over {{ background: {ACCENT_TINT}; color: {ACCENT_DIM}; }}
  .ais-approaching {{ background: {SUBTLE}; color: {INK}; }}
  .ais-ontrack {{ background: {POSITIVE_TINT}; color: {POSITIVE}; }}
  .ais-muted {{ background: {SUBTLE}; color: {INK_SOFT}; }}

  /* Action list rows (Home "Needs you"). */
  [class*="st-key-todo-"] button {{
    justify-content: flex-start; width: 100%; padding: 6px 8px; border-radius: 18px;
    font-weight: 600; color: {INK};
  }}
  [class*="st-key-todo-"] button:hover {{ background: {SUBTLE}; color: {INK}; }}
  /* Streamlit centres the button's inner wrappers too, not just the button. */
  [class*="st-key-todo-"] button > div, [class*="st-key-todo-"] button > div > span {{
    justify-content: flex-start; text-align: left;
  }}
  [class*="st-key-todo-"] button p {{ text-align: left; }}

  /* Segmented controls and tabs: black pill for the active choice. */
  /* Streamlit 1.62 renders segmented-control options as role="radio" buttons
     in a stButtonGroup; the selected one carries aria-checked="true". */
  [data-testid="stButtonGroup"] button[role="radio"] {{ border-radius: 999px; }}
  [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] {{
    background: {INK}; border-color: {INK}; color: #fff;
  }}
  [data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] * {{ color: #fff !important; }}
  .stTabs [data-baseweb="tab-list"] {{ gap: 2px; background: {SUBTLE}; padding: 4px; border-radius: 999px; width: fit-content; }}
  .stTabs [data-baseweb="tab"] {{ border-radius: 999px; padding: 6px 16px; height: auto; }}
  .stTabs [aria-selected="true"] {{ background: {CARD}; box-shadow: 0 1px 3px rgba(17,17,17,.10); }}
  .stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] {{ display: none; }}

  /* Status-toned KPI tiles (My Committee): the value and a wash take the
     status colour, so on track / approaching / over read at a glance. */
  [class*="st-key-tone-ok-"] [data-testid="stMetric"] {{ background: {POSITIVE_TINT}; }}
  [class*="st-key-tone-ok-"] [data-testid="stMetricValue"] {{ color: {POSITIVE}; }}
  [class*="st-key-tone-warn-"] [data-testid="stMetric"] {{ background: {WARNING_TINT}; }}
  [class*="st-key-tone-warn-"] [data-testid="stMetricValue"] {{ color: {WARNING}; }}
  [class*="st-key-tone-over-"] [data-testid="stMetric"] {{ background: {ACCENT_TINT}; }}
  [class*="st-key-tone-over-"] [data-testid="stMetricValue"] {{ color: {ACCENT_DIM}; }}
  .ais-warn {{ background: {WARNING_TINT}; color: {WARNING}; }}

  /* Vertical spend bar beside the KPI tiles. */
  .ais-vbar {{ display: flex; gap: 18px; align-items: stretch; min-height: 196px; }}
  .ais-vbar-track {{
    position: relative; width: 46px; flex: none; border-radius: 14px;
    background: {SUBTLE}; box-shadow: inset 0 0 0 1px {GRID}; overflow: visible;
  }}
  .ais-vbar-fill {{ position: absolute; left: 0; right: 0; bottom: 0; border-radius: 14px; }}
  .ais-vbar-pace {{ position: absolute; left: -6px; right: -6px; border-top: 2px dashed {INK}; }}
  .ais-vbar-text {{ display: flex; flex-direction: column; justify-content: center; gap: 2px; }}
  .ais-vbar-pct {{ font-size: 40px; font-weight: 700; letter-spacing: -.04em; line-height: 1.05; margin-top: 8px; }}
  .ais-vbar-label {{ font-size: 13.5px; color: {INK_SOFT}; }}
  .ais-vbar-money {{ font-size: 14.5px; color: {INK}; margin-top: 8px; font-variant-numeric: tabular-nums; }}
  .ais-vbar-term {{ font-size: 12.5px; color: {INK_MUTE}; margin-top: 4px; }}
  .ais-tone-ok .ais-vbar-fill {{ background: {POSITIVE}; }}   .ais-tone-ok .ais-vbar-pct {{ color: {POSITIVE}; }}
  .ais-tone-warn .ais-vbar-fill {{ background: {WARNING}; }}  .ais-tone-warn .ais-vbar-pct {{ color: {WARNING}; }}
  .ais-tone-over .ais-vbar-fill {{ background: {ACCENT}; }}   .ais-tone-over .ais-vbar-pct {{ color: {ACCENT_DIM}; }}
  .ais-tone-muted .ais-vbar-fill {{ background: {INK_MUTE}; }}

  /* Stacked Pareto: one bar across the budget, biggest payee first. */
  .ais-stack {{ position: relative; display: flex; height: 34px; border-radius: 10px; overflow: hidden; background: {SUBTLE}; }}
  .ais-stack span {{ display: block; height: 100%; border-right: 2px solid {CARD}; }}
  .ais-stack .ais-stack-rest {{
    background: repeating-linear-gradient(135deg, {SUBTLE} 0 6px, {BUDGET} 6px 12px); border-right: 0;
  }}
  .ais-stack-mark {{ position: absolute; top: -4px; bottom: -4px; border-left: 2px solid {ACCENT}; }}
  .ais-stack-scale {{ display: flex; justify-content: space-between; font-size: 12px; color: {INK_MUTE}; margin: 6px 0 14px; }}

  /* Readable tables drawn as HTML (Pareto list, past-term comparison). */
  table.ais-table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }}
  table.ais-table th {{
    text-align: left; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
    color: {INK_MUTE}; padding: 0 10px 8px; border-bottom: 1px solid {GRID};
  }}
  table.ais-table td {{ padding: 9px 10px; border-bottom: 1px solid {GRID}; color: {INK}; vertical-align: middle; }}
  table.ais-table tr:last-child td {{ border-bottom: 0; }}
  table.ais-table .num {{ text-align: right; white-space: nowrap; }}
  table.ais-table .muted {{ color: {INK_MUTE}; }}
  table.ais-table tr.ais-now td {{ background: {SUBTLE}; font-weight: 600; }}
  table.ais-table tr.ais-now td:first-child {{ border-radius: 10px 0 0 10px; }}
  table.ais-table tr.ais-now td:last-child {{ border-radius: 0 10px 10px 0; }}
  .ais-swatch {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
  .ais-chip {{
    display: inline-block; margin-left: 6px; padding: 1px 7px; border-radius: 999px;
    font-size: 11px; font-weight: 600; background: {SUBTLE}; color: {INK_SOFT}; vertical-align: 1px;
  }}
  .ais-chip-now {{ background: {INK}; color: #fff; }}
  .ais-meter {{ display: flex; align-items: center; gap: 10px; min-width: 170px; }}
  .ais-meter-track {{ position: relative; flex: 1; height: 10px; border-radius: 999px; background: {SUBTLE}; box-shadow: inset 0 0 0 1px {GRID}; }}
  .ais-meter-fill {{ position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: {EXPENSE}; }}
  .ais-meter-mark {{ position: absolute; top: -4px; bottom: -4px; border-left: 2px solid {INK}; }}
  .ais-meter-value {{ width: 42px; text-align: right; }}

  /* Tables, expanders */
  [data-testid="stDataFrame"] {{ border-radius: 16px; overflow: hidden; }}
  [data-testid="stExpander"] details {{ border: 0; background: {SUBTLE}; border-radius: 18px; }}

  /* Empty / error states */
  .ais-empty {{
    background: {SUBTLE}; border-radius: 20px;
    padding: 1.6rem; text-align: center; color: {INK_SOFT}; font-size: .92rem;
  }}
  .ais-empty strong {{ display: block; color: {INK}; margin-bottom: .3rem; }}

  @media (prefers-reduced-motion: reduce) {{
    .stApp *, .stApp *::before, .stApp *::after {{ animation: none !important; transition: none !important; }}
  }}
</style>
"""


def app_css(palette: "Palette | None" = None) -> str:
    """The stylesheet. Pebble has one palette, so `palette` is ignored."""
    return APP_CSS


# --- Compatibility: one palette behind the old `active()` API ----------------
#
# Between the design snapshot and this merge the app moved to per-session
# light/dark palettes read through `theme.active()`, and several pages the
# redesign does not touch (Assistant, Dues, Officer, Planner) still read their
# colours that way. Pebble is light-only, so `active()` now always returns the
# one palette below, built from the module tokens. New code should use the
# module constants (theme.INK, theme.OVER, ...) directly.


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
        return dict(STATUS_COLORS)


PEBBLE = Palette(
    name="pebble",
    void=CANVAS,
    surface=CARD,
    raised=SUBTLE,
    line=GRID,
    line_lit="#D9D9D4",
    line_edge="#BDBDB7",
    rule=GRID,
    tile_bg=CARD,
    tile_bg_hover=SUBTLE,
    ink=INK,
    ink_soft=INK_SOFT,
    ink_mute=INK_MUTE,
    ink_mute_raised=INK_MUTE,
    accent=ACCENT,
    accent_dim=ACCENT_DIM,
    accent_alt=INK,
    accent_wash=ACCENT_TINT,
    income=INCOME,
    expense=EXPENSE,
    budget=BUDGET,
    over=OVER,
    over_wash=ACCENT_TINT,
    approaching=APPROACHING,
    approaching_wash=SUBTLE,
    on_track=ON_TRACK,
    on_track_wash=SUBTLE,
    unbudgeted=UNBUDGETED,
    categorical=tuple(CATEGORICAL),
)


def active() -> Palette:
    """The palette for this run. Always Pebble; kept for pages that call it."""
    return PEBBLE
