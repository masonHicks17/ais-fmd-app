"""
HTML panels for a VP's committee page: the vertical spend bar, the stacked
Pareto of payees, and the past-term comparison table.

Drawn as HTML rather than Plotly or `st.dataframe` because each one is mostly
text that has to stay legible: Streamlit's grid renders on a canvas the app's
stylesheet cannot reach, which is why the comparison table used to blend into
the page. Pass the result to `shell.html`, which keeps dollar signs away from
Streamlit's math parser. The CSS lives in `theme.py` with everything else.
"""

from __future__ import annotations

from html import escape

from ais_fmd.domain import vp_history
from ais_fmd.domain.money import format_currency

from . import shell, theme

# Status from `vp_metrics.status_for` -> the tone the tiles and bar use.
TONES = {"on track": "ok", "approaching": "warn", "over": "over"}

# Pareto segments, biggest first: ink fading to grey (`theme.Tokens.SEGMENTS`).
# Orange stays reserved for "over budget", so no payee is ever drawn in the
# warning colour.
def _segment(i: int) -> str:
    segments = theme.tokens().SEGMENTS
    return segments[min(i, len(segments) - 1)]

_PILL_KIND = {"on track": "on track", "approaching": "warn", "over": "over"}


def tone_for(status: str) -> str:
    return TONES.get(status, "muted")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def spend_bar(position: dict, status: str, elapsed: float | None) -> str:
    """A tall bar filled to the share of the budget spent, with the term's progress marked."""
    percent = position["percent"]
    fill = 0.0 if percent is None else max(0.0, min(percent, 100.0))
    pace = (
        f'<div class="ais-vbar-pace" style="bottom:{min(max(elapsed, 0.0), 100.0):.1f}%"></div>'
        if elapsed is not None
        else ""
    )
    term_line = (
        f'<div class="ais-vbar-term">Dashed line: {elapsed:.0f}% of the term has passed</div>'
        if elapsed is not None
        else ""
    )
    title = f"{_pct(percent)} of the budget spent"
    return (
        f'<div class="ais-vbar ais-tone-{tone_for(status)}">'
        f'<div class="ais-vbar-track" title="{title}">'
        f'<div class="ais-vbar-fill" style="height:{fill:.1f}%"></div>{pace}</div>'
        f'<div class="ais-vbar-text">'
        f"<div>{shell.pill(status, _PILL_KIND.get(status, 'muted'))}</div>"
        f'<div class="ais-vbar-pct">{_pct(percent)}</div>'
        f'<div class="ais-vbar-label">of the budget spent</div>'
        f'<div class="ais-vbar-money"><b>{format_currency(position["spent"])}</b> '
        f'of {format_currency(position["budget"])}</div>'
        f"{term_line}</div></div>"
    )


