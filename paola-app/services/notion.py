"""Асинхронный доступ к Notion: задачи, привычки, напоминания.

Схемы свойств совпадают с базами Second Brain — приложение и бот Паола
работают с одной памятью.
"""
import logging
from datetime import datetime, date, timedelta

from notion_client import AsyncClient

import config

logger = logging.getLogger(__name__)

notion = AsyncClient(auth=config.NOTION_TOKEN)

ACTIVE_STATUSES = ["To do", "In progress"]
PRIORITY_ICONS = {"❗ Важное": "🔴", "✅ Обычное": "🟢", "🔜 Когда-нибудь": "⚪"}
PRIORITY_ORDER = ["❗ Важное", "✅ Обычное", "🔜 Когда-нибудь"]


def _rich_to_text(rich: list) -> str:
    return "".join(part.get("plain_text", "") for part in rich)


async def _query_all(database_id: str, **kwargs) -> list[dict]:
    """Запрос с пагинацией — не теряем записи после первой сотни."""
    results: list[dict] = []
    cursor = None
    while True:
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = await notion.databases.query(database_id=database_id, **kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            return results
        cursor = resp.get("next_cursor")


# ---------------------------------------------------------------------------
# Задачи (база «Задачи» — общая с Паолой)
# ---------------------------------------------------------------------------

def parse_task(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id":        page["id"],
        "title":     _rich_to_text(props.get("Задача", {}).get("title", [])),
        "priority":  (props.get("Приоритет", {}).get("select") or {}).get("name", ""),
        "zone":      (props.get("Зона", {}).get("select") or {}).get("name", ""),
        "deadline":  (props.get("Дедлайн", {}).get("date") or {}).get("start", ""),
        "status":    (props.get("Статус", {}).get("select") or {}).get("name", ""),
        "performer": (props.get("Кто делает", {}).get("select") or {}).get("name", ""),
        "project":   (props.get("Проект", {}).get("select") or {}).get("name", ""),
    }


def sort_by_priority(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: PRIORITY_ORDER.index(t["priority"])
                  if t.get("priority") in PRIORITY_ORDER else 2)


async def get_active_tasks() -> list[dict]:
    pages = await _query_all(
        config.NOTION_TODOLIST_DB_ID,
        filter={"or": [{"property": "Статус", "select": {"equals": s}}
                       for s in ACTIVE_STATUSES]},
    )
    return [t for p in pages if (t := parse_task(p))["title"]]


async def get_overdue_tasks() -> list[dict]:
    today = datetime.now(config.ROME_TZ).strftime("%Y-%m-%d")
    pages = await _query_all(
        config.NOTION_TODOLIST_DB_ID,
        filter={"and": [
            {"or": [{"property": "Статус", "select": {"equals": s}}
                    for s in ACTIVE_STATUSES]},
            {"property": "Дедлайн", "date": {"before": today}},
        ]},
    )
    return [t for p in pages if (t := parse_task(p))["title"]]


async def create_task(title: str, deadline: str | None = None,
                      priority: str = "✅ Обычное", zone: str = "💼 Бизнес",
                      project: str | None = None, performer: str = "Юля",
                      comment: str | None = None) -> dict:
    properties = {
        "Задача":     {"title": [{"text": {"content": title}}]},
        "Приоритет":  {"select": {"name": priority}},
        "Статус":     {"select": {"name": "To do"}},
        "Зона":       {"select": {"name": zone}},
        "Кто делает": {"select": {"name": performer}},
    }
    if deadline:
        properties["Дедлайн"] = {"date": {"start": deadline}}
    if project:
        properties["Проект"] = {"select": {"name": project}}
    if comment:
        properties["Комментарий"] = {"rich_text": [{"text": {"content": comment[:2000]}}]}
    return await notion.pages.create(
        parent={"database_id": config.NOTION_TODOLIST_DB_ID}, properties=properties)


async def close_task(page_id: str) -> None:
    await notion.pages.update(page_id=page_id,
                              properties={"Статус": {"select": {"name": "Done"}}})


async def delete_task(page_id: str) -> None:
    """Удалить задачу — страница уходит в архив Notion (в базе не видна)."""
    await notion.pages.update(page_id=page_id, archived=True)


async def reschedule_task(page_id: str, new_date: str) -> None:
    await notion.pages.update(page_id=page_id,
                              properties={"Дедлайн": {"date": {"start": new_date}}})


async def get_deadline_between(start_iso: str, end_iso: str,
                               statuses: tuple[str, ...] = ("To do", "In progress", "Sospeso")
                               ) -> list[dict]:
    """Незакрытые задачи с дедлайном в интервале [start; end] (даты YYYY-MM-DD)."""
    pages = await _query_all(
        config.NOTION_TODOLIST_DB_ID,
        filter={"and": [
            {"or": [{"property": "Статус", "select": {"equals": s}} for s in statuses]},
            {"property": "Дедлайн", "date": {"on_or_after": start_iso}},
            {"property": "Дедлайн", "date": {"on_or_before": end_iso}},
        ]},
    )
    return [t for p in pages if (t := parse_task(p))["title"]]


async def get_undated_active() -> list[dict]:
    """Активные задачи без дедлайна (для «важное» и «очереди» в плане недели)."""
    pages = await _query_all(
        config.NOTION_TODOLIST_DB_ID,
        filter={"and": [
            {"or": [{"property": "Статус", "select": {"equals": s}}
                    for s in ACTIVE_STATUSES]},
            {"property": "Дедлайн", "date": {"is_empty": True}},
        ]},
    )
    return [t for p in pages if (t := parse_task(p))["title"]]


async def get_done_between(start_iso: str, end_iso: str) -> list[dict]:
    """Задачи со статусом Done, отредактированные в интервале дат (прокси даты закрытия)."""
    pages = await _query_all(
        config.NOTION_TODOLIST_DB_ID,
        filter={"and": [
            {"property": "Статус", "select": {"equals": "Done"}},
            {"timestamp": "last_edited_time",
             "last_edited_time": {"on_or_after": start_iso}},
            {"timestamp": "last_edited_time",
             "last_edited_time": {"on_or_before": end_iso}},
        ]},
    )
    return [t for p in pages if (t := parse_task(p))["title"]]


# ---------------------------------------------------------------------------
# Привычки: база «Привычки» + база «Журнал привычек»
# Схемы описаны в README (создаются один раз вручную).
# ---------------------------------------------------------------------------

async def get_habits() -> list[dict]:
    """Активные привычки."""
    if not config.NOTION_HABITS_DB_ID:
        return []
    pages = await _query_all(
        config.NOTION_HABITS_DB_ID,
        filter={"property": "Активна", "checkbox": {"equals": True}},
    )
    habits = []
    for p in pages:
        props = p.get("properties", {})
        name = _rich_to_text(props.get("Привычка", {}).get("title", []))
        if name:
            habits.append({
                "id": p["id"],
                "name": name,
                "goal": _rich_to_text(props.get("Цель", {}).get("rich_text", [])),
            })
    return habits


async def get_habit_log(days: int = 30) -> list[dict]:
    """Записи журнала за последние N дней."""
    if not config.NOTION_HABIT_LOG_DB_ID:
        return []
    since = (datetime.now(config.ROME_TZ).date() - timedelta(days=days)).isoformat()
    pages = await _query_all(
        config.NOTION_HABIT_LOG_DB_ID,
        filter={"property": "Дата", "date": {"on_or_after": since}},
    )
    entries = []
    for p in pages:
        props = p.get("properties", {})
        entries.append({
            "id":    p["id"],
            "habit": _rich_to_text(props.get("Привычка", {}).get("rich_text", [])),
            "date":  (props.get("Дата", {}).get("date") or {}).get("start", ""),
            "done":  props.get("Выполнено", {}).get("checkbox", False),
        })
    return entries


async def log_habit(habit_name: str, day: str, done: bool) -> None:
    """Отметить привычку за день. Если запись уже есть — обновляет её."""
    existing = await _query_all(
        config.NOTION_HABIT_LOG_DB_ID,
        filter={"and": [
            {"property": "Дата", "date": {"equals": day}},
            {"property": "Привычка", "rich_text": {"equals": habit_name}},
        ]},
    )
    if existing:
        await notion.pages.update(
            page_id=existing[0]["id"],
            properties={"Выполнено": {"checkbox": done}},
        )
        return
    await notion.pages.create(
        parent={"database_id": config.NOTION_HABIT_LOG_DB_ID},
        properties={
            "Запись":    {"title": [{"text": {"content": f"{habit_name} — {day}"}}]},
            "Привычка":  {"rich_text": [{"text": {"content": habit_name}}]},
            "Дата":      {"date": {"start": day}},
            "Выполнено": {"checkbox": done},
        },
    )


def habit_streak(entries: list[dict], habit_name: str, today: date) -> int:
    """Текущая серия подряд выполненных дней для привычки."""
    done_days = {e["date"] for e in entries if e["habit"] == habit_name and e["done"]}
    streak = 0
    day = today
    # сегодняшний день ещё может быть не отмечен — серия не рвётся
    if day.isoformat() not in done_days:
        day -= timedelta(days=1)
    while day.isoformat() in done_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


# ---------------------------------------------------------------------------
# Напоминания: база «Напоминания»
# ---------------------------------------------------------------------------

async def create_reminder(text: str, when_iso: str) -> dict:
    return await notion.pages.create(
        parent={"database_id": config.NOTION_REMINDERS_DB_ID},
        properties={
            "Напоминание": {"title": [{"text": {"content": text}}]},
            "Когда":       {"date": {"start": when_iso, "time_zone": "Europe/Rome"}},
            "Отправлено":  {"checkbox": False},
        },
    )


async def get_pending_reminders() -> list[dict]:
    """Неотправленные напоминания (для планировщика и дашборда)."""
    if not config.NOTION_REMINDERS_DB_ID:
        return []
    pages = await _query_all(
        config.NOTION_REMINDERS_DB_ID,
        filter={"property": "Отправлено", "checkbox": {"equals": False}},
    )
    reminders = []
    for p in pages:
        props = p.get("properties", {})
        when = (props.get("Когда", {}).get("date") or {}).get("start", "")
        text = _rich_to_text(props.get("Напоминание", {}).get("title", []))
        if text and when:
            reminders.append({"id": p["id"], "text": text, "when": when})
    return sorted(reminders, key=lambda r: r["when"])


async def mark_reminder_sent(page_id: str) -> None:
    await notion.pages.update(page_id=page_id,
                              properties={"Отправлено": {"checkbox": True}})


# ---------------------------------------------------------------------------
# Материалы и досье клиентов: страницы с результатами и памятью проектов
# ---------------------------------------------------------------------------

def _para(text: str = "") -> dict:
    rich = [{"type": "text", "text": {"content": text}}] if text else []
    return {"type": "paragraph", "paragraph": {"rich_text": rich}}


def _h2(text: str) -> dict:
    return {"type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


async def create_material_page(title: str, blocks: list[dict],
                               parent_page_id: str | None = None) -> str:
    """Создать страницу-материал (внутри досье проекта или «Материалов»), вернуть URL.

    Notion принимает максимум 100 блоков за запрос — остальные дописываются
    батчами через blocks.children.append.
    """
    parent = parent_page_id or config.NOTION_MATERIALS_PAGE_ID
    if not parent:
        raise RuntimeError("Не задан родитель страницы (NOTION_MATERIALS_PAGE_ID)")
    first, rest = blocks[:100], blocks[100:]
    page = await notion.pages.create(
        parent={"page_id": parent},
        properties={"title": {"title": [{"text": {"content": title[:200]}}]}},
        children=first,
    )
    for i in range(0, len(rest), 100):
        await notion.blocks.children.append(block_id=page["id"],
                                            children=rest[i:i + 100])
    return page.get("url", "")


async def create_dossier_page(name: str, description: str = "") -> dict:
    """Досье клиента внутри страницы «Клиенты»: скелет секций.

    «Факты и решения» — последняя секция: append_dossier_facts дописывает
    блоки в конец страницы, и факты попадают под нужный заголовок.
    """
    if not config.NOTION_CLIENTS_PAGE_ID:
        raise RuntimeError("NOTION_CLIENTS_PAGE_ID не задан")
    children = []
    if description:
        children.append(_para(description))
    children += [_h2("Карточка клиента"), _para(),
                 _h2("Цели"), _para(),
                 _h2("Факты и решения")]
    page = await notion.pages.create(
        parent={"page_id": config.NOTION_CLIENTS_PAGE_ID},
        properties={"title": {"title": [{"text": {"content": name[:200]}}]}},
        children=children,
    )
    return {"id": page["id"], "url": page.get("url", "")}


async def get_child_pages(page_id: str) -> list[dict]:
    """Дочерние страницы (child_page) заданной страницы: id, title, url.

    Нужна для обратной синхронизации: папки, заведённые вручную под «Клиенты»,
    приложение подхватывает как проекты. URL собираем из id — короткая форма
    notion.so/<id> открывает ту же страницу (Notion редиректит на слаг).
    """
    pages: list[dict] = []
    cursor = None
    while True:
        kwargs = {"start_cursor": cursor} if cursor else {}
        resp = await notion.blocks.children.list(block_id=page_id, **kwargs)
        for b in resp.get("results", []):
            if b.get("type") == "child_page":
                pid = b["id"]
                pages.append({
                    "id": pid,
                    "title": b.get("child_page", {}).get("title", ""),
                    "url": "https://www.notion.so/" + pid.replace("-", ""),
                })
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return pages


async def append_dossier_facts(page_id: str, facts: list[str]) -> None:
    stamp = datetime.now(config.ROME_TZ).strftime("%d.%m.%Y")
    children = [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [
        {"type": "text", "text": {"content": f"{stamp}: {f}"[:1900]}}]}}
        for f in facts]
    await notion.blocks.children.append(block_id=page_id, children=children)


