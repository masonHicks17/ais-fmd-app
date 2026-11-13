"""
A month calendar in HTML, shared by Events and My Plan.

Callers turn their rows into `Chip`s (title, a small line under it, a colour)
and this draws the weeks. A planned event is drawn dashed, so a VP can tell
what is on the club calendar from what they are only planning.

Seven columns don't fit a phone: every event shrank to a letter. So the same
chips are also drawn as a list grouped by day, and CSS shows the list instead
of the grid on narrow screens (2026-11-09).

`editable=True` (2026-11-13) makes it work like a calendar app through
ui/clickable.py: each day carries `data-day`, so a double-click (or its "+")
asks to add an event there, and a `movable` chip can be dragged to another day.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from html import escape

from . import theme


@dataclass(frozen=True)
class Chip:
    title: str
    meta_html: str = ""   # already-escaped HTML for the line under the title
    color: str | None = None   # None: the theme's muted grey
    tip: str = ""
    planned: bool = False
    pick: str | None = None   # set: the chip is clickable and reports this (ui/clickable.py)
    movable: bool = False     # with `pick` and an editable grid: can be dragged to another day


def css() -> str:
    """The calendar stylesheet, in this session's light or dark tokens."""
    t = theme.tokens()
    return f"""
<style>
.ais-cal {{ display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:6px; margin-top:.5rem; }}
.ais-cal-dow {{ font-size:.72rem; color:{t.INK_MUTE}; text-transform:uppercase; letter-spacing:.04em; padding:0 6px; }}
.ais-cal-day {{ background:{t.CARD}; border-radius:12px; min-height:104px; padding:6px; box-shadow:{t.SHADOW};
  display:flex; flex-direction:column; gap:4px; overflow:hidden; }}
.ais-cal-day.out {{ background:transparent; box-shadow:none; }}
.ais-cal-day.today {{ outline:2px solid {t.ACCENT}; }}
.ais-cal-num {{ font-size:.75rem; color:{t.INK_SOFT}; font-weight:600; }}
.ais-cal-ev {{ font-size:.74rem; line-height:1.2; padding:3px 6px; border-radius:6px; background:{t.SUBTLE};
  border-left:3px solid var(--c); color:{t.INK}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.ais-cal-ev.past {{ opacity:.82; }}
.ais-cal-ev.planned {{ background:transparent; border:1.5px dashed var(--c); border-left-width:3px; }}
.ais-cal-ev b {{ font-weight:600; }}
.ais-cal-ev[data-pick] {{ cursor:pointer; transition:background .12s ease; }}
.ais-cal-ev[data-pick]:hover {{ background:{t.GRID}; }}
.ais-cal-ev[data-pick]:focus-visible {{ outline:2px solid {t.ACCENT}; outline-offset:1px; }}
.ais-cal-meta {{ display:block; color:{t.INK_MUTE}; font-size:.68rem; }}
.ais-cal-ev .spent {{ color:{t.ACCENT_DIM}; font-weight:600; }}
.ais-cal-ev .est {{ color:{t.INK_SOFT}; }}
.ais-cal-hint {{ font-size:.78rem; color:{t.INK_MUTE}; }}
.ais-cal-top {{ display:flex; align-items:center; justify-content:space-between; min-height:20px; }}
.ais-cal-add {{ width:20px; height:20px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:15px; line-height:1; color:{t.INK_SOFT}; cursor:pointer; opacity:0;
  transition:opacity .12s ease, background .12s ease; }}
.ais-cal-day:hover .ais-cal-add, .ais-cal-add:focus-visible {{ opacity:1; }}
.ais-cal-add:hover {{ background:{t.ACCENT}; color:#fff; }}
.ais-cal.editable .ais-cal-day[data-day] {{ cursor:default; user-select:none; }}
.ais-cal-ev[draggable="true"] {{ cursor:grab; }}
.ais-cal-ev.dragging {{ opacity:.4; }}
.ais-cal-day.drop {{ outline:2px dashed {t.ACCENT}; outline-offset:-2px; background:{t.ACCENT_TINT}; }}
@media (hover: none) {{ .ais-cal-add {{ opacity:1; }} }}
.ais-agenda {{ display:none; margin-top:.5rem; }}
.ais-agenda-day {{ display:flex; gap:12px; padding:10px 12px; background:{t.CARD}; border-radius:12px;
  box-shadow:{t.SHADOW}; margin-bottom:8px; }}
.ais-agenda-day.today {{ outline:2px solid {t.ACCENT}; }}
.ais-agenda-date {{ flex:0 0 44px; text-align:center; color:{t.INK_SOFT}; line-height:1.1; }}
.ais-agenda-date b {{ display:block; font-size:1.25rem; color:{t.INK}; }}
.ais-agenda-date span {{ font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; }}
.ais-agenda-events {{ flex:1; min-width:0; display:flex; flex-direction:column; gap:6px; }}
.ais-agenda .ais-cal-ev {{ white-space:normal; font-size:.85rem; padding:6px 8px; }}
.ais-agenda .ais-cal-meta {{ font-size:.75rem; }}
.ais-agenda-empty {{ color:{t.INK_MUTE}; font-size:.85rem; padding:4px 2px; }}
@media (max-width: 640px) {{
  .ais-cal {{ display:none; }}
  .ais-agenda {{ display:block; }}
}}
</style>
"""


