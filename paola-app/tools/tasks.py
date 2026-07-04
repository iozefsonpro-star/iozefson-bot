"""Инструменты агента: задачи в Notion (общая база с Second Brain)."""
from datetime import datetime

import config
from services import notion


def _line(t: dict) -> str:
    icon = notion.PRIORITY_ICONS.get(t.get("priority", ""), "⚪")
    dl   = f" — до {t['deadline']}" if t.get("deadline") else ""
    zone = f" [{t['zone']}]" if t.get("zone") else ""
    return f"{icon} {t['title']}{dl}{zone}"


async def _list_tasks(inp: dict) -> str:
    scope = inp.get("scope", "active")
    if scope == "overdue":
        tasks = await notion.get_overdue_tasks()
        header = "Просроченные задачи"
    elif scope == "today":
        today = datetime.now(config.ROME_TZ).strftime("%Y-%m-%d")
        tasks = [t for t in await notion.get_active_tasks()
                 if t.get("deadline", "")[:10] == today]
        header = "Задачи на сегодня"
    else:
        tasks = await notion.get_active_tasks()
        header = "Активные задачи"
    if not tasks:
        return f"{header}: нет."
    tasks = notion.sort_by_priority(tasks)
    return f"{header} ({len(tasks)}):\n" + "\n".join(
        f"[id:{t['id']}] {_line(t)}" for t in tasks)


async def _create_task(inp: dict) -> str:
    page = await notion.create_task(
        title=inp["title"],
        deadline=inp.get("deadline") or None,
        priority=inp.get("priority", "✅ Обычное"),
        zone=inp.get("zone", "💼 Бизнес"),
        comment=inp.get("comment") or None,
    )
    dl = f", дедлайн {inp['deadline']}" if inp.get("deadline") else ""
    return f"Задача создана: «{inp['title']}»{dl} (id:{page['id']})"


async def _close_task(inp: dict) -> str:
    await notion.close_task(inp["task_id"])
    return "Задача закрыта (Done)."


async def _reschedule_task(inp: dict) -> str:
    await notion.reschedule_task(inp["task_id"], inp["new_date"])
    return f"Дедлайн перенесён на {inp['new_date']}."


ZONES = ["💼 Бизнес", "👥 Networking", "🏥 Salute", "🧠 Mental", "🏠 Family",
         "💅 Beauty", "✈️ Viaggi", "📚 Ресурсы", "✍️ Контент", "💰 Финансы"]

TOOLS = [
    {
        "schema": {
            "name": "list_tasks",
            "description": "Показать задачи Юлии из Notion. Используй перед закрытием или "
                           "переносом задачи, чтобы получить её id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["active", "today", "overdue"],
                              "description": "active — все активные; today — с дедлайном "
                                             "сегодня; overdue — просроченные"},
                },
                "required": [],
            },
        },
        "handler": _list_tasks,
    },
    {
        "schema": {
            "name": "create_task",
            "description": "Создать задачу в Notion. Вызывай, когда Юлия просит что-то "
                           "записать, запланировать или не забыть сделать.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title":    {"type": "string", "description": "Название: глагол + суть"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD, если назван срок"},
                    "priority": {"type": "string",
                                 "enum": ["❗ Важное", "✅ Обычное", "🔜 Когда-нибудь"]},
                    "zone":     {"type": "string", "enum": ZONES},
                    "comment":  {"type": "string", "description": "Дополнительный контекст"},
                },
                "required": ["title"],
            },
        },
        "handler": _create_task,
    },
    {
        "schema": {
            "name": "close_task",
            "description": "Закрыть задачу (статус Done). Сначала найди её id через list_tasks.",
            "input_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
        "handler": _close_task,
    },
    {
        "schema": {
            "name": "reschedule_task",
            "description": "Перенести дедлайн задачи. Сначала найди её id через list_tasks.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id":  {"type": "string"},
                    "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["task_id", "new_date"],
            },
        },
        "handler": _reschedule_task,
    },
]
