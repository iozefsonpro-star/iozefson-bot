"""Инструменты агента: трекер привычек (базы «Привычки» и «Журнал привычек» в Notion)."""
from datetime import datetime

import config
from services import notion


async def _habits_status(inp: dict) -> str:
    habits = await notion.get_habits()
    if not habits:
        return ("Активных привычек нет. Добавь привычки в базу Notion «Привычки» "
                "или скажи мне — я не создаю привычки сама, только отмечаю выполнение.")
    entries = await notion.get_habit_log(days=60)
    today = datetime.now(config.ROME_TZ).date()
    today_iso = today.isoformat()
    done_today = {e["habit"] for e in entries if e["date"] == today_iso and e["done"]}
    lines = [f"Привычки на {today_iso}:"]
    for h in habits:
        streak = notion.habit_streak(entries, h["name"], today)
        mark = "✅" if h["name"] in done_today else "⬜"
        goal = f" · цель: {h['goal']}" if h.get("goal") else ""
        lines.append(f"{mark} {h['name']} — серия {streak} дн.{goal}")
    return "\n".join(lines)


async def _log_habit(inp: dict) -> str:
    day = inp.get("date") or datetime.now(config.ROME_TZ).date().isoformat()
    done = bool(inp.get("done", True))
    habits = {h["name"] for h in await notion.get_habits()}
    name = inp["habit_name"]
    if name not in habits:
        return (f"Привычка «{name}» не найдена среди активных: "
                f"{', '.join(sorted(habits)) or 'список пуст'}. Уточни название.")
    await notion.log_habit(name, day, done)
    return f"Отмечено: «{name}» за {day} — {'выполнено ✅' if done else 'не выполнено'}."


TOOLS = [
    {
        "schema": {
            "name": "habits_status",
            "description": "Статус привычек на сегодня: что отмечено, текущие серии (streak). "
                           "Вызывай при вопросах о привычках и в вечернем чек-ине.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": _habits_status,
    },
    {
        "schema": {
            "name": "log_habit",
            "description": "Отметить выполнение (или пропуск) привычки за день.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "habit_name": {"type": "string", "description": "Точное название привычки"},
                    "done":       {"type": "boolean", "description": "true — выполнена (по умолчанию)"},
                    "date":       {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
                },
                "required": ["habit_name"],
            },
        },
        "handler": _log_habit,
    },
]
