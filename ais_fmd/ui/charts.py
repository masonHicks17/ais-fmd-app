"""
Chart factories.

Every chart the app draws comes from here, so they cannot drift apart.

PEBBLE CHART PLAN. Home and Dashboard never share a chart:

  Home (now and next, from today forward)
    cash_runway       will the balance stay above the reserve through term end?
    spend_pace        are we spending faster than the budget allows?
    committee_pace    which committees are ahead of the calendar, and when do they run out?
    dues_curve        are members paying faster than the same term last year?

  Dashboard (analysis, from today backward)
    KPI mini bars     each figure at the same day of the last five terms (st.metric chart_data)
    waterfall         which income sources and committees produced the net
    budget_health     % used against % of term elapsed, all committees
    net_by_week       cumulative net against the last two equivalent terms

Conventions: ink for this term, greys for past terms, orange only for the one
figure that matters (projection, over budget, net). Rounded bars come from the
template's `barcornerradius`.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from . import theme

_HEIGHT_PER_ROW = 34
_MIN_HEIGHT = 220


def _height(rows: int, *, per_row: int = _HEIGHT_PER_ROW, base: int = 90) -> int:
    return max(_MIN_HEIGHT, base + rows * per_row)


def _translucent(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _short_money(value: float) -> str:
    sign = "−" if value < 0 else ""
    value = abs(value)
    if value >= 1000:
        return f"{sign}${value / 1000:.1f}k"
    return f"{sign}${value:,.0f}"


def _day_axis(start: pd.Timestamp, length: int) -> dict:
    """Days since term start on the axis, labelled with month names."""
    values, text = [], []
    for day in range(0, length + 1):
        date = start + pd.Timedelta(days=day)
        if date.day == 1:
            values.append(day)
            text.append(f"{date:%b}")
    return dict(
        tickmode="array", tickvals=values, ticktext=text, range=[0, length],
        showgrid=False, title=None, fixedrange=True,
    )


def _money_axis(**extra) -> dict:
    """A currency axis. `extra` overrides the defaults (e.g. nticks=3)."""
    return {**dict(tickprefix="$", separatethousands=True, title=None, fixedrange=True, nticks=5), **extra}


def empty_figure(message: str) -> go.Figure:
    """A chart-shaped placeholder, so layout does not jump when data is absent."""
    figure = go.Figure()
    figure.add_annotation(
        text=message, showarrow=False, font=dict(color=theme.INK_MUTE, size=13),
        xref="paper", yref="paper", x=0.5, y=0.5,
    )
    figure.update_layout(height=_MIN_HEIGHT, xaxis=dict(visible=False), yaxis=dict(visible=False))
    return figure


# --- Home --------------------------------------------------------------------

def cash_runway(runway, clock, *, reserve: float) -> go.Figure:
    """Actual balance to today (ink), projected range to term end (orange), reserve line."""
    if runway is None or runway.actual.empty:
        return empty_figure("No balance history yet.")

    figure = go.Figure()
    projected = runway.projected
    if len(projected) > 1:
        figure.add_trace(go.Scatter(
            x=projected["Day"], y=projected["High"], mode="lines", line=dict(width=0),
            hoverinfo="skip", showlegend=False,
        ))
        figure.add_trace(go.Scatter(
            x=projected["Day"], y=projected["Low"], mode="lines", line=dict(width=0),
            fill="tonexty", fillcolor=theme.ACCENT_TINT, name="Likely range", hoverinfo="skip",
        ))
        figure.add_trace(go.Scatter(
            x=projected["Day"], y=projected["Mid"], mode="lines", name="Projected",
            line=dict(color=theme.ACCENT, width=2.5, dash="dash"),
            hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>Projected</extra>",
        ))
        figure.add_trace(go.Scatter(
            x=[clock.length], y=[runway.end_mid], mode="markers+text", showlegend=False,
            marker=dict(color=theme.ACCENT, size=11, line=dict(color="#fff", width=2.5)),
            text=[f"${runway.end_mid:,.0f}"], textposition="middle right",
            textfont=dict(color=theme.ACCENT_DIM, size=14), hoverinfo="skip", cliponaxis=False,
        ))

    actual = runway.actual
    figure.add_trace(go.Scatter(
        x=actual["Day"], y=actual["Balance"], mode="lines", name="Actual",
        line=dict(color=theme.INK, width=2.5), fill="tozeroy",
        fillcolor=_translucent(theme.INK, 0.05),
        hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>Balance</extra>",
    ))
    figure.add_trace(go.Scatter(
        x=[clock.day], y=[runway.today_balance], mode="markers", showlegend=False,
        marker=dict(color=theme.INK, size=11, line=dict(color="#fff", width=2.5)), hoverinfo="skip",
    ))
    if not clock.finished:
        figure.add_vline(
            x=clock.day, line=dict(color=theme.INK, width=1, dash="dot"),
            annotation_text="Today", annotation_position="top", annotation_font=dict(color=theme.INK, size=12),
        )
    figure.add_hline(
        y=reserve, line=dict(color=theme.ACCENT, width=1.5, dash="dot"), opacity=0.7,
        annotation_text=f"Reserve {_short_money(reserve)}", annotation_position="bottom right",
        annotation_font=dict(color=theme.ACCENT_DIM, size=12),
    )
    figure.update_layout(
        height=300, margin=dict(l=8, r=70, t=28, b=8),
        xaxis=_day_axis(clock.start, clock.length), yaxis=_money_axis(rangemode="tozero"),
        legend=dict(x=1, xanchor="right", y=1.08),
    )
    return figure


def spend_pace(actual: pd.DataFrame, prior: pd.DataFrame | None, clock, *, budget: float,
               prior_label: str | None = None) -> go.Figure:
    """Cumulative spending against a straight budget-pace line and last year's curve."""
    if actual.empty:
        return empty_figure("No spending yet.")
    figure = go.Figure()
    if prior is not None and not prior.empty and prior_label:
        figure.add_trace(go.Scatter(
            x=prior["Day"], y=prior["Value"], mode="lines", name=prior_label,
            line=dict(color=theme.HISTORY[1], width=2),
            hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>" + prior_label + "</extra>",
        ))
    if budget > 0:
        figure.add_trace(go.Scatter(
            x=[0, clock.length], y=[0, budget], mode="lines", name="Budget pace",
            line=dict(color=theme.INK_MUTE, width=1.5, dash="dash"), hoverinfo="skip",
        ))
    figure.add_trace(go.Scatter(
        x=actual["Day"], y=actual["Value"], mode="lines", name="This term",
        line=dict(color=theme.INK, width=2.5),
        hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>This term</extra>",
    ))
    last = actual.iloc[-1]
    figure.add_trace(go.Scatter(
        x=[last["Day"]], y=[last["Value"]], mode="markers+text", showlegend=False,
        marker=dict(color=theme.INK, size=9, line=dict(color="#fff", width=2)),
        text=[f"${last['Value']:,.0f}"], textposition="top left",
        textfont=dict(color=theme.INK, size=13), hoverinfo="skip",
    ))
    figure.update_layout(
        height=230, xaxis=_day_axis(clock.start, clock.length), yaxis=_money_axis(rangemode="tozero"),
        legend=dict(y=-0.28, yanchor="top"), margin=dict(l=8, r=8, t=8, b=8),
    )
    return figure


