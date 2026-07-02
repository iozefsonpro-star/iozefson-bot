"""Инструменты агента: разовые напоминания (пуш в Telegram в назначенное время)."""
from services import notion


async def _create_reminder(inp: dict) -> str:
    await notion.create_reminder(inp["text"], inp["when"])
    return (f"Напоминание создано: «{inp['text']}» на {inp['when']} (Europe/Rome). "
            f"Придёт пушем в Telegram.")


async def _list_reminders(inp: dict) -> str:
    reminders = await notion.get_pending_reminders()
    if not reminders:
        return "Ожидающих напоминаний нет."
    return "Ожидающие напоминания:\n" + "\n".join(
        f"• {r['when']} — {r['text']}" for r in reminders)


TOOLS = [
    {
        "schema": {
            "name": "create_reminder",
            "description": "Создать разовое напоминание. Юлия получит его пушем в Telegram "
                           "в указанное время. Время всегда Europe/Rome.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст напоминания"},
                    "when": {"type": "string",
                             "description": "Дата и время ISO: YYYY-MM-DDTHH:MM:00"},
                },
                "required": ["text", "when"],
            },
        },
        "handler": _create_reminder,
    },
    {
        "schema": {
            "name": "list_reminders",
            "description": "Список ещё не отправленных напоминаний.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        "handler": _list_reminders,
    },
]
