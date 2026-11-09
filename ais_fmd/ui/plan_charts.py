"""
Charts for a VP's My Plan page (`views/MyPlan.py`).

Kept in their own file, apart from `ui/committee_charts.py` (My Committee) and
`ui/charts.py` (the UI team's), so changes to one page's charts never collide
with another's. Same conventions: colours from the active palette, no data
fetching, and an `empty_figure` placeholder when there is nothing to draw.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from . import charts, theme


def plan_vs_actual(
    sheet: pd.DataFrame, *, actual: str = "Spent", actual_label: str = "Spent", flag_over: bool = True
) -> go.Figure:
    """
    One pair of horizontal bars per category: the plan, and what has actually
    gone out (this term's spending, or for next term what the same season cost).
    Planned is the quiet grey, actual the ink; an actual bar past its plan turns
    the over colour, and each bar carries its value, so nothing rests on colour.
    `flag_over=False` for a comparison that can't be "over" a plan -- last
    season's spending set against next term's plan.
    """
    if sheet is None or sheet.empty or not (sheet["Planned"].sum() or sheet[actual].sum()):
        return charts.empty_figure("Nothing planned or spent yet.")
    p = theme.active()
    rows = sheet.iloc[::-1]  # biggest plan at the top
    over = (rows[actual] > rows["Planned"]) if flag_over else pd.Series(False, index=rows.index)
    figure = go.Figure()
    figure.add_trace(go.Bar(
        name="Planned", y=rows["Category"], x=rows["Planned"], orientation="h",
        offsetgroup="planned", marker=dict(color=p.line_edge), text=[f"${v:,.0f}" for v in rows["Planned"]],
        textposition="outside", cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>Planned $%{x:,.2f}<extra></extra>",
    ))
    # Within budget and over budget are separate traces sharing one slot, so the
    # legend shows both meanings instead of whichever colour the first bar had.
    for name, mask, color in ((actual_label, ~over, p.ink), ("Over plan", over, p.over)):
        if not mask.any() and name == "Over plan":
            continue
        figure.add_trace(go.Bar(
            name=name, y=rows["Category"], x=rows[actual].where(mask), orientation="h",
            offsetgroup="actual", marker=dict(color=color),
            text=[f"${v:,.0f}" if m else "" for v, m in zip(rows[actual], mask)],
            textposition="outside", cliponaxis=False,
            hovertemplate="<b>%{y}</b><br>" + actual_label + " $%{x:,.2f}<extra></extra>",
        ))
    figure.update_layout(
        barmode="group", bargap=0.35, bargroupgap=0.12,
        height=max(220, 70 * len(rows) + 60),
        margin=dict(l=8, r=48, t=8, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis=dict(tickprefix="$", separatethousands=True, showgrid=True, zeroline=False),
        yaxis=dict(title=None),
    )
    return figure


def plan_donut(slices: pd.DataFrame) -> go.Figure:
    """
    Where the plan goes: one ring slice per category, in the palette's fixed
    order, with the category and its share written on the slice and the total
    in the middle. A 2px surface gap between slices keeps neighbours apart.
    """
    t = theme.tokens()
    if slices is None or slices.empty:
        return charts.empty_figure("Nothing planned yet.")
    p = theme.active()
    # The team palette's neutrals only, dark and light alternating so neighbours
    # stand apart. Its oranges are left out on purpose: next to this ring, orange
    # means "over plan", and a status colour is never reused for a category.
    palette = [c for c in (p.categorical or t.CATEGORICAL) if c.upper() not in {
        p.over.upper(), p.accent.upper(), t.ACCENT_DIM.upper(), "#F08A5D", "#FFB59A",
    }]
    palette = sorted(palette, key=lambda c: int(c.lstrip("#")[:2], 16))
    palette = [palette[i // 2] if i % 2 == 0 else palette[-(i // 2) - 1] for i in range(len(palette))]
    total = float(slices["Planned"].sum())
    figure = go.Figure(go.Pie(
        labels=slices["Category"], values=slices["Planned"], hole=0.62, sort=False,
        direction="clockwise",
        marker=dict(colors=palette[: len(slices)], line=dict(color=p.surface, width=2)),
        textinfo="label+percent", textposition="outside", automargin=True,
        hovertemplate="<b>%{label}</b><br>$%{value:,.2f} · %{percent}<extra></extra>",
    ))
    figure.update_layout(
        height=320, showlegend=False, margin=dict(l=8, r=8, t=24, b=24),
        annotations=[dict(
            text=f"${total:,.0f}<br><span style='font-size:12px'>planned</span>",
            x=0.5, y=0.5, showarrow=False, font=dict(size=20, color=p.ink),
        )],
    )
    return figure


def projection_bar(figures: dict, color: str) -> str:
    """
    One bar across the budget: what has been spent (solid), the planned events
    still to come (striped, an estimate), and the room left for extras (empty).
    When the plan goes past the budget the bar is scaled to the plan and a mark
    shows where the budget ends.
    """
    t = theme.tokens()
    from html import escape

    budget = float(figures["budget"] or 0)
    spent, ahead = figures["spent"], figures["ahead"]
    scale = max(budget, spent + ahead, 0.01)

    def width(amount: float) -> str:
        return f"{max(amount, 0) / scale * 100:.2f}%"

    room = max(budget - spent - ahead, 0.0)
    mark = (f'<div style="position:absolute;top:-4px;bottom:-4px;left:{width(budget)};width:2px;'
            f'background:{t.OVER}"></div>') if spent + ahead > budget > 0 else ""

    def key(swatch: str, label: str, amount: float) -> str:
        return (f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:18px">'
                f'<span style="width:12px;height:12px;border-radius:3px;{swatch}"></span>'
                f'{escape(label)} <b>&#36;{amount:,.0f}</b></span>')

    stripes = f"repeating-linear-gradient(135deg,{color} 0 6px,{_tint(color)} 6px 12px)"
    return (
        '<div style="margin:.4rem 0 1rem">'
        f'<div style="position:relative;display:flex;height:26px;border-radius:8px;overflow:visible;'
        f'background:{t.CARD};box-shadow:inset 0 0 0 1px {t.INK_MUTE}55">'
        f'<div title="Spent (reported by the bank)" style="width:{width(spent)};background:{color};'
        f'border-radius:8px 0 0 8px"></div>'
        f'<div title="Planned events still to come (estimated)" style="width:{width(ahead)};background:{stripes}"></div>'
        f'{mark}</div>'
        f'<div style="margin-top:.5rem;font-size:.85rem;color:{t.INK_SOFT}">'
        + key(f"background:{color}", "Spent (reported)", spent)
        + key(f"background:{stripes}", "Planned events ahead (estimate)", ahead)
        + key(f"background:{t.SUBTLE};border:1px solid {t.INK_MUTE}", "Room for extras", room)
        + "</div></div>"
    )


def _tint(hex_color: str) -> str:
    t = theme.tokens()
    value = hex_color.lstrip("#")
    if len(value) != 6:
        return t.SUBTLE
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},0.35)"
