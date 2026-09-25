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


async def reschedule_task(page_id: str, new_date: str | None) -> None:
    """new_date=None — очистить дедлайн (задача становится без даты)."""
    await notion.pages.update(
        page_id=page_id,
        properties={"Дедлайн": {"date": {"start": new_date} if new_date else None}})


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

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]  # индекс = date.weekday()


def habit_scheduled(habit: dict, day: date) -> bool:
    """Запланирована ли привычка на этот день (по полю «Дни»; пусто — всегда)."""
    weekdays = habit.get("weekdays")
    return not weekdays or day.weekday() in weekdays


async def get_habits() -> list[dict]:
    """Активные привычки. Тип «Число» — цель считается достигнутой по значению
    (Значение >= Цель число), «Да/Нет» — по чекбоксу Выполнено.

    Порядок и Группа задают отображение на Доме (сортировка + заголовки
    секций, например «Витамины», «Самообразование») — управляются из Notion,
    без правки кода. Дни (мультиселект Пн…Вс) — расписание: пусто — каждый
    день, иначе привычка показывается только в эти дни недели."""
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
                "type": (props.get("Тип", {}).get("select") or {}).get("name") or "Да/Нет",
                "goal": _rich_to_text(props.get("Цель", {}).get("rich_text", [])),
                "goal_number": props.get("Цель число", {}).get("number"),
                "unit": _rich_to_text(props.get("Единица", {}).get("rich_text", [])),
                "order": props.get("Порядок", {}).get("number"),
                "group": _rich_to_text(props.get("Группа", {}).get("rich_text", [])),
                "weekdays": sorted(
                    WEEKDAYS.index(o["name"])
                    for o in props.get("Дни", {}).get("multi_select", [])
                    if o.get("name") in WEEKDAYS) or None,
            })
    habits.sort(key=lambda h: h["order"] if h["order"] is not None else 9999)
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
            "value": props.get("Значение", {}).get("number"),
            "note":  _rich_to_text(props.get("Заметка", {}).get("rich_text", [])),
        })
    return entries


async def log_habit(habit_name: str, day: str, done: bool,
                    value: float | None = None, note: str | None = None) -> None:
    """Отметить привычку за день. Если запись уже есть — обновляет её."""
    existing = await _query_all(
        config.NOTION_HABIT_LOG_DB_ID,
        filter={"and": [
            {"property": "Дата", "date": {"equals": day}},
            {"property": "Привычка", "rich_text": {"equals": habit_name}},
        ]},
    )
    properties: dict = {"Выполнено": {"checkbox": done}}
    if value is not None:
        properties["Значение"] = {"number": value}
    if note is not None:
        properties["Заметка"] = {"rich_text": [{"text": {"content": note[:2000]}}]}
    if existing:
        await notion.pages.update(page_id=existing[0]["id"], properties=properties)
        return
    await notion.pages.create(
        parent={"database_id": config.NOTION_HABIT_LOG_DB_ID},
        properties={
            "Запись":   {"title": [{"text": {"content": f"{habit_name} — {day}"}}]},
            "Привычка": {"rich_text": [{"text": {"content": habit_name}}]},
            "Дата":     {"date": {"start": day}},
            **properties,
        },
    )


def habit_streak(entries: list[dict], habit_name: str, today: date,
                 goal_number: float | None = None,
                 weekdays: list[int] | None = None) -> int:
    """Текущая серия подряд выполненных дней для привычки.

    goal_number задан — привычка числовая, день засчитан при Значение >=
    Цель число; иначе — по чекбоксу Выполнено (тип «Да/Нет»).
    weekdays задан — считаются только запланированные дни (для привычки
    «по субботам» серия = сколько суббот подряд)."""
    if goal_number is not None:
        done_days = {e["date"] for e in entries
                    if e["habit"] == habit_name and e.get("value") is not None
                    and e["value"] >= goal_number}
    else:
        done_days = {e["date"] for e in entries if e["habit"] == habit_name and e["done"]}
    if weekdays:
        # отметка в незапланированный день серию не рвёт и не увеличивает
        scheduled = lambda d: d.weekday() in weekdays
    else:
        scheduled = lambda d: True

    def prev(d: date) -> date:
        d -= timedelta(days=1)
        while not scheduled(d):
            d -= timedelta(days=1)
        return d

    streak = 0
    day = today if scheduled(today) else prev(today)
    # сегодняшний день ещё может быть не отмечен — серия не рвётся
    if day == today and day.isoformat() not in done_days:
        day = prev(day)
    while day.isoformat() in done_days:
        streak += 1
        day = prev(day)
    return streak


# ---------------------------------------------------------------------------
# Консунтиво Generali: учёт часов и биллинг (базы под страницей клиента Generali)
# Consuntivo Generali — строки-активности, Tariffe — ставки, Reparti — справочник.
# ---------------------------------------------------------------------------

# Строки, которые считаются подтверждёнными для фактуры (черновик TO CHECK — нет).
CONSUNTIVO_BILLABLE = ("Done", "Confermato", "Fatturato")


def _sel(props: dict, name: str) -> str:
    return (props.get(name, {}).get("select") or {}).get("name", "")


def _num(props: dict, name: str):
    return props.get(name, {}).get("number")