def committee_pace(df: pd.DataFrame, clock, *, rows: int = 3) -> go.Figure:
    """
    Pace dots: a grey dot where the committee should be (% of term elapsed), a
    coloured dot where it is (% of budget used), joined by a segment.
    """
    if df.empty:
        return empty_figure("No committee budgets set for this term.")
    data = df.head(rows).iloc[::-1]
    expected = clock.fraction * 100
    names = data["Committee_Name"].tolist()
    colors = [theme.STATUS_COLORS.get(status, theme.ON_TRACK) for status in data["Status"]]

    xs, ys = [], []
    for name, used in zip(names, data["Used"]):
        xs += [expected, used, None]
        ys += [name, name, None]

    labels = []
    for used, runs_out in zip(data["Used"], data["Runs out"]):
        if used > 100:
            labels.append(f"{used:.0f}% · over")
        elif runs_out is not None and not pd.isna(runs_out):
            labels.append(f"{used:.0f}% · out {pd.Timestamp(runs_out):%b %d}")
        else:
            labels.append(f"{used:.0f}%")

    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines", line=dict(color=theme.GRID, width=8), hoverinfo="skip", showlegend=False,
    ))
    figure.add_trace(go.Scatter(
        x=[expected] * len(names), y=names, mode="markers", showlegend=False,
        marker=dict(color=theme.INK, symbol="line-ns", size=18, line=dict(color=theme.INK, width=2)),
        hovertemplate="Term %{x:.0f}% through<extra></extra>",
    ))
    figure.add_trace(go.Scatter(
        x=data["Used"], y=names, mode="markers+text", showlegend=False,
        marker=dict(color=colors, size=15, line=dict(color="#fff", width=3)),
        text=labels, textposition="middle right", textfont=dict(color=theme.INK, size=12.5),
        hovertemplate="<b>%{y}</b><br>%{x:.0f}% of budget used<extra></extra>", cliponaxis=False,
    ))
    top = max(120.0, float(data["Used"].max()) + 10)
    figure.update_layout(
        height=max(170, 60 + rows * 46), margin=dict(l=8, r=110, t=8, b=8),
        xaxis=dict(range=[0, top], ticksuffix="%", tickvals=[0, round(expected), 100],
                   showgrid=False, fixedrange=True),
        yaxis=dict(showgrid=False, fixedrange=True, tickfont=dict(color=theme.INK, size=13)),
    )
    return figure


