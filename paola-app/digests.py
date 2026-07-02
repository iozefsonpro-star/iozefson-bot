"""Сводки: утренняя и вечерняя. Используются дашбордом и планировщиком."""
import asyncio
import logging
from datetime import datetime

import config
from services import notion, gcal

logger = logging.getLogger(__name__)

_WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _task_line(t: dict) -> str:
    icon = notion.PRIORITY_ICONS.get(t.get("priority", ""), "⚪")
    who = (f" (→ {t['performer']})"
           if t.get("performer") and t["performer"] != "Юля" else "")
    dl = f" — до {t['deadline'][:10]}" if t.get("deadline") else ""
    return f"{icon} {t['title']}{dl}{who}"


async def build_morning_digest() -> str:
    now = datetime.now(config.ROME_TZ)
    today_iso = now.date().isoformat()
    header = f"Доброе утро, Юлия! {now:%d.%m.%Y} ({_WEEKDAYS_RU[now.weekday()]})"

    tasks, overdue, events, habits, log = await asyncio.gather(
        notion.get_active_tasks(),
        notion.get_overdue_tasks(),
        gcal.get_events(days=0),
        notion.get_habits(),
        notion.get_habit_log(days=60),
        return_exceptions=True,
    )
    tasks   = tasks   if isinstance(tasks, list) else []
    overdue = overdue if isinstance(overdue, list) else []
    events  = events  if isinstance(events, list) else []
    habits  = habits  if isinstance(habits, list) else []
    log     = log     if isinstance(log, list) else []

    parts = [header, ""]

    today_events = [e for e in events if e["day"] == today_iso]
    if today_events:
        parts.append("📅 Встречи дня:")
        parts.extend(f"  {gcal.format_event(e)}" for e in today_events)
        parts.append("")

    tasks_today = notion.sort_by_priority(
        [t for t in tasks if t.get("deadline", "")[:10] == today_iso])
    if tasks_today:
        parts.append(f"📋 Запланировано на сегодня ({len(tasks_today)}):")
        parts.extend(f"  {_task_line(t)}" for t in tasks_today)
    else:
        parts.append("📋 Задач с дедлайном сегодня нет.")
    parts.append("")

    if overdue:
        parts.append(f"⚠️ Просрочено ({len(overdue)}):")
        parts.extend(f"  {_task_line(t)}" for t in overdue)
        parts.append("")

    if habits:
        parts.append("🔁 Привычки на сегодня:")
        for h in habits:
            streak = notion.habit_streak(log, h["name"], now.date())
            parts.append(f"  ⬜ {h['name']} (серия {streak} дн.)")

    return "\n".join(parts).strip()


async def build_evening_digest() -> str:
    now = datetime.now(config.ROME_TZ)
    today_iso = now.date().isoformat()
    parts = [f"🌙 Вечерняя сводка — {now:%d.%m.%Y}", ""]

    overdue, habits, log = await asyncio.gather(
        notion.get_overdue_tasks(),
        notion.get_habits(),
        notion.get_habit_log(days=7),
        return_exceptions=True,
    )
    overdue = overdue if isinstance(overdue, list) else []
    habits  = habits  if isinstance(habits, list) else []
    log     = log     if isinstance(log, list) else []

    if overdue:
        parts.append(f"⚠️ Просрочено ({len(overdue)}) — закрыть или перенести?")
        parts.extend(f"  {_task_line(t)}" for t in overdue)
        parts.append("")

    if habits:
        done_today = {e["habit"] for e in log if e["date"] == today_iso and e["done"]}
        undone = [h["name"] for h in habits if h["name"] not in done_today]
        parts.append("🔁 Чек-ин привычек:")
        for h in habits:
            mark = "✅" if h["name"] in done_today else "⬜"
            parts.append(f"  {mark} {h['name']}")
        if undone:
            parts.append(f"\nНе отмечены: {', '.join(undone)}. Отметить можно в приложении или одной фразой мне.")

    parts.append("\nХорошего вечера! 🌆")
    return "\n".join(parts).strip()
