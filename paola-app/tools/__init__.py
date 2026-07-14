"""Реестр инструментов агента.

Каждый модуль экспортирует TOOLS: list[dict] c ключами:
  schema  — описание инструмента для Claude (name, description, input_schema)
  handler — async функция (dict) -> str
Добавление функции приложению = добавление одного инструмента здесь.
"""
from tools import (tasks, calendar_tools, habits, reminders, translate, board,
                   materials, memory, consuntivo)

ALL_TOOLS = (
    tasks.TOOLS
    + calendar_tools.TOOLS
    + habits.TOOLS
    + reminders.TOOLS
    + translate.TOOLS
    + board.TOOLS
    + materials.TOOLS
    + memory.TOOLS
    + consuntivo.TOOLS
)

SCHEMAS  = [t["schema"] for t in ALL_TOOLS]
HANDLERS = {t["schema"]["name"]: t["handler"] for t in ALL_TOOLS}