def dues_curve(current: pd.DataFrame, prior: pd.DataFrame | None, clock, *,
               prior_label: str | None = None) -> go.Figure:
    """Cumulative dues collected by day, this term against the same term last year."""
    if current.empty and (prior is None or prior.empty):
        return empty_figure("No dues payments yet.")
    figure = go.Figure()
    if prior is not None and not prior.empty and prior_label:
        figure.add_trace(go.Scatter(
            x=prior["Day"], y=prior["Cumulative"], mode="lines", line_shape="hv", name=prior_label,
            line=dict(color=theme.HISTORY[1], width=2),
            hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>" + prior_label + "</extra>",
        ))
    if not current.empty:
        x = list(current["Day"]) + [clock.day]
        y = list(current["Cumulative"]) + [float(current["Cumulative"].iloc[-1])]
        figure.add_trace(go.Scatter(
            x=x, y=y, mode="lines", line_shape="hv", name="This term",
            line=dict(color=theme.INK, width=2.5), fill="tozeroy",
            fillcolor=_translucent(theme.INK, 0.05),
            hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>This term</extra>",
        ))
    figure.update_layout(
        height=170, margin=dict(l=4, r=4, t=4, b=4), showlegend=False,
        xaxis=_day_axis(clock.start, clock.length), yaxis=_money_axis(rangemode="tozero", nticks=3),
    )
    return figure


# --- Dashboard ---------------------------------------------------------------

def budget_health(df: pd.DataFrame, clock) -> go.Figure:
    """% of budget used per committee, with a marker for % of term elapsed."""
    if df.empty:
        return empty_figure("No budget data for this selection.")
    data = df.sort_values("Used", ascending=True)
    shown = data["Used"].clip(upper=130)
    colors = [theme.STATUS_COLORS.get(status, theme.ON_TRACK) for status in data["Status"]]
    text = [
        f"{used:.0f}%  ·  ${spent:,.0f} of ${budget:,.0f}"
        for used, spent, budget in zip(data["Used"], data["Spent"], data["Budget"])
    ]
    runs = [
        "Over budget" if used > 100 else (f"Runs out {pd.Timestamp(r):%b %d}" if r is not None and not pd.isna(r) else "Lasts the term")
        for used, r in zip(data["Used"], data["Runs out"])
    ]
    figure = go.Figure(go.Bar(
        y=data["Committee_Name"], x=shown, orientation="h", marker=dict(color=colors),
        width=0.5, text=text, textposition="outside", textfont=dict(color=theme.INK_SOFT, size=12),
        customdata=runs, cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x:.0f}% used<br>%{customdata}<extra></extra>",
    ))
    expected = clock.fraction * 100
    figure.add_vline(x=100, line=dict(color=theme.GRID, width=1.5))
    figure.add_vline(
        x=expected, line=dict(color=theme.INK, width=1.5),
        annotation_text=f"Term {expected:.0f}%", annotation_position="top",
        annotation_font=dict(color=theme.INK, size=12),
    )
    figure.update_layout(
        height=_height(len(data), per_row=30, base=60), showlegend=False,
        margin=dict(l=8, r=150, t=28, b=8),
        xaxis=dict(range=[0, 130], ticksuffix="%", tickvals=[0, 50, 100], showgrid=False, fixedrange=True),
        yaxis=dict(showgrid=False, fixedrange=True, tickfont=dict(color=theme.INK_SOFT, size=13)),
    )
    return figure