def pareto(rows: list[dict], budget: float, spent: float) -> str:
    """
    One bar across the budget, split by payee biggest-first, with the unspent part
    hatched; underneath, the same payees with their share and the running share.
    When spending passes the budget the bar is scaled to the spending and an
    orange mark shows where the budget ran out.
    """
    scale = max(budget, spent, 0.01)
    segments = "".join(
        f'<span style="width:{row["amount"] / scale * 100:.2f}%;'
        f'background:{_segment(i)}" '
        f'title="{escape(row["label"])}: {format_currency(row["amount"])}"></span>'
        for i, row in enumerate(rows)
    )
    remaining = max(budget - spent, 0.0)
    if remaining > 0:
        segments += (
            f'<span class="ais-stack-rest" style="width:{remaining / scale * 100:.2f}%" '
            f'title="Remaining: {format_currency(remaining)}"></span>'
        )
    if spent > budget > 0:
        segments += f'<div class="ais-stack-mark" style="left:{budget / scale * 100:.2f}%"></div>'
    right_label = (
        f"Budget {format_currency(budget)}"
        if spent <= budget
        else f"Spent {format_currency(spent)} · budget {format_currency(budget)}"
    )

    body = "".join(
        "<tr>"
        f'<td><i class="ais-swatch" style="background:{_segment(i)}"></i></td>'
        f'<td>{escape(row["label"])}</td>'
        f'<td class="num">{format_currency(row["amount"])}</td>'
        f'<td class="num">{_pct(row["share"])}</td>'
        f'<td class="num muted">{_pct(row["cumulative"])}</td>'
        "</tr>"
        for i, row in enumerate(rows)
    )
    rest_row = (
        "<tr>"
        f'<td><i class="ais-swatch ais-stack-rest" style="background:repeating-linear-gradient(135deg,{theme.tokens().SUBTLE} 0 3px,{theme.tokens().BUDGET} 3px 6px)"></i></td>'
        f'<td class="muted">{"Remaining" if spent <= budget else "Over budget by"}</td>'
        f'<td class="num muted">{format_currency(abs(budget - spent))}</td><td></td><td></td>'
        "</tr>"
    )
    return (
        f'<div class="ais-stack">{segments}</div>'
        f'<div class="ais-stack-scale"><span>$0</span><span>{right_label}</span></div>'
        '<table class="ais-table"><thead><tr><th></th><th>Paid to</th>'
        '<th class="num">Spent</th><th class="num">Share</th><th class="num">Running</th>'
        f"</tr></thead><tbody>{body}{rest_row}</tbody></table>"
    )


def _meter(value: float | None, scale: float, *, mark: float | None, color: str | None = None) -> str:
    width = 0.0 if value is None else max(0.0, min(value / scale * 100, 100.0))
    style = f"width:{width:.1f}%" + (f";background:{color}" if color else "")
    marker = (
        f'<div class="ais-meter-mark" style="left:{min(mark / scale * 100, 100.0):.1f}%" '
        'title="Where you are now"></div>'
        if mark is not None
        else ""
    )
    return (
        '<div class="ais-meter"><div class="ais-meter-track">'
        f'<div class="ais-meter-fill" style="{style}"></div>{marker}</div>'
        f'<span class="ais-meter-value">{_pct(value)}</span></div>'
    )


def comparison_table(comparison: vp_history.PastComparison, status: str) -> str:
    """
    This term against the same point in past terms. Each past row's bar carries a
    black mark at where this term is now, so "ahead or behind" reads down the
    column without comparing numbers.
    """
    table = comparison.table
    values = [comparison.current_percent, *table["Of its budget"].tolist()]
    scale = max([100.0] + [v for v in values if v is not None and v == v])
    t = theme.tokens()
    now_color = {"ok": t.POSITIVE, "warn": t.WARNING, "over": t.ACCENT}.get(tone_for(status))
    current = comparison.current_percent

    rows = [
        '<tr class="ais-now">'
        f'<td>{escape(comparison.semester)}<span class="ais-chip ais-chip-now">Now</span></td>'
        f'<td class="num">{format_currency(comparison.current_spent)}</td>'
        f"<td>{_meter(current, scale, mark=None, color=now_color)}</td>"
        '<td class="num muted">in progress</td></tr>'
    ]
    for _, row in table.iterrows():
        chip = '<span class="ais-chip">Same season</span>' if row["Same season"] else ""
        ended = row["Ended the term at"]
        rows.append(
            "<tr>"
            f'<td>{escape(row["Semester"])}{chip}</td>'
            f'<td class="num">{format_currency(row["Spent by this point"])}</td>'
            f'<td>{_meter(row["Of its budget"], scale, mark=current)}</td>'
            f'<td class="num">{_pct(None if ended is None or ended != ended else ended)}</td>'
            "</tr>"
        )
    return (
        '<table class="ais-table ais-compare"><thead><tr><th>Term</th>'
        '<th class="num">Spent by this point</th><th>Of its budget by this point</th>'
        '<th class="num">Ended the term at</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
