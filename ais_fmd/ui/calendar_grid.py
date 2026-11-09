"""
A month calendar in HTML, shared by Events and My Plan.

Callers turn their rows into `Chip`s (title, a small line under it, a colour)
and this draws the weeks. A planned event is drawn dashed, so a VP can tell
what is on the club calendar from what they are only planning.
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
    color: str = theme.INK_MUTE
    tip: str = ""
    planned: bool = False


CSS = f"""
<style>
.ais-cal {{ display:grid; grid-template-columns:repeat(7, minmax(0,1fr)); gap:6px; margin-top:.5rem; }}
.ais-cal-dow {{ font-size:.72rem; color:{theme.INK_MUTE}; text-transform:uppercase; letter-spacing:.04em; padding:0 6px; }}
.ais-cal-day {{ background:{theme.CARD}; border-radius:12px; min-height:104px; padding:6px; box-shadow:{theme.SHADOW};
  display:flex; flex-direction:column; gap:4px; overflow:hidden; }}
.ais-cal-day.out {{ background:transparent; box-shadow:none; }}
.ais-cal-day.today {{ outline:2px solid {theme.ACCENT}; }}
.ais-cal-num {{ font-size:.75rem; color:{theme.INK_SOFT}; font-weight:600; }}
.ais-cal-ev {{ font-size:.74rem; line-height:1.2; padding:3px 6px; border-radius:6px; background:{theme.SUBTLE};
  border-left:3px solid var(--c); color:{theme.INK}; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.ais-cal-ev.past {{ opacity:.82; }}
.ais-cal-ev.planned {{ background:transparent; border:1.5px dashed var(--c); border-left-width:3px; }}
.ais-cal-ev b {{ font-weight:600; }}
.ais-cal-meta {{ display:block; color:{theme.INK_MUTE}; font-size:.68rem; }}
.ais-cal-ev .spent {{ color:{theme.ACCENT_DIM}; font-weight:600; }}
</style>
"""


def month_grid(year: int, month: int, chips: dict[date, list[Chip]], *, today: date | None = None) -> str:
    today = today or date.today()
    cells = [f'<div class="ais-cal-dow">{name}</div>' for name in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")]
    for week in calendar.Calendar().monthdatescalendar(year, month):
        for day in week:
            inside = day.month == month
            classes = ["ais-cal-day"] + ([] if inside else ["out"]) + (["today"] if day == today else [])
            drawn = []
            for chip in chips.get(day, []) if inside else []:
                kind = " planned" if chip.planned else (" past" if day < today else "")
                drawn.append(
                    f'<div class="ais-cal-ev{kind}" style="--c:{chip.color}" title="{escape(chip.tip)}">'
                    f'<b>{escape(chip.title)}</b><span class="ais-cal-meta">{chip.meta_html or "&nbsp;"}</span></div>'
                )
            number = day.day if inside else ""
            cells.append(f'<div class="{" ".join(classes)}"><span class="ais-cal-num">{number}</span>{"".join(drawn)}</div>')
    return CSS + f'<div class="ais-cal">{"".join(cells)}</div>'