def waterfall(
    labels: list[str],
    values: list[float],
    measures: list[str],
    *,
    title: str | None = None,
) -> go.Figure:
    """Income steps up in ink, spending steps down in grey, the net in orange."""
    if not labels:
        return empty_figure("Not enough data for a waterfall.")
    text = [
        _short_money(v) if m != "total" else "" for v, m in zip(values, measures)
    ]
    # Bars are placed by position, not by label. A label can repeat -- "Formal"
    # is both an income purpose and a committee -- and Plotly stacks repeated
    # category labels into one slot, which drew a spending step in the middle
    # of the income run.
    positions = list(range(len(labels)))
    figure = go.Figure(go.Waterfall(
        orientation="v", measure=measures, x=positions, y=values,
        text=text, textposition="outside", textfont=dict(color=theme.INK_SOFT, size=12),
        connector=dict(line=dict(color=theme.ON_TRACK, width=1, dash="dot")),
        increasing=dict(marker=dict(color=theme.INK)),
        decreasing=dict(marker=dict(color=theme.ON_TRACK)),
        totals=dict(marker=dict(color=theme.ACCENT)),
        customdata=labels,
        hovertemplate="<b>%{customdata}</b><br>$%{y:,.2f}<extra></extra>",
        cliponaxis=False,
    ))
    figure.update_layout(
        height=330, showlegend=False,
        yaxis=_money_axis(),
        xaxis=dict(
            title=None, fixedrange=True, tickfont=dict(size=11.5),
            tickmode="array", tickvals=positions, ticktext=labels,
        ),
    )
    if title:
        figure.update_layout(title=title)
    return figure


def flow_waterfall(steps: list[tuple[str, float, str]]) -> go.Figure:
    """`waterfall` fed from `domain.pace.flow_steps`."""
    if not steps:
        return empty_figure("No transactions in this selection.")
    labels = [label for label, _, _ in steps]
    values = [0.0 if kind == "net" else value for _, value, kind in steps]
    measures = ["total" if kind == "net" else "relative" for _, _, kind in steps]
    figure = waterfall(labels, values, measures)
    net = steps[-1][1]
    figure.add_annotation(
        x=len(labels) - 1, y=max(net, 0), text=_short_money(net), showarrow=False, yshift=12,
        font=dict(color=theme.ACCENT_DIM, size=13),
    )
    return figure