async def get_reparti() -> list[dict]:
    """Справочник департаментов Generali: reparto → wave, responsabile, gruppo, fase."""
    if not config.NOTION_REPARTI_DB_ID:
        return []
    pages = await _query_all(config.NOTION_REPARTI_DB_ID)
    out = []
    for p in pages:
        props = p.get("properties", {})
        name = _rich_to_text(props.get("Reparto", {}).get("title", []))
        if name:
            out.append({
                "reparto": name,
                "wave": _sel(props, "Wave"),
                "responsabile": _rich_to_text(props.get("Responsabile", {}).get("rich_text", [])),
                "gruppo": _sel(props, "Gruppo"),
                "fase": _sel(props, "Fase"),
            })
    return out


async def get_tariffe() -> dict[str, float]:
    """Тарифная карта: тип активности → ставка €/час."""
    if not config.NOTION_TARIFFE_DB_ID:
        return {}
    pages = await _query_all(config.NOTION_TARIFFE_DB_ID)
    rates: dict[str, float] = {}
    for p in pages:
        props = p.get("properties", {})
        tipo = _rich_to_text(props.get("Tipo", {}).get("title", []))
        rate = _num(props, "Rate")
        if tipo:
            rates[tipo] = rate if rate is not None else 0.0
    return rates


def _parse_consuntivo(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id":        page["id"],
        "attivita":  _rich_to_text(props.get("Attività", {}).get("title", [])),
        "data":      (props.get("Data", {}).get("date") or {}).get("start", ""),
        "ondata":    _sel(props, "Ondata"),
        "fase":      _sel(props, "Fase"),
        "tipo":      _sel(props, "Tipo"),
        "reparto":   _sel(props, "Reparto"),
        "mese":      _sel(props, "Mese"),
        "ore":       _num(props, "Ore") or 0.0,
        "prezzo":    _num(props, "Prezzo") or 0.0,
        "stato":     _sel(props, "Stato"),
        "inizio":    _rich_to_text(props.get("Inizio", {}).get("rich_text", [])),
    }


async def query_consuntivo(start_iso: str, end_iso: str,
                           statuses: tuple[str, ...] | None = None) -> list[dict]:
    """Строки консунтиво с датой в [start; end]. statuses=None — все статусы."""
    if not config.NOTION_CONSUNTIVO_DB_ID:
        return []
    date_filter = [
        {"property": "Data", "date": {"on_or_after": start_iso}},
        {"property": "Data", "date": {"on_or_before": end_iso}},
    ]
    if statuses:
        date_filter.append({"or": [
            {"property": "Stato", "select": {"equals": s}} for s in statuses]})
    pages = await _query_all(config.NOTION_CONSUNTIVO_DB_ID,
                             filter={"and": date_filter})
    return [_parse_consuntivo(p) for p in pages]


async def consuntivo_existing_keys(start_iso: str, end_iso: str) -> set[tuple[str, str, str]]:
    """Ключи (дата, начало, название) уже занесённых строк — чтобы не дублировать
    при повторном сборе периода."""
    rows = await query_consuntivo(start_iso, end_iso)
    return {(r["data"], r["inizio"], r["attivita"].strip().lower()) for r in rows}


async def create_consuntivo_rows(rows: list[dict]) -> int:
    """Создать строки консунтиво со статусом TO CHECK (черновик на проверку).

    Каждый row: attivita, data (YYYY-MM-DD), inizio, fine, ore (float),
    prezzo (float|None), tipo, reparto, ondata, fase, fase_nome, mese.
    Пустые поля пропускаются. Возвращает число созданных строк.
    """
    created = 0
    for r in rows:
        props: dict = {
            "Attività": {"title": [{"text": {"content": (r.get("attivita") or "(без названия)")[:2000]}}]},
            "Stato":    {"select": {"name": "TO CHECK"}},
        }
        if r.get("data"):
            props["Data"] = {"date": {"start": r["data"]}}
        for key, field in (("ondata", "Ondata"), ("fase", "Fase"),
                           ("tipo", "Tipo"), ("reparto", "Reparto"), ("mese", "Mese")):
            if r.get(key):
                props[field] = {"select": {"name": r[key]}}
        for key, field in (("inizio", "Inizio"), ("fine", "Fine"),
                           ("fase_nome", "Fase nome")):
            if r.get(key):
                props[field] = {"rich_text": [{"text": {"content": str(r[key])[:2000]}}]}
        if r.get("ore") is not None:
            props["Ore"] = {"number": round(float(r["ore"]), 4)}
        if r.get("prezzo") is not None:
            props["Prezzo"] = {"number": round(float(r["prezzo"]), 4)}
        await notion.pages.create(
            parent={"database_id": config.NOTION_CONSUNTIVO_DB_ID}, properties=props)
        created += 1
    return created


async def confirm_consuntivo_period(start_iso: str, end_iso: str) -> int:
    """Перевести черновики TO CHECK за период в Confermato. Возвращает число строк."""
    rows = await query_consuntivo(start_iso, end_iso, statuses=("TO CHECK",))
    for r in rows:
        await notion.pages.update(
            page_id=r["id"], properties={"Stato": {"select": {"name": "Confermato"}}})
    return len(rows)