CSS = css()  # the light stylesheet, for code that imported the constant


def _chip_html(chip: Chip, day: date, today: date, *, editable: bool = False) -> str:
    kind = " planned" if chip.planned else (" past" if day < today else "")
    color = chip.color or theme.tokens().INK_MUTE
    pick = f' data-pick="{escape(chip.pick)}" role="button" tabindex="0"' if chip.pick else ""
    if editable and chip.pick and chip.movable:
        pick += f' draggable="true" data-drag="{escape(chip.pick)}" data-from="{day.isoformat()}"'
    return (
        f'<div class="ais-cal-ev{kind}" style="--c:{color}" title="{escape(chip.tip)}"{pick}>'
        f'<b>{escape(chip.title)}</b><span class="ais-cal-meta">{chip.meta_html or "&nbsp;"}</span></div>'
    )


def agenda(year: int, month: int, chips: dict[date, list[Chip]], *, today: date | None = None) -> str:
    """The month as a list of days that have something on, for phones."""
    today = today or date.today()
    days = sorted(d for d, items in chips.items() if items and d.year == year and d.month == month)
    if not days:
        return '<div class="ais-agenda"><div class="ais-agenda-empty">Nothing on this month.</div></div>'
    rows = []
    for day in days:
        classes = "ais-agenda-day" + (" today" if day == today else "")
        rows.append(
            f'<div class="{classes}"><div class="ais-agenda-date"><span>{day:%a}</span><b>{day.day}</b></div>'
            f'<div class="ais-agenda-events">{"".join(_chip_html(c, day, today) for c in chips[day])}</div></div>'
        )
    return f'<div class="ais-agenda">{"".join(rows)}</div>'


def month_grid(
    year: int, month: int, chips: dict[date, list[Chip]], *, today: date | None = None, editable: bool = False,
) -> str:
    """
    The month as a grid; on a phone, CSS swaps in the day list drawn alongside it.

    `editable`: days take a double-click or their "+" to add an event
    (`add:<day>`), and movable chips drag to another day (`move:<pick>:<day>`).
    """
    today = today or date.today()
    cells = [f'<div class="ais-cal-dow">{name}</div>' for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    for week in calendar.Calendar().monthdatescalendar(year, month):
        for day in week:
            inside = day.month == month
            classes = ["ais-cal-day"] + ([] if inside else ["out"]) + (["today"] if day == today else [])
            drawn = []
            for chip in chips.get(day, []) if inside else []:
                drawn.append(_chip_html(chip, day, today, editable=editable))
            number = f'<span class="ais-cal-num">{day.day if inside else ""}</span>'
            target = ""
            if editable and inside:
                target = f' data-day="{day.isoformat()}"'
                number = (
                    f'<div class="ais-cal-top">{number}<span class="ais-cal-add" data-pick="add:{day.isoformat()}" '
                    f'role="button" tabindex="0" title="Add an event on {day:%b %-d}" '
                    f'aria-label="Add an event on {day:%b %-d}">+</span></div>'
                )
            cells.append(f'<div class="{" ".join(classes)}"{target}>{number}{"".join(drawn)}</div>')
    grid_class = "ais-cal editable" if editable else "ais-cal"
    return css() + f'<div class="{grid_class}">{"".join(cells)}</div>' + agenda(year, month, chips, today=today)