def net_by_week(series: list[tuple[str, pd.DataFrame]], clock) -> go.Figure:
    """Cumulative net by day. First series is this term (ink); the rest are past terms (greys)."""
    if not series or series[0][1].empty:
        return empty_figure("No transactions yet.")
    figure = go.Figure()
    for index, (label, frame) in reversed(list(enumerate(series))):
        if frame.empty:
            continue
        current = index == 0
        color = theme.INK if current else theme.HISTORY[min(index - 1, len(theme.HISTORY) - 1)]
        figure.add_trace(go.Scatter(
            x=frame["Day"], y=frame["Value"], mode="lines", name=label,
            line=dict(color=color, width=2.5 if current else 2),
            hovertemplate="Day %{x}<br>$%{y:,.0f}<extra>" + label + "</extra>",
        ))
        end = frame.iloc[-1]
        figure.add_trace(go.Scatter(
            x=[end["Day"]], y=[end["Value"]], mode="markers+text" if current else "text",
            marker=dict(color=color, size=9, line=dict(color="#fff", width=2)),
            text=[f"{label} {_short_money(end['Value'])}"], textposition="middle right",
            textfont=dict(color=theme.INK if current else theme.INK_MUTE, size=12),
            showlegend=False, hoverinfo="skip", cliponaxis=False,
        ))
    length = max(int(frame["Day"].max()) for _, frame in series if not frame.empty)
    if not clock.finished:
        figure.add_vline(x=clock.day, line=dict(color=theme.INK, width=1, dash="dot"))
    figure.add_hline(y=0, line=dict(color=theme.ON_TRACK, width=1))
    figure.update_layout(
        height=260, margin=dict(l=8, r=110, t=8, b=8), showlegend=False,
        xaxis=_day_axis(clock.start, max(length, clock.length)), yaxis=_money_axis(),
    )
    return figure


# --- Kept from the dark theme, restyled by the template ----------------------

def budget_bullet(df: pd.DataFrame) -> go.Figure:
    """Budget health in dollars: allocation (light) behind spend (by status)."""
    if df.empty:
        return empty_figure("No budget data for this selection.")
    data = df.sort_values("Spent", ascending=True)
    names = data["Committee_Name"].tolist()
    figure = go.Figure()
    figure.add_trace(go.Bar(
        name="Budget", y=names, x=data["Budget"], orientation="h",
        marker=dict(color=theme.BUDGET), width=0.62,
        hovertemplate="<b>%{y}</b><br>Budget: $%{x:,.2f}<extra></extra>",
    ))
    figure.add_trace(go.Bar(
        name="Spent", y=names, x=data["Spent"], orientation="h",
        marker=dict(color=[theme.STATUS_COLORS.get(s, theme.UNBUDGETED) for s in data["Status"]]),
        width=0.3, customdata=data[["% Spent", "Budget", "Status"]].values,
        hovertemplate=(
            "<b>%{y}</b><br>Spent: $%{x:,.2f}<br>Budget: $%{customdata[1]:,.2f}<br>"
            "Used: %{customdata[0]:.1f}%<br>Status: %{customdata[2]}<extra></extra>"
        ),
    ))
    figure.update_layout(
        barmode="overlay", height=_height(len(data)),
        xaxis=dict(title=None, tickprefix="$", separatethousands=True), yaxis=dict(title=None),
    )
    return figure


def ranked_bar(
    df: pd.DataFrame,
    *,
    label_column: str,
    value_column: str,
    color: str,
    title: str | None = None,
    max_rows: int = 12,
) -> go.Figure:
    """Sorted horizontal bars."""
    if df.empty:
        return empty_figure("Nothing to show for this selection.")
    data = df.sort_values(value_column, ascending=False).head(max_rows)
    data = data.sort_values(value_column, ascending=True)
    total = float(df[value_column].sum()) or 1.0
    share = data[value_column] / total * 100
    figure = go.Figure(go.Bar(
        y=data[label_column], x=data[value_column], orientation="h", marker=dict(color=color),
        text=[f"${value:,.0f}" for value in data[value_column]], textposition="outside",
        textfont=dict(color=theme.INK_SOFT, size=12), customdata=share,
        hovertemplate="<b>%{y}</b><br>$%{x:,.2f}<br>%{customdata:.1f}% of total<extra></extra>",
    ))
    figure.update_layout(
        height=_height(len(data)), xaxis=dict(title=None, tickprefix="$", separatethousands=True),
        yaxis=dict(title=None), showlegend=False,
    )
    # Only set a title when there is one -- None renders as the string "undefined".
    if title:
        figure.update_layout(title=title)
    figure.update_xaxes(range=[0, float(data[value_column].max()) * 1.18])
    return figure


