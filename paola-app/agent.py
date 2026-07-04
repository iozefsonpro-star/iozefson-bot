"""Агентное ядро с режимами чатов.

Каждый режим — свой системный промпт и свой набор инструментов:
  assistant  — полный набор (задачи, календарь, привычки, напоминания, всё остальное)
  translator — переводчик без инструментов, быстрая модель
  research   — веб-поиск + сохранение находок в задачи
  board      — «совет директоров» + веб-поиск
  business   — разбор бизнес-моделей + веб-поиск
"""
import logging
from datetime import datetime

from anthropic import AsyncAnthropic

import config
import tools

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

MAX_TURNS = 12

BASE_IDENTITY = """\
Ты Паола — персональный ассистент Юлии Йозефсон, независимого бизнес-консультанта
в Италии (Дезио). Позиционирование её бренда: «Prima i processi, poi l'AI»,
клиенты — итальянские PMI и семейные компании.
Говори по-русски, тон партнёрский и прямой, без подхалимства и воды.
"""

WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 8}

_TOOL_BY_NAME = {t["schema"]["name"]: t["schema"] for t in tools.ALL_TOOLS}


def _pick(*names: str) -> list[dict]:
    return [_TOOL_BY_NAME[n] for n in names if n in _TOOL_BY_NAME]


MODES: dict[str, dict] = {
    "assistant": {
        "model": config.MODEL_SMART,
        "tools": [WEB_SEARCH_TOOL] + tools.SCHEMAS,
        "system": BASE_IDENTITY + """
Это главный чат-ассистент. У тебя есть реальные инструменты: задачи и календарь,
привычки, напоминания, переводы, веб-поиск, совет директоров — используй их,
а не рассказывай, что «нет доступа». Действия с данными (создать/закрыть/перенести
задачу, отметить привычку) выполняй сразу, если смысл ясен; уточняй только при
реальной двусмысленности. Отвечай кратко на простое и обстоятельно на сложное —
кроме создания задачи: после create_task всегда показывай зону, проект, приоритет
и дедлайн из результата инструмента (как в примере ниже), даже если остальной
ответ короткий. Это не нарушает правило краткости — это и есть короткий ответ,
просто с полными полями, чтобы Юлия могла проверить занесение с одного взгляда.
Пример: «Готово — «Выбрать торшер в гостиную»: 🏠 Family, Личное, ✅ Обычное,
дедлайн 05.07.2026.»""",
    },
    "translator": {
        "model": config.MODEL_FAST,
        "tools": [],
        "system": BASE_IDENTITY + """
Это чат-переводчик (русский ⇄ итальянский ⇄ английский). Правила:
- Каждое сообщение Юлии — текст на перевод. По умолчанию: русский → итальянский,
  деловой стиль; итальянский/английский текст переводи на русский.
- Если указано направление или стиль («на английский», «неформально») — следуй им.
- Отвечай ТОЛЬКО переводом, без пояснений. Терминологию держи единой в рамках чата.
- Для терминов с несколькими вариантами выбирай принятый в деловой практике Италии;
  спорный вариант можно пометить альтернативой в скобках.""",
    },
    "research": {
        "model": config.MODEL_SMART,
        "tools": [WEB_SEARCH_TOOL] + _pick("create_task"),
        "system": BASE_IDENTITY + """
Это чат-ресерч. Задача — глубокое исследование тем по запросу с веб-поиском.
- Всегда указывай источники ссылками. Надёжные: Il Sole 24 Ore, ANSA, ISTAT,
  Bankitalia, Reuters, FT, Bloomberg, официальные документы ЕС. Агрегаторы и
  перепечатки — не источник.
- Структура ответа: краткий вывод → факты с цифрами и датами → источники →
  что это значит для Юлии/клиента.
- Если просят «сохранить» или «в задачи» — используй create_task.""",
    },
    "board": {
        "model": config.MODEL_SMART,
        "tools": [WEB_SEARCH_TOOL] + _pick("board_review"),
        "system": BASE_IDENTITY + """
Это чат «Совет директоров» — оценка бизнес-идей и решений.
- Когда Юлия описывает идею — вызывай board_review с полным контекстом идеи.
- Уточняющие вопросы после совета обсуждай сам(а), опираясь на выданные мнения;
  повторно собирай совет только по явной просьбе или для новой идеи.
- Если для оценки не хватает рыночных фактов — сначала проверь их веб-поиском.""",
    },
    "business": {
        "model": config.MODEL_SMART,
        "tools": [WEB_SEARCH_TOOL],
        "system": BASE_IDENTITY + """
Это чат разбора бизнес-моделей и анализа рынка/конкурентов.
- Бизнес-модель разбирай структурно: ценностное предложение → сегменты клиентов →
  каналы → потоки выручки → структура издержек → ключевые ресурсы и партнёры →
  риски. В конце — 3 главных вопроса, которые надо проверить.
- Конкурентов и рынок проверяй веб-поиском: реальные игроки, цены, динамика.
  Цифры — с источниками.
- Всегда привязывай выводы к контексту: итальянский рынок, PMI, специфика клиента.""",
    },
}


def _system_for(mode: str, project_name: str | None, project_desc: str | None) -> str:
    cfg = MODES.get(mode, MODES["assistant"])
    now = datetime.now(config.ROME_TZ)
    weekdays = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]
    system = cfg["system"] + (
        f"\nСегодня {now:%d.%m.%Y}, {weekdays[now.weekday()]}, "
        f"время {now:%H:%M} (Europe/Rome)."
    )
    if project_name:
        system += (f"\n\nЭтот чат относится к проекту «{project_name}». "
                   f"Весь анализ веди в контексте этого проекта.")
        if project_desc:
            system += f"\nОписание проекта: {project_desc}"
    return system


def _extract_text(content: list) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


async def run_chat(mode: str, messages: list[dict],
                   project_name: str | None = None,
                   project_desc: str | None = None) -> str:
    """Прогнать историю чата через агентный цикл выбранного режима."""
    cfg = MODES.get(mode, MODES["assistant"])
    system = _system_for(mode, project_name, project_desc)
    convo = list(messages)

    # режим без инструментов (переводчик) — один вызов
    if not cfg["tools"]:
        response = await client.messages.create(
            model=cfg["model"], max_tokens=4000, system=system, messages=convo)
        return _extract_text(response.content) or "(пустой ответ)"

    for _ in range(MAX_TURNS):
        response = await client.messages.create(
            model=cfg["model"], max_tokens=8000, system=system,
            tools=cfg["tools"], messages=convo)

        if response.stop_reason == "pause_turn":
            convo.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            return _extract_text(response.content) or "(пустой ответ)"

        convo.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            handler = tools.HANDLERS.get(block.name)
            if handler is None:
                result, is_error = f"Неизвестный инструмент: {block.name}", True
            else:
                try:
                    result, is_error = await handler(block.input or {}), False
                except Exception as e:
                    logger.exception("Tool %s failed", block.name)
                    result, is_error = f"Ошибка инструмента: {e}", True
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(result)[:50000],
                "is_error": is_error,
            })
        convo.append({"role": "user", "content": results})

    return "Я упёрлась в лимит шагов на этот запрос. Разбей задачу на части или повтори."
