"""
Shared pieces of the Transactions screens: the four-stage status bar, the
transaction row header, and the Haiku spend meter.

Presentation only. Every figure shown is passed in by the page from what the
categorizer, the database or the model ledger already produced.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass

import streamlit as st

from ais_fmd.ui import shell, theme
from ais_fmd.ui import txn_display as show

def _css(t) -> str:
    # Read at draw time (theme.tokens()), so dark mode gets dark values.
    return f"""
<style>
.ais-status {{ background:{t.CARD}; border-radius:24px; padding:18px 20px 16px; box-shadow:{t.SHADOW};
  display:flex; flex-direction:column; gap:12px; margin-bottom:8px; }}
.ais-status-top {{ display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; font-size:14px; }}
.ais-status-top b {{ font-weight:600; }}
.ais-status-top span {{ color:{t.INK_SOFT}; }}
.ais-status ol {{ list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }}
.ais-status li {{ display:flex; flex-direction:column; gap:10px; min-width:0; margin:0; }}
.ais-status .bar {{ display:block; height:8px; border-radius:999px; background:{t.GRID}; overflow:hidden; }}
.ais-status .bar i {{ display:block; height:8px; border-radius:999px; background:{t.ACCENT}; }}
.ais-status .done .bar {{ background:{t.INK}; }}
.ais-status .label {{ display:flex; align-items:flex-start; gap:10px; min-width:0; }}
.ais-status .dot {{ flex:none; width:26px; height:26px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:13px; font-weight:700; background:{t.SUBTLE}; color:{t.INK_SOFT}; }}
.ais-status .done .dot {{ background:{t.INK}; color:{t.CARD}; }}
.ais-status .active .dot {{ background:{t.ACCENT}; color:#fff; }}
.ais-status .name {{ font-weight:600; line-height:1.3; }}
.ais-status .sub {{ font-size:13px; color:{t.INK_SOFT}; line-height:1.35; }}
.ais-status .needs .sub {{ color:{t.ACCENT_DIM}; font-weight:700; }}
@media (max-width: 640px) {{ .ais-status ol {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}

.ais-row-top {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 10px; }}
.ais-row-top .amt {{ font-size:20px; font-weight:700; }}
.ais-row-top .amt.in {{ color:#11683A; }}
.ais-row-top .who {{ font-size:17px; font-weight:600; }}
.ais-row-top .when {{ font-size:14px; color:{t.INK_SOFT}; }}
.ais-row-card {{ font-size:14px; color:{t.INK_SOFT}; margin-top:2px; }}
.ais-row-why {{ font-size:14px; margin-top:6px; display:flex; flex-wrap:wrap; align-items:center; gap:8px; }}
.ais-tag {{ display:inline-block; border-radius:999px; padding:2px 10px; font-size:13px; font-weight:600;
  background:{t.WARNING_TINT}; color:#7A4505; }}
.ais-tag.ok {{ background:{t.POSITIVE_TINT}; color:#11683A; }}
.ais-tag.plain {{ background:{t.SUBTLE}; color:{t.INK}; }}
.ais-row-done {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px 12px; font-size:15px; }}
.ais-meter {{ display:flex; align-items:center; gap:10px; font-size:13px; color:{t.INK_SOFT}; }}
.ais-meter .bar {{ flex:0 0 160px; height:6px; border-radius:999px; background:{t.GRID}; overflow:hidden; display:block; }}
.ais-meter .bar i {{ display:block; height:6px; background:{t.INK}; }}
.ais-meter b {{ color:{t.INK}; font-weight:600; }}
.st-key-ais-savebar {{ position:sticky; bottom:12px; z-index:20; background:{t.INK}; color:{t.CARD};
  border-radius:24px; padding:10px 14px 10px 22px; box-shadow:0 8px 24px rgba(17,17,17,.18); }}
.st-key-ais-savebar p, .st-key-ais-savebar strong {{ color:{t.CARD}; margin:0; }}
.ais-glance {{ display:flex; flex-wrap:wrap; gap:8px; }}
.ais-glance span {{ background:{t.SUBTLE}; border-radius:999px; padding:5px 12px; font-size:14px; }}
</style>
"""


def styles() -> None:
    """The CSS for everything in this module; call once per page, before drawing."""
    st.markdown(_css(theme.tokens()), unsafe_allow_html=True)


# --- Status bar ----------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    label: str
    state: str  # "done" | "active" | "upcoming"
    sub: str = ""
    fill: float = 0.0  # 0..1, how far an active stage has got
    needs_you: bool = False


def status_bar(stages: list[Stage], *, title: str, right: str = "") -> None:
    items = []
    for number, stage in enumerate(stages, start=1):
        classes = stage.state + (" needs" if stage.needs_you and stage.state == "active" else "")
        if stage.state == "done":
            bar = '<span class="bar"></span>'
            dot = "&#10003;"
        elif stage.state == "active":
            width = max(0.0, min(1.0, stage.fill)) * 100
            bar = f'<span class="bar"><i style="width:{width:.0f}%"></i></span>'
            dot = str(number)
        else:
            bar = '<span class="bar"></span>'
            dot = str(number)
        items.append(
            f'<li class="{classes}">{bar}<div class="label"><span class="dot" aria-hidden="true">{dot}</span>'
            f'<span><div class="name">{_html.escape(stage.label)}</div>'
            f'<div class="sub">{_html.escape(stage.sub)}</div></span></div></li>'
        )
    shell.html(
        f'<section class="ais-status" aria-label="Statement status">'
        f'<div class="ais-status-top"><b>{_html.escape(title)}</b><span>{_html.escape(right)}</span></div>'
        f'<ol>{"".join(items)}</ol></section>'
    )


# --- Rows ------------------------------------------------------------------------


def row_header(record: dict, *, tag: str = "", why: str = "", tag_kind: str = "") -> None:
    """Amount, merchant, weekday date, card line and (optionally) why it's here."""
    amount = record.get("amount")
    details = record.get("details")
    when = record.get("transaction_date")
    money_class = "amt in" if show.is_money_in(amount) else "amt"
    card = show.card_line(details, when, amount)
    parts = [
        '<div class="ais-row-top">'
        f'<span class="{money_class}">{_html.escape(show.signed_money(amount))}</span>'
        f'<span class="who">{_html.escape(show.merchant(details))}</span>'
        f'<span class="when">{_html.escape(show.day(when))}</span></div>'
    ]
    if card:
        parts.append(f'<div class="ais-row-card">{_html.escape(card)}</div>')
    if tag or why:
        tag_html = f'<span class="ais-tag {tag_kind}">{_html.escape(tag)}</span>' if tag else ""
        parts.append(f'<div class="ais-row-why">{tag_html}<span>{_html.escape(why)}</span></div>')
    shell.html("".join(parts))


def bank_line(details: object) -> None:
    """The raw bank description, one click away."""
    with st.expander("Bank line"):
        st.code(" ".join(str(details or "").split()), language=None, wrap_lines=True)


def booked_line(record: dict, committee: str) -> None:
    amount = record.get("amount")
    shell.html(
        '<div class="ais-row-done">'
        f'<span style="color:#11683A;font-weight:700" aria-hidden="true">&#10003;</span>'
        f'<b>{_html.escape(show.signed_money(amount))}</b>'
        f'<span>{_html.escape(show.merchant(record.get("details")))}</span>'
        f'<span style="color:{theme.tokens().INK_SOFT};font-size:14px">{_html.escape(show.day(record.get("transaction_date")))}</span>'
        f'<span class="ais-tag ok">{_html.escape(committee)}</span></div>'
    )


# --- Haiku spend ---------------------------------------------------------------------


def haiku_meter(spent: float, cap: float) -> None:
    """How much of this month's Haiku budget is used."""
    share = (spent / cap) if cap else 0.0
    width = max(2.0, min(100.0, share * 100))
    spent_text = f"${spent:,.4f}" if spent < 1 else f"${spent:,.2f}"
    shell.html(
        f'<div class="ais-meter"><span>Haiku this month</span>'
        f'<span class="bar" aria-hidden="true"><i style="width:{width:.0f}%"></i></span>'
        f"<b>{spent_text} of ${cap:,.2f}</b></div>"
    )


def glance(totals: list[tuple[str, float]]) -> None:
    """Committee totals for one statement, largest first."""
    chips = "".join(
        f"<span><b>{_html.escape(name)}</b> {_html.escape(show.signed_money(value))}</span>"
        for name, value in sorted(totals, key=lambda item: -abs(item[1]))
    )
    shell.html(f'<div class="ais-glance">{chips}</div>')
