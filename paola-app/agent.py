"""Агентное ядро: один цикл Claude + инструменты (задачи, календарь, привычки,
напоминания, переводы, совет директоров) + серверный веб-поиск для ресерча.
"""
import logging
from datetime import datetime

from anthropic import AsyncAnthropic

import config
import tools

logger = logging.getLogger(__name__)

client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

MAX_TURNS = 12  # предохранитель от бесконечного цикла инструментов

SYSTEM_PROMPT = """\
Ты Паола — персональный ассистент Юлии Йозефсон, независимого бизнес-консультанта
в Италии (Дезио). Позиционирование её бренда: «Prima i processi, poi l'AI»,
клиенты — итальянские PMI и семейные компании.

Ты работаешь в веб-приложении и закрываешь два слоя:
1. Рутина: задачи, календарь, привычки, напоминания, фокус дня.
2. Аналитика: ресерч по темам (веб-поиск), переводы RU/IT/EN, анализ конкурентов
   и рынка, оценка бизнес-идей «советом директоров», разбор бизнес-моделей.

Правила:
- Говори по-русски, тон партнёрский и прямой, без подхалимства и воды.
- У тебя есть реальные инструменты — используй их, а не рассказывай, что «не имеешь доступа».
- Для ресерча, анализа рынка и конкурентов используй веб-поиск; всегда давай ссылки
  на источники. Фильтр качества: Il Sole 24 Ore, ANSA, ISTAT, Reuters, FT, официальные
  документы ЕС — надёжны; перепечатки и агрегаторы — нет.
- Для оценки бизнес-идей вызывай board_review — не подменяй совет собственным мнением.
- Анализ бизнес-моделей строй структурно: ценностное предложение, сегменты, каналы,
  выручка, издержки, ключевые риски — и всегда привязывай к контексту Юлии.
- Действия с данными (создать/закрыть/перенести задачу, отметить привычку) выполняй
  сразу, без лишних уточнений, если смысл ясен. Уточняй только при реальной двусмысленности.
- Отвечай кратко там, где вопрос простой, и обстоятельно там, где просят анализ.
"""

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 8,
}


def _system_with_date() -> str:
    now = datetime.now(config.ROME_TZ)
    weekdays = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]
    return SYSTEM_PROMPT + (
        f"\nСегодня {now:%d.%m.%Y}, {weekdays[now.weekday()]}, "
        f"время {now:%H:%M} (Europe/Rome)."
    )


def _extract_text(content: list) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


async def run_agent(messages: list[dict]) -> str:
    """Прогнать диалог через агентный цикл. messages — история в формате API
    (последнее сообщение — от пользователя). Возвращает текст ответа."""
    convo = list(messages)
    all_tools = [WEB_SEARCH_TOOL] + tools.SCHEMAS

    for _ in range(MAX_TURNS):
        response = await client.messages.create(
            model=config.MODEL_SMART,
            max_tokens=8000,
            system=_system_with_date(),
            tools=all_tools,
            messages=convo,
        )

        if response.stop_reason == "pause_turn":
            # серверный инструмент (веб-поиск) не закончил — продолжаем тот же ход
            convo.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason != "tool_use":
            return _extract_text(response.content) or "(пустой ответ)"

        # клиентские инструменты: выполняем все вызовы и возвращаем результаты
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