def sparkline(values: list[float], *, color: str | None = None) -> go.Figure:
    """Mini bars under a metric: earlier values faded, the last one solid."""
    color = color or theme.INK
    if not values:
        return empty_figure("")
    faded = _translucent(color, 0.28) if color.startswith("#") else theme.UNBUDGETED
    figure = go.Figure(go.Bar(
        y=values, marker=dict(color=[faded] * (len(values) - 1) + [color]), hoverinfo="skip",
    ))
    figure.update_layout(
        height=48, margin=dict(l=0, r=0, t=4, b=0), showlegend=False, bargap=0.3,
        xaxis=dict(visible=False, fixedrange=True), yaxis=dict(visible=False, fixedrange=True),
    )
    return figure


def trend_bars(df: pd.DataFrame) -> go.Figure:
    """
    Budget against actual across semesters. A semester that went over its
    budget draws its actual bar in orange; theme.BUDGET is too close to the
    canvas to read as a bar, so the budget uses the darker edge grey.
    """
    if df.empty:
        return empty_figure("No historical data yet.")
    p = theme.active()
    over = df["Spent"] > df["Budget"]
    figure = go.Figure()
    figure.add_trace(go.Bar(
        name="Budget", x=df["Semester"], y=df["Budget"], marker=dict(color=p.line_edge),
        hovertemplate="Budget: $%{y:,.2f}<extra></extra>",
    ))
    figure.add_trace(go.Bar(
        name="Actual", x=df["Semester"], y=df["Spent"],
        marker=dict(color=[p.over if is_over else p.ink for is_over in over]),
        hovertemplate="Spent: $%{y:,.2f}<extra></extra>",
    ))
    if over.any():
        # Legend-only entry so the orange bars explain themselves.
        figure.add_trace(go.Bar(
            name="Over budget", x=[None], y=[None], marker=dict(color=p.over), hoverinfo="skip",
        ))
    figure.update_layout(
        barmode="group", height=340,
        yaxis=dict(title=None, tickprefix="$", separatethousands=True), xaxis=dict(title=None),
    )
    return figure


def category_treemap(df: pd.DataFrame, *, label_column: str, value_column: str) -> go.Figure:
    if df.empty:
        return empty_figure("Nothing to show for this selection.")
    figure = go.Figure(go.Treemap(
        labels=df[label_column], parents=[""] * len(df), values=df[value_column],
        marker=dict(colors=theme.CATEGORICAL * (len(df) // len(theme.CATEGORICAL) + 1)),
        texttemplate="<b>%{label}</b><br>$%{value:,.0f}",
        hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<extra></extra>",
    ))
    figure.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0))
    return figure


def sponsorship_trend_bars(df: pd.DataFrame) -> go.Figure:
    """
    Sponsorship goal against amount raised, across semesters.

    Same grouped-bar shape and Pebble colours as `trend_bars`: the goal in the
    budget tone, the amount raised in the income tone. A semester with no goal
    set simply has no goal bar for that x position -- Plotly skips a NaN value
    rather than drawing a $0 bar, which is the correct reading: "no target",
    not "target of zero".
    """
    if df.empty:
        return empty_figure("No sponsorship history yet.")

    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            name="Goal",
            x=df["Semester"],
            y=df["Goal"],
            marker=dict(color=theme.BUDGET),
            hovertemplate="Goal: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Bar(
            name="Raised",
            x=df["Semester"],
            y=df["Raised"],
            marker=dict(color=theme.INCOME),
            hovertemplate="Raised: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        barmode="group",
        height=380,
        yaxis=dict(title=None, tickprefix="$", separatethousands=True),
        xaxis=dict(title=None, showgrid=False),
    )
    return figure


def confidence_histogram(confidences: list[float]) -> go.Figure:
    """How certain the categorizer was, for the review queue."""
    if not confidences:
        return empty_figure("Nothing categorized yet.")
    figure = go.Figure(go.Histogram(
        x=confidences, nbinsx=10, marker=dict(color=theme.INK),
        hovertemplate="Confidence %{x}<br>%{y} transactions<extra></extra>",
    ))
    figure.update_layout(
        height=220, xaxis=dict(title="Match confidence", range=[0, 1]),
        yaxis=dict(title="Transactions"), showlegend=False,
    )
    return figure
