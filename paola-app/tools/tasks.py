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


def _fmt_deadline(deadline: str | None) -> str:
    if not deadline:
        return "без дедлайна"
    try:
        return "дедлайн " + datetime.strptime(deadline, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return f"дедлайн {deadline}"


async def _create_task(inp: dict) -> str:
    zone = inp.get("zone") or "💼 Бизнес"
    priority = inp.get("priority") or "✅ Обычное"
    project = inp.get("project") or None
    performer = inp.get("performer") or "Юля"
    page = await notion.create_task(
        title=inp["title"],
        deadline=inp.get("deadline") or None,
        priority=priority,
        zone=zone,
        project=project,
        performer=performer,
        comment=inp.get("comment") or None,
    )
    # Формат — как в Second Brain: все поля видны сразу, чтобы Юлия могла
    # проверить занесение с одного взгляда, не открывая Notion.
    parts = [zone, project or "без проекта", priority, _fmt_deadline(inp.get("deadline"))]
    if performer != "Юля":
        parts.append(performer)
    return (f"Задача создана: «{inp['title']}» — " + ", ".join(parts) + ", To do."
            f" (id:{page['id']})")


async def _close_task(inp: dict) -> str:
    await notion.close_task(inp["task_id"])
    return "Задача закрыта (Done)."


async def _reschedule_task(inp: dict) -> str:
    await notion.reschedule_task(inp["task_id"], inp["new_date"])
    return f"Дедлайн перенесён на {inp['new_date']}."


ZONES = ["💼 Бизнес", "👥 Networking", "🏥 Salute", "🧠 Mental", "🏠 Family",
         "💅 Beauty", "✈️ Viaggi", "📚 Ресурсы", "✍️ Контент", "💰 Финансы"]
PROJECTS = ["Generali", "Second Brain", "Развитие бизнеса", "Личное"]
PERFORMERS = ["Юля", "Винченцо", "Делегировано"]

ZONE_RULES = (
    "Раздел жизни — определи по смыслу задачи, никогда не спрашивай. "
    "💼 Бизнес — работа, клиенты, консалтинг, бизнес-проекты. "
    "👥 Networking — встречи, знакомства, follow-up с контактами. "
    "🏥 Salute — здоровье, врачи, витамины, спорт. "
    "🧠 Mental — обучение, курсы, коучинг, книги, личное развитие "
    "(сюда же — работа над Second Brain / ботом / приложением Паола). "
    "🏠 Family — Винченцо, дочка, дом, квартира, ремонт, дизайнер, мебель, быт. "
    "💅 Beauty — процедуры, записи к мастеру. "
    "✈️ Viaggi — путешествия, поездки. "
    "📚 Ресурсы — книги, фильмы, инструменты на будущее. "
    "✍️ Контент — посты, сайт, LinkedIn. "
    "💰 Финансы — оплаты, возвраты, долги, счета, налоги, commercialista. "
    "Пример на который легко ошибиться: «выбрать торшер», «квартира дизайнеру», "
    "«ремонт» → 🏠 Family, НЕ 💼 Бизнес — Бизнес это про работу и клиентов, а не про дом."
)
PRIORITY_RULES = (
    "Определяй по смыслу задачи, а НЕ по наличию дедлайна на сегодня — "
    "дедлайн сегодня сам по себе не поднимает приоритет. "
    "❗ Важное — деньги (оплата/возврат/долг/счёт), клиентские задачи, влияет на "
    "доход или репутацию, документы для commercialista, здоровье (врачи/анализы), "
    "внешний дедлайн приближается за 2-3 дня, или Юлия явно говорит «важно». "
    "🔜 Когда-нибудь — ТОЛЬКО если Юлия явно говорит «не срочно» / «на будущее» / "
    "«когда-нибудь»; отсутствие даты — это не повод для этого приоритета. "
    "✅ Обычное — всё остальное, включая обычные задачи с дедлайном на сегодня."
)
PROJECT_RULES = (
    "К какому проекту относится — заполняй, если контекст очевиден, иначе не указывай. "
    "Generali — контракт, встречи, отчёты, коучинг, формационе. "
    "Second Brain — Notion, дайджесты, автоматизации, бот/приложение Паола. "
    "Развитие бизнеса — сайт, LinkedIn, новые клиенты, Unity, стратегия. "
    "Личное — семья, здоровье, быт, личные финансы."
)

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
                           "записать, запланировать или не забыть сделать — но не когда она "
                           "просто задаёт вопрос, рассуждает вслух или просит анализ/текст. "
                           "Перед вызовом молча проверь про себя все поля: зона (по правилам "
                           "ниже, не по умолчанию), приоритет (не завышен из-за дедлайна "
                           "сегодня), проект (если очевиден). После вызова обязательно "
                           "покажи Юлии в ответе зону, проект, приоритет и дедлайн из "
                           "результата инструмента — коротко, но не пропуская поля: так она "
                           "может проверить занесение с одного взгляда, как это делает Claude "
                           "в Second Brain.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title":     {"type": "string", "description": "Название: глагол + суть"},
                    "deadline":  {"type": "string", "description": "YYYY-MM-DD, если назван срок"},
                    "priority":  {"type": "string",
                                 "enum": ["❗ Важное", "✅ Обычное", "🔜 Когда-нибудь"],
                                 "description": PRIORITY_RULES},
                    "zone":      {"type": "string", "enum": ZONES, "description": ZONE_RULES},
                    "project":   {"type": "string", "enum": PROJECTS,
                                 "description": PROJECT_RULES},
                    "performer": {"type": "string", "enum": PERFORMERS,
                                 "description": "Кто делает — по умолчанию Юля, меняй только "
                                                "если явно назван Винченцо или сказано "
                                                "«делегирую»."},
                    "comment":   {"type": "string", "description": "Дополнительный контекст"},
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