_TEXT_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3",
                "bulleted_list_item", "numbered_list_item", "to_do",
                "quote", "callout", "toggle"}


async def get_page_text(page_id: str, max_chars: int = 6000) -> str:
    """Содержимое страницы плоским текстом — для инжекта досье в промпт.

    Заголовки помечаются ##, таблицы разворачиваются построчно, подстраницы
    (материалы) — одной строкой с названием.
    """
    lines: list[str] = []
    cursor = None
    while True:
        kwargs = {"start_cursor": cursor} if cursor else {}
        resp = await notion.blocks.children.list(block_id=page_id, **kwargs)
        for b in resp.get("results", []):
            t = b.get("type")
            if t in _TEXT_BLOCKS:
                txt = _rich_to_text(b[t].get("rich_text", []))
                if not txt:
                    continue
                if t.startswith("heading"):
                    lines.append(f"\n## {txt}")
                elif t in ("bulleted_list_item", "numbered_list_item", "to_do"):
                    lines.append(f"- {txt}")
                else:
                    lines.append(txt)
            elif t == "table":
                rows = await notion.blocks.children.list(block_id=b["id"])
                for r in rows.get("results", []):
                    cells = r.get("table_row", {}).get("cells", [])
                    lines.append(" | ".join(_rich_to_text(c) for c in cells))
            elif t == "child_page":
                lines.append(f"- [сохранённый материал] {b.get('child_page', {}).get('title', '')}")
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return "\n".join(lines).strip()[:max_chars]
