"""Инструмент агента: события Google Calendar."""
from collections import defaultdict

from services import gcal


async def _get_calendar(inp: dict) -> str:
    days = min(int(inp.get("days", 0)), 30)
    events = await gcal.get_events(days=days, from_now=bool(inp.get("from_now", False)))
    if not events:
        return "Событий в календаре нет (или календарь недоступен)."
    by_day: dict[str, list] = defaultdict(list)
    for ev in events:
        by_day[ev["day"]].append(ev)
    lines = []
    for day in sorted(by_day):
        lines.append(f"📅 {day}:")
        lines.extend(f"  {gcal.format_event(ev)}" for ev in by_day[day])
    return "\n".join(lines)


TOOLS = [
    {
        "schema": {
            "name": "get_calendar",
            "description": "События из Google Calendar Юлии. days=0 — только сегодня, "
                           "days=7 — неделя вперёд.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days":     {"type": "integer", "description": "Сколько дней вперёд (0–30)"},
                    "from_now": {"type": "boolean",
                                 "description": "true — только будущие события (прошедшие сегодня скрыть)"},
                },
                "required": [],
            },
        },
        "handler": _get_calendar,
    },
]
