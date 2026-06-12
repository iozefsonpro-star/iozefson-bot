import os
import io
import logging
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import json
import anthropic
from telegram import Update, Document, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from notion_client import Client as NotionClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build as gcal_build

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY")
NOTION_TOKEN          = os.environ.get("NOTION_TOKEN")
NOTION_TODOLIST_DB_ID = os.environ.get("NOTION_TODOLIST_DB_ID")
OWNER_CHAT_ID         = os.environ.get("OWNER_CHAT_ID")
GOOGLE_TOKEN_JSON     = os.environ.get("GOOGLE_TOKEN_JSON")

_missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "NOTION_TOKEN": NOTION_TOKEN,
    "NOTION_TODOLIST_DB_ID": NOTION_TODOLIST_DB_ID,
}.items() if not v]

if _missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(_missing)}"
    )

MODEL       = "claude-haiku-4-5-20251001"
MODEL_SMART = "claude-sonnet-4-6"
ROME_TZ     = pytz.timezone("Europe/Rome")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты Паола — персональный ассистент Юлии Йозефсон. "
    "Говоришь по-русски, тон партнёрский и прямой. "
    "Без подхалимства, без лишних слов. "
    "У тебя есть прямой доступ к Notion (задачи) и Google Calendar Юлии. "
    "Когда задача уже создана ботом — просто подтверди коротко. "
    "Никогда не говори 'у меня нет доступа к Notion' — это неправда. "
    "Отвечай только plain text и эмодзи, без Markdown звёздочек.\n\n"
    "ПРАВИЛО ВСТРЕЧ: при любом упоминании встреч из календаря — "
    "всегда ставь эмодзи по типу календаря ПЕРЕД временем. "
    "Формат строго: {эмодзи} {время} — {название}. "
    "Никогда не заменяй эмодзи на тире или точку. "
    "💼=Business/Generali, 👥=Networking, 🏥=Salute, 💅=Beauty, "
    "🧠=Mental efficiency, ✈️=Viaggi, 🏠=Vita/Work Vin, 🎂=Birthday, 📌=остальное."
)

_pending_overdue_by_user: dict[int, list] = {}

# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------
notion = NotionClient(auth=NOTION_TOKEN)

ZONES     = ["💼 Бизнес", "👥 Networking", "🏥 Salute", "🧠 Mental",
             "🏠 Family", "💅 Beauty", "✈️ Viaggi", "📚 Ресурсы",
             "✍️ Контент", "💰 Финансы"]
PRIORITIES     = ["❗ Важное", "✅ Обычное", "🔜 Когда-нибудь"]
PRIORITY_ICONS = {"❗ Важное": "🔴", "✅ Обычное": "🟢", "🔜 Когда-нибудь": "⚪"}
PRIORITY_ORDER = ["❗ Важное", "✅ Обычное", "🔜 Когда-нибудь"]
PROJECTS  = ["Generali", "Second Brain", "Развитие бизнеса", "Личное"]


def _task_line(t: dict) -> str:
    icon      = PRIORITY_ICONS.get(t.get("priority", ""), "⚪")
    deadline  = f" — до {t['deadline']}" if t.get("deadline") else ""
    performer = t.get("performer", "")
    who       = f" (→ {performer})" if performer and performer != "Юля" else ""
    return f"{icon} {t['title']}{deadline}{who}"


def notion_create_task(
    title: str,
    deadline: str | None,
    priority: str,
    zone: str,
    project: str | None,
    comment: str | None,
) -> dict:
    priority_map = {
        "важное": "❗ Важное",
        "обычное": "✅ Обычное",
        "когда-нибудь": "🔜 Когда-нибудь",
    }
    priority = priority_map.get(priority.lower().strip(), "✅ Обычное")

    zone_map = {
        "бизнес": "💼 Бизнес", "networking": "👥 Networking",
        "нетворкинг": "👥 Networking", "salute": "🏥 Salute",
        "здоровье": "🏥 Salute", "mental": "🧠 Mental",
        "mental efficiency": "🧠 Mental", "обучение": "🧠 Mental",
        "family": "🏠 Family", "семья": "🏠 Family",
        "beauty": "💅 Beauty", "viaggi": "✈️ Viaggi",
        "путешествия": "✈️ Viaggi", "ресурсы": "📚 Ресурсы",
        "контент": "✍️ Контент", "финансы": "💰 Финансы",
    }
    zone = zone_map.get(zone.lower().strip(), zone)

    properties = {
        "Задача":     {"title": [{"text": {"content": title}}]},
        "Приоритет":  {"select": {"name": priority}},
        "Статус":     {"select": {"name": "To do"}},
        "Зона":       {"select": {"name": zone}},
        "Кто делает": {"select": {"name": "Юля"}},
    }
    if deadline:
        properties["Дедлайн"] = {"date": {"start": deadline}}
    if project and project in PROJECTS:
        properties["Проект"] = {"select": {"name": project}}
    if comment:
        properties["Комментарий"] = {"rich_text": [{"text": {"content": comment}}]}

    return notion.pages.create(
        parent={"database_id": NOTION_TODOLIST_DB_ID},
        properties=properties,
    )


def notion_get_tasks() -> list[dict]:
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"or": [
            {"property": "Статус", "select": {"equals": "To do"}},
            {"property": "Статус", "select": {"equals": "In progress"}},
        ]},
        "sorts": [{"property": "Приоритет", "direction": "ascending"}],
    })
    tasks = []
    for page in response.get("results", []):
        props     = page.get("properties", {})
        title     = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        deadline  = (props.get("Дедлайн", {}).get("date") or {}).get("start", "")
        priority  = (props.get("Приоритет", {}).get("select") or {}).get("name", "")
        status    = (props.get("Статус", {}).get("select") or {}).get("name", "")
        zone      = (props.get("Зона", {}).get("select") or {}).get("name", "")
        project   = (props.get("Проект", {}).get("select") or {}).get("name", "")
        performer = (props.get("Кто делает", {}).get("select") or {}).get("name", "")
        tasks.append({
            "id": page["id"], "title": title, "deadline": deadline,
            "priority": priority, "status": status, "zone": zone,
            "project": project, "performer": performer,
        })
    return tasks


def notion_get_sospeso() -> list[dict]:
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Статус", "select": {"equals": "Sospeso"}},
    })
    tasks = []
    for page in response.get("results", []):
        props     = page.get("properties", {})
        title     = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        priority  = (props.get("Приоритет", {}).get("select") or {}).get("name", "")
        performer = (props.get("Кто делает", {}).get("select") or {}).get("name", "")
        tasks.append({"id": page["id"], "title": title, "priority": priority, "performer": performer})
    return tasks


def notion_close_task(title: str) -> bool:
    results = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Задача", "rich_text": {"contains": title}},
    }).get("results", [])
    if not results:
        return False
    notion.pages.update(page_id=results[0]["id"],
                        properties={"Статус": {"select": {"name": "Done"}}})
    return True


def notion_update_deadline(title: str, new_date: str) -> bool:
    results = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Задача", "rich_text": {"contains": title}},
    }).get("results", [])
    if not results:
        return False
    notion.pages.update(
        page_id=results[0]["id"],
        properties={"Дедлайн": {"date": {"start": new_date}}},
    )
    return True


def notion_close_task_by_id(page_id: str) -> bool:
    try:
        notion.pages.update(page_id=page_id,
                            properties={"Статус": {"select": {"name": "Done"}}})
        return True
    except Exception:
        return False


def notion_update_deadline_by_id(page_id: str, new_date: str) -> bool:
    try:
        notion.pages.update(page_id=page_id,
                            properties={"Дедлайн": {"date": {"start": new_date}}})
        return True
    except Exception:
        return False


def find_task_by_description(description: str, tasks: list[dict]) -> dict | None:
    """Fuzzy match: Claude picks the best task from the list by semantic similarity."""
    if not tasks:
        return None
    task_list = "\n".join(f"{i + 1}. {t['title']}" for i, t in enumerate(tasks))
    prompt = (
        f"Пользователь описывает задачу: «{description}»\n\n"
        f"Список задач:\n{task_list}\n\n"
        "Какой номер задачи лучше всего соответствует описанию? "
        "Учти семантическое сходство — название задачи может сильно отличаться от описания пользователя. "
        "Ответь ТОЛЬКО одной цифрой (1, 2, 3...). Если ни одна не подходит — ответь 0."
    )
    try:
        result = ask_claude(prompt, description, model=MODEL_SMART).strip()
        idx = int(result) - 1
        if 0 <= idx < len(tasks):
            return tasks[idx]
    except Exception:
        pass
    return None


def notion_get_overdue() -> list[dict]:
    # "before" today — tasks with deadline today go to "planned", not overdue
    today = datetime.now(ROME_TZ).strftime("%Y-%m-%d")
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"and": [
            {"or": [
                {"property": "Статус", "select": {"equals": "To do"}},
                {"property": "Статус", "select": {"equals": "In progress"}},
            ]},
            {"property": "Дедлайн", "date": {"before": today}},
        ]},
    })
    tasks = []
    for page in response.get("results", []):
        props     = page.get("properties", {})
        title     = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        deadline  = (props.get("Дедлайн", {}).get("date") or {}).get("start", "")
        priority  = (props.get("Приоритет", {}).get("select") or {}).get("name", "")
        performer = (props.get("Кто делает", {}).get("select") or {}).get("name", "")
        tasks.append({"id": page["id"], "title": title, "deadline": deadline,
                      "priority": priority, "performer": performer})
    return tasks


def notion_get_done_today() -> list[dict]:
    today = datetime.now(ROME_TZ).strftime("%Y-%m-%d")
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Статус", "select": {"equals": "Done"}},
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        "page_size": 50,
    })
    tasks = []
    for page in response.get("results", []):
        edited_utc = page.get("last_edited_time", "")
        if not edited_utc:
            continue
        try:
            edited_rome = datetime.fromisoformat(
                edited_utc.replace("Z", "+00:00")
            ).astimezone(ROME_TZ).strftime("%Y-%m-%d")
        except Exception:
            edited_rome = edited_utc[:10]
        if edited_rome != today:
            continue  # skip older tasks, but keep scanning (no break)
        props = page.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        if title:
            tasks.append({"id": page["id"], "title": title})
    return tasks


def notion_get_tomorrow_important() -> list[dict]:
    import datetime as dt_module
    tomorrow = (dt_module.date.today() + dt_module.timedelta(days=1)).strftime("%Y-%m-%d")
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"and": [
            {"or": [
                {"property": "Статус", "select": {"equals": "To do"}},
                {"property": "Статус", "select": {"equals": "In progress"}},
            ]},
            {"property": "Дедлайн", "date": {"equals": tomorrow}},
            {"property": "Приоритет", "select": {"equals": "❗ Важное"}},
        ]},
    })
    tasks = []
    for page in response.get("results", []):
        props     = page.get("properties", {})
        title     = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        performer = (props.get("Кто делает", {}).get("select") or {}).get("name", "")
        tasks.append({"id": page["id"], "title": title, "performer": performer})
    return tasks


def notion_get_done_this_week() -> list[dict]:
    """Tasks with status Done, last-edited Mon–Fri of current week (Rome TZ)."""
    import datetime as dt_module
    now        = datetime.now(ROME_TZ)
    week_start = now.date() - dt_module.timedelta(days=now.weekday())  # Monday
    week_end   = week_start + dt_module.timedelta(days=4)              # Friday

    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Статус", "select": {"equals": "Done"}},
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        "page_size": 100,
    })
    tasks = []
    for page in response.get("results", []):
        edited_utc = page.get("last_edited_time", "")
        if not edited_utc:
            continue
        try:
            edited_date = datetime.fromisoformat(
                edited_utc.replace("Z", "+00:00")
            ).astimezone(ROME_TZ).date()
        except Exception:
            continue
        if not (week_start <= edited_date <= week_end):
            continue
        props = page.get("properties", {})
        title = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        zone  = (props.get("Зона", {}).get("select") or {}).get("name", "")
        if title:
            tasks.append({"id": page["id"], "title": title, "zone": zone})
    return tasks


def notion_get_undone_deadline_this_week() -> list[dict]:
    """Tasks with deadline Mon–Fri of current week, status NOT Done."""
    import datetime as dt_module
    now        = datetime.now(ROME_TZ)
    week_start = now.date() - dt_module.timedelta(days=now.weekday())
    week_end   = week_start + dt_module.timedelta(days=4)

    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"and": [
            {"or": [
                {"property": "Статус", "select": {"equals": "To do"}},
                {"property": "Статус", "select": {"equals": "In progress"}},
                {"property": "Статус", "select": {"equals": "Sospeso"}},
            ]},
            {"property": "Дедлайн", "date": {"on_or_after": str(week_start)}},
            {"property": "Дедлайн", "date": {"on_or_before": str(week_end)}},
        ]},
    })
    tasks = []
    for page in response.get("results", []):
        props    = page.get("properties", {})
        title    = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        zone     = (props.get("Зона", {}).get("select") or {}).get("name", "")
        deadline = (props.get("Дедлайн", {}).get("date") or {}).get("start", "")
        if title:
            tasks.append({"id": page["id"], "title": title, "zone": zone, "deadline": deadline})
    return tasks


def notion_get_next_week_tasks() -> dict:
    """For Sunday digest: tasks grouped for next Mon–Fri."""
    import datetime as dt_module
    now        = datetime.now(ROME_TZ)
    days_ahead = (7 - now.weekday()) % 7 or 7  # days until next Monday
    next_mon   = now.date() + dt_module.timedelta(days=days_ahead)
    next_fri   = next_mon + dt_module.timedelta(days=4)

    deadline_resp = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"and": [
            {"or": [
                {"property": "Статус", "select": {"equals": "To do"}},
                {"property": "Статус", "select": {"equals": "In progress"}},
                {"property": "Статус", "select": {"equals": "Sospeso"}},
            ]},
            {"property": "Дедлайн", "date": {"on_or_after": str(next_mon)}},
            {"property": "Дедлайн", "date": {"on_or_before": str(next_fri)}},
        ]},
    })
    no_deadline_resp = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"and": [
            {"or": [
                {"property": "Статус", "select": {"equals": "To do"}},
                {"property": "Статус", "select": {"equals": "In progress"}},
            ]},
            {"property": "Дедлайн", "date": {"is_empty": True}},
        ]},
    })

    def _parse(page: dict) -> dict | None:
        props    = page.get("properties", {})
        title    = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        zone     = (props.get("Зона", {}).get("select") or {}).get("name", "")
        priority = (props.get("Приоритет", {}).get("select") or {}).get("name", "")
        return {"title": title, "zone": zone, "priority": priority} if title else None

    deadline_tasks  = [t for p in deadline_resp.get("results", []) if (t := _parse(p))]
    no_dl_tasks     = [t for p in no_deadline_resp.get("results", []) if (t := _parse(p))]
    important_tasks = [t for t in no_dl_tasks if t["priority"] == "❗ Важное"]
    queue_tasks     = [t for t in no_dl_tasks if t["priority"] != "❗ Важное"][:5]

    return {
        "deadline_tasks":  deadline_tasks,
        "important_tasks": important_tasks,
        "queue_tasks":     queue_tasks,
        "next_mon":        next_mon,
        "next_fri":        next_fri,
    }


def format_tasks_list(tasks: list[dict]) -> str:
    if not tasks:
        return "✅ Активных задач нет."
    lines = [f"📋 Активные задачи (всего: {len(tasks)}):\n"]
    for t in tasks:
        zone = f" [{t['zone']}]" if t.get("zone") else ""
        lines.append(f"{_task_line(t)}{zone}")
    return "\n".join(lines)


def build_notion_context(tasks: list[dict]) -> str:
    if not tasks:
        return "Список задач пуст."
    lines = ["Текущие задачи Юлии:"]
    for t in tasks:
        deadline  = f", дедлайн {t['deadline']}" if t.get("deadline") else ""
        zone      = f", зона: {t['zone']}" if t.get("zone") else ""
        performer = t.get("performer", "")
        who       = f", кто делает: {performer}" if performer and performer != "Юля" else ""
        lines.append(f"- {t['title']} [{t['priority']}{deadline}{zone}{who}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def ask_claude(system: str, user_message: str, model: str = MODEL, max_tokens: int = 1024) -> str:
    response = claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def clean_markdown(text: str) -> str:
    text = text.replace("**", "").replace("__", "")
    text = text.replace("* ", "• ").replace("*", "")
    text = text.replace("`", "")
    return text


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GCAL_EMOJI = {
    "Business": "💼", "Networking": "👥", "Salute": "🏥",
    "Beauty": "💅", "Mental efficiency": "🧠", "Viaggi": "✈️",
    "Астро-календарь": "🌙", "Work Vin": "🏠", "Vita": "🏠",
    "My calendar": "🏠", "Birthday": "🎂",
}
GCAL_SKIP       = {"Tasks", "Ciclo Yuliya", "Астро-календарь"}
GENERALI_DOMAIN = "@agmonza.it"
LONG_EVENT_DAYS = 3


def _get_gcal_credentials() -> Credentials | None:
    if not GOOGLE_TOKEN_JSON:
        return None
    try:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token_data, GCAL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds
    except Exception as e:
        logger.error("Google credentials error: %s", e)
        return None


def _weekday_ru(s: str) -> str:
    return (s.replace("Monday","Пн").replace("Tuesday","Вт").replace("Wednesday","Ср")
             .replace("Thursday","Чт").replace("Friday","Пт")
             .replace("Saturday","Сб").replace("Sunday","Вс"))


def _month_ru(s: str) -> str:
    return (s.replace("Jan","янв").replace("Feb","фев").replace("Mar","мар")
             .replace("Apr","апр").replace("May","май").replace("Jun","июн")
             .replace("Jul","июл").replace("Aug","авг").replace("Sep","сен")
             .replace("Oct","окт").replace("Nov","ноя").replace("Dec","дек"))


def get_calendar_events(days: int = 7, from_now: bool = False) -> dict:
    creds = _get_gcal_credentials()
    if not creds:
        return {}
    try:
        from datetime import timedelta, date as date_cls
        service = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
        now     = datetime.now(ROME_TZ)
        if from_now:
            time_min = now.isoformat()
        else:
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        time_max = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59).isoformat()

        cal_list  = service.calendarList().list().execute().get("items", [])
        by_date: dict[str, list] = {}
        long_evs: list = []
        birthdays: list = []

        for cal in cal_list:
            cal_name = cal.get("summary", "")
            if cal_name in GCAL_SKIP:
                continue
            emoji = GCAL_EMOJI.get(cal_name, "📌")

            try:
                result = service.events().list(
                    calendarId=cal["id"], timeMin=time_min, timeMax=time_max,
                    singleEvents=True, orderBy="startTime", maxResults=20,
                ).execute()

                for ev in result.get("items", []):
                    title = ev.get("summary", "(без названия)")
                    if title.strip().upper() == "BBR":
                        continue

                    start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
                    end_raw   = ev["end"].get("dateTime", ev["end"].get("date", ""))

                    if "T" in start_raw:
                        dt_start   = datetime.fromisoformat(start_raw).astimezone(ROME_TZ)
                        dt_end     = datetime.fromisoformat(end_raw).astimezone(ROME_TZ)
                        date_str   = dt_start.strftime("%d.%m")
                        time_range = f"{dt_start.strftime('%H:%M')}–{dt_end.strftime('%H:%M')}"
                        sort_key   = dt_start.isoformat()
                        duration_d = (dt_end - dt_start).days
                    else:
                        from datetime import date as date_cls2
                        d_start    = date_cls2.fromisoformat(start_raw)
                        d_end      = date_cls2.fromisoformat(end_raw)
                        date_str   = d_start.strftime("%d.%m")
                        time_range = "весь день"
                        sort_key   = start_raw
                        duration_d = (d_end - d_start).days

                    day_key = sort_key[:10]

                    if duration_d >= LONG_EVENT_DAYS:
                        long_evs.append({"title": title, "end_raw": end_raw})
                        continue

                    if cal_name == "Birthday":
                        birthdays.append({"date_label": date_str, "title": title})
                        continue

                    attendees = ev.get("attendees", [])
                    ev_emoji  = emoji
                    if any(GENERALI_DOMAIN in a.get("email", "") for a in attendees):
                        ev_emoji = "💼"

                    by_date.setdefault(day_key, []).append({
                        "date_label": date_str, "time": time_range,
                        "title": title, "emoji": ev_emoji, "_sort": sort_key,
                    })

            except Exception as e:
                logger.warning("Calendar '%s' error: %s", cal_name, e)

        for evs in by_date.values():
            evs.sort(key=lambda x: x["_sort"])

        return {"by_date": by_date, "long": long_evs, "birthdays": birthdays}

    except Exception as e:
        logger.error("Google Calendar error: %s", e)
        return {}


def format_calendar_events(data: dict) -> str:
    if not data:
        return ""
    from datetime import date as date_cls
    lines = []
    by_date = data.get("by_date", {})
    for day_key in sorted(by_date.keys()):
        evs = by_date[day_key]
        if not evs:
            continue
        try:
            d       = date_cls.fromisoformat(day_key)
            weekday = _weekday_ru(d.strftime("%A"))
            lines.append(f"\n📅 {weekday}, {d.strftime('%d.%m')}")
        except Exception:
            lines.append(f"\n📅 {day_key}")
        for ev in evs:
            lines.append(f"{ev['emoji']} {ev['time']} — {ev['title']}")

    if data.get("long"):
        lines.append("\n📚 В процессе:")
        for ev in data["long"]:
            try:
                end_d   = date_cls.fromisoformat(ev["end_raw"][:10])
                end_fmt = _month_ru(end_d.strftime("до %d %b"))
            except Exception:
                end_fmt = f"до {ev['end_raw'][:10]}"
            lines.append(f"  {ev['title']} ({end_fmt})")

    if data.get("birthdays"):
        lines.append("\n🎂 Дни рождения:")
        for ev in data["birthdays"]:
            lines.append(f"  {ev['date_label']} — {ev['title']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Дайджесты
# ---------------------------------------------------------------------------

def _sort_by_priority(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: PRIORITY_ORDER.index(t["priority"])
                  if t.get("priority") in PRIORITY_ORDER else 2)


async def _send_digest(bot: Bot, digest_type: str) -> None:
    if not OWNER_CHAT_ID:
        return
    try:
        now      = datetime.now(ROME_TZ)
        now_str  = _weekday_ru(now.strftime("%d.%m.%Y (%A)"))
        today_iso = now.strftime("%Y-%m-%d")

        if digest_type == "morning":
            tasks   = notion_get_tasks()
            overdue = notion_get_overdue()
            sospeso = notion_get_sospeso()
            events  = get_calendar_events(days=0)
            cal_text = format_calendar_events(events).strip()

            tasks_today = _sort_by_priority(
                [t for t in tasks if t.get("deadline") == today_iso]
            )

            parts = [f"Доброе утро, Юлия! {now_str}\n"]

            # 1. Встречи
            if cal_text:
                parts.append(cal_text)
                parts.append("")

            # 2. Запланировано на сегодня
            if tasks_today:
                parts.append(f"📋 Запланировано на сегодня ({len(tasks_today)}):")
                for t in tasks_today:
                    parts.append(f"  {_task_line(t)}")
            else:
                parts.append("📋 Задач с дедлайном сегодня нет.")
            parts.append("")

            # 3. Просрочено
            if overdue:
                parts.append(f"⚠️ Просрочено ({len(overdue)}):")
                for t in overdue:
                    parts.append(f"  {_task_line(t)}")
                parts.append("")

            # 4. Подвисшие
            if sospeso:
                parts.append(f"🔄 Подвисшие ({len(sospeso)}):")
                for t in sospeso:
                    who = f" (→ {t['performer']})" if t.get("performer") and t["performer"] != "Юля" else ""
                    parts.append(f"  ⚫ {t['title']}{who}")
                parts.append("")

            text = "\n".join(parts).strip()

        else:  # evening
            done_today         = notion_get_done_today()
            overdue            = notion_get_overdue()
            tomorrow_important = notion_get_tomorrow_important()

            parts = [f"🌙 Вечерняя сводка — {now_str}\n"]

            # 1. Сделано сегодня
            if done_today:
                parts.append(f"✅ Сделано сегодня ({len(done_today)}):")
                for t in done_today:
                    parts.append(f"  • {t['title']}")
                parts.append("")

            # 2. Просрочено — закрыть или перенести
            if overdue:
                parts.append(f"⚠️ Просрочено ({len(overdue)}) — закрыть или перенести?")
                for t in overdue:
                    parts.append(f"  {_task_line(t)}")
                parts.append("")
                _pending_overdue_by_user[int(OWNER_CHAT_ID)] = list(overdue)

            # 3. Важное на завтра
            if tomorrow_important:
                parts.append(f"❗ Важное на завтра ({len(tomorrow_important)}):")
                for t in tomorrow_important:
                    who = f" (→ {t['performer']})" if t.get("performer") and t["performer"] != "Юля" else ""
                    parts.append(f"  🔴 {t['title']}{who}")
                parts.append("")

            closing = clean_markdown(ask_claude(
                SYSTEM_PROMPT,
                f"Сегодня {now_str}. Добавь 1 предложение тёплого завершения дня. Plain text.",
            ))
            parts.append(closing)
            text = "\n".join(parts).strip()

        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
        logger.info("%s digest sent", digest_type)

    except Exception as e:
        logger.error("%s digest error: %s", digest_type, e)


def build_intraday_digest() -> str:
    now       = datetime.now(ROME_TZ)
    today_iso = now.strftime("%Y-%m-%d")
    now_str   = _weekday_ru(now.strftime("%d.%m.%Y (%A)"))

    tasks      = notion_get_tasks()
    overdue    = notion_get_overdue()
    sospeso    = notion_get_sospeso()
    done_today = notion_get_done_today()
    events     = get_calendar_events(days=0, from_now=True)

    tasks_today = _sort_by_priority(
        [t for t in tasks if t.get("deadline") == today_iso]
    )

    parts = [f"📊 Сводка — {now_str}\n"]

    # 1. Встречи (только предстоящие, from_now=True уже фильтрует)
    today_events = events.get("by_date", {}).get(today_iso, [])
    if today_events:
        parts.append("📅 Предстоит сегодня:")
        for ev in today_events:
            parts.append(f"  {ev['emoji']} {ev['time']} — {ev['title']}")
        parts.append("")

    # 2. Сделано сегодня
    if done_today:
        parts.append(f"✅ Сделано ({len(done_today)}):")
        for t in done_today:
            parts.append(f"  • {t['title']}")
        parts.append("")

    # 3. Запланировано на сегодня
    if tasks_today:
        parts.append(f"📋 Запланировано на сегодня ({len(tasks_today)}):")
        for t in tasks_today:
            parts.append(f"  {_task_line(t)}")
        parts.append("")

    # 4. Просрочено
    if overdue:
        parts.append(f"⚠️ Просрочено ({len(overdue)}):")
        for t in overdue:
            parts.append(f"  {_task_line(t)}")
        parts.append("")

    # 5. Подвисшие
    if sospeso:
        parts.append(f"🔄 Подвисшие ({len(sospeso)}):")
        for t in sospeso:
            who = f" (→ {t['performer']})" if t.get("performer") and t["performer"] != "Юля" else ""
            parts.append(f"  ⚫ {t['title']}{who}")
        parts.append("")

    return "\n".join(parts).strip()


async def send_friday_digest(bot: Bot) -> None:
    """Пятничная вечерняя сводка — итоги недели (расширенный формат по ТЗ)."""
    if not OWNER_CHAT_ID:
        return
    try:
        import datetime as dt_module
        now     = datetime.now(ROME_TZ)
        now_str = now.strftime("%d.%m.%Y")

        week_start = now.date() - dt_module.timedelta(days=now.weekday())
        saturday   = week_start + dt_module.timedelta(days=5)
        sunday     = week_start + dt_module.timedelta(days=6)

        done_week        = notion_get_done_this_week()
        undone_this_week = notion_get_undone_deadline_this_week()
        done_today       = notion_get_done_today()

        events      = get_calendar_events(days=3)
        cal_by_date = events.get("by_date", {})
        sat_events  = cal_by_date.get(str(saturday), [])
        sun_events  = cal_by_date.get(str(sunday), [])

        parts = [f"📊 Итоги недели — {now_str} (Пт)\n"]

        # 1. Сделано за неделю — grouped by Зона
        if done_week:
            parts.append("✅ Сделано за неделю:")
            by_zone: dict[str, list[str]] = {}
            for t in done_week:
                zone = t.get("zone") or "📌 Без зоны"
                by_zone.setdefault(zone, []).append(t["title"])
            for zone, titles in by_zone.items():
                parts.append(f"[{zone}]: {', '.join(titles)}")

            top_zone = max(by_zone, key=lambda z: len(by_zone[z]))
            focus = clean_markdown(ask_claude(
                SYSTEM_PROMPT,
                f"На этой неделе больше всего задач закрыто в зоне '{top_zone}'. "
                "Напиши одно предложение: 'На этой неделе фокус был на [зона]'. Только plain text.",
            ))
            parts.append(f"\n{focus}")
        else:
            parts.append("✅ Сделано за неделю: —")
        parts.append("")

        # 2. Переносится (только если есть)
        if undone_this_week:
            parts.append("⏩ Переносится на следующую неделю:")
            for t in undone_this_week:
                zone = f"[{t['zone']}] " if t.get("zone") else ""
                parts.append(f"• {zone}{t['title']}")
            parts.append("")

        # 3. Выходные
        parts.append("📅 Выходные:")
        sat_fmt  = saturday.strftime("%d.%m")
        sun_fmt  = sunday.strftime("%d.%m")
        sat_line = (", ".join(f"{ev['emoji']} {ev['time']} — {ev['title']}" for ev in sat_events)
                    if sat_events else "свободно")
        sun_line = (", ".join(f"{ev['emoji']} {ev['time']} — {ev['title']}" for ev in sun_events)
                    if sun_events else "свободно")
        parts.append(f"Сб {sat_fmt} — {sat_line}")
        parts.append(f"Вс {sun_fmt} — {sun_line}")
        parts.append("")

        # 4. Сделано сегодня
        if done_today:
            parts.append(f"✅ Сделано сегодня ({len(done_today)}):")
            for t in done_today:
                parts.append(f"• {t['title']}")
            parts.append("")

        parts.append("Хорошего уик-энда! 🌅")

        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text="\n".join(parts).strip())
        logger.info("Friday digest sent")

    except Exception as e:
        logger.error("Friday digest error: %s", e)


async def send_sunday_digest(bot: Bot) -> None:
    """Воскресная сводка — старт следующей рабочей недели."""
    if not OWNER_CHAT_ID:
        return
    try:
        import datetime as dt_module
        now     = datetime.now(ROME_TZ)
        now_str = now.strftime("%d.%m.%Y")

        week_data = notion_get_next_week_tasks()
        next_mon  = week_data["next_mon"]
        next_fri  = week_data["next_fri"]

        days_to_mon = (next_mon - now.date()).days
        events      = get_calendar_events(days=days_to_mon + 5)
        cal_by_date = events.get("by_date", {})

        weekdays_ru = ["Пн", "Вт", "Ср", "Чт", "Пт"]
        cal_lines   = []
        for i, wd in enumerate(weekdays_ru):
            day      = next_mon + dt_module.timedelta(days=i)
            day_fmt  = day.strftime("%d.%m")
            day_evs  = cal_by_date.get(str(day), [])
            ev_str   = (", ".join(f"{ev['emoji']} {ev['time']} — {ev['title']}" for ev in day_evs)
                        if day_evs else "свободно")
            cal_lines.append(f"{wd} {day_fmt} — {ev_str}")

        all_week_tasks = week_data["deadline_tasks"] + week_data["important_tasks"]
        queue_tasks    = week_data["queue_tasks"]

        zone_count: dict[str, int] = {}
        for t in all_week_tasks:
            z = t.get("zone") or "📌 Без зоны"
            zone_count[z] = zone_count.get(z, 0) + 1

        parts = [f"📅 Старт недели — {now_str} (Вс)\n"]
        parts.append("Впереди 5 рабочих дней\n")

        parts.append("🗓 Календарь недели:")
        parts.extend(cal_lines)
        parts.append("")

        if all_week_tasks:
            parts.append("🎯 Задачи на эту неделю:")
            for t in all_week_tasks:
                zone = f"[{t['zone']}] " if t.get("zone") else ""
                parts.append(f"• {zone}{t['title']}")
            parts.append("")

        if queue_tasks:
            parts.append("🔜 Висит в очереди:")
            for t in queue_tasks:
                zone = f"[{t['zone']}] " if t.get("zone") else ""
                parts.append(f"• {zone}{t['title']}")
            parts.append("")

        if zone_count:
            top_zone = max(zone_count, key=lambda z: zone_count[z])
            focus = clean_markdown(ask_claude(
                SYSTEM_PROMPT,
                f"На следующей неделе больше всего задач в зоне '{top_zone}'. "
                "Напиши одно предложение про фокус недели. Только plain text.",
            ))
        else:
            focus = "Хорошая неделя для планирования."
        parts.append("💭 Фокус недели:")
        parts.append(focus)

        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text="\n".join(parts).strip())
        logger.info("Sunday digest sent")

    except Exception as e:
        logger.error("Sunday digest error: %s", e)


async def send_morning_digest(bot: Bot) -> None:
    await _send_digest(bot, "morning")


async def send_evening_digest(bot: Bot) -> None:
    await _send_digest(bot, "evening")


# ---------------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я Паола, твой ассистент.\n\n"
        "Просто говори — что нужно сделать, что запомнить, что показать.\n"
        "Я запишу в Notion и отвечу по делу.\n\n"
        "Примеры:\n"
        "• Записаться к врачу до пятницы\n"
        "• Покажи активные задачи\n"
        "• Закрой задачу про отчёт\n"
        "• Перенеси задачу X на 15 июня"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.message.text or ""
    doc     = update.message.document

    if doc:
        await update.message.reply_text("⏳ Читаю файл...")
        try:
            file_text = await _extract_text_from_document(doc, context.bot)
            message = (f"{message}\n\n[{doc.file_name}]:\n{file_text}"
                       if message else f"[{doc.file_name}]:\n{file_text}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при чтении файла: {e}")
            return

    if not message.strip():
        await update.message.reply_text("Напиши что нужно — я помогу.")
        return

    await update.message.chat.send_action("typing")

    import datetime as dt_module
    today_date   = dt_module.date.today()
    tomorrow_str = (today_date + dt_module.timedelta(days=1)).strftime("%Y-%m-%d")
    today_iso    = today_date.strftime("%Y-%m-%d")

    if user_id in _pending_overdue_by_user:
        context.user_data["pending_overdue"] = _pending_overdue_by_user.pop(user_id)
        if context.user_data["pending_overdue"]:
            context.user_data["last_task"] = context.user_data["pending_overdue"][0]["title"]

    pending_tasks = context.user_data.get("pending_overdue", [])
    last_task     = context.user_data.get("last_task", "")
    task_title    = last_task or (pending_tasks[0]["title"] if pending_tasks else "")
    msg_lower     = message.lower()

    if task_title:
        handled = False

        task_id = pending_tasks[0]["id"] if pending_tasks else None

        if any(kw in msg_lower for kw in ["уже сделано", "закрой", "выполнено", "сделано", "готово", "done"]):
            done = notion_close_task_by_id(task_id) if task_id else notion_close_task(task_title)
            await update.message.reply_text(
                f"✅ Закрыла: {task_title}" if done else f"❌ Не нашла: {task_title}"
            )
            handled = True

        elif any(kw in msg_lower for kw in ["да", "перенеси", "перенести", "завтра", "yes"]):
            done = (notion_update_deadline_by_id(task_id, tomorrow_str) if task_id
                    else notion_update_deadline(task_title, tomorrow_str))
            await update.message.reply_text(
                f"✅ Перенесла на {tomorrow_str}: {task_title}" if done
                else f"❌ Не нашла: {task_title}"
            )
            handled = True

        elif any(kw in msg_lower for kw in ["на ", "до ", "июня", "июля", "августа",
                                              "сентября", "октября", "ноября", "декабря",
                                              "января", "февраля", "марта", "апреля", "мая"]):
            try:
                parsed_date = ask_claude(
                    f"Сегодня {today_iso}. Из сообщения извлеки дату и верни ТОЛЬКО YYYY-MM-DD. Если нет — NONE.",
                    message, model=MODEL_SMART,
                ).strip()
                if parsed_date != "NONE" and len(parsed_date) == 10:
                    done = (notion_update_deadline_by_id(task_id, parsed_date) if task_id
                            else notion_update_deadline(task_title, parsed_date))
                    await update.message.reply_text(
                        f"✅ Перенесла на {parsed_date}: {task_title}" if done
                        else f"❌ Не нашла: {task_title}"
                    )
                    handled = True
            except Exception as e:
                logger.error("Date parse error: %s", e)

        if handled:
            if pending_tasks:
                pending_tasks.pop(0)
            context.user_data["pending_overdue"] = pending_tasks
            if pending_tasks:
                next_t = pending_tasks[0]
                dl = f" (дедлайн {next_t['deadline']})" if next_t.get("deadline") else ""
                context.user_data["last_task"] = next_t["title"]
                await update.message.reply_text(
                    f"Следующая: {next_t['title']}{dl}\nПеренести? (да / дата / закрой)"
                )
            else:
                context.user_data.pop("last_task", None)
                context.user_data.pop("pending_overdue", None)
            return

    # Keyword shortcut — не полагаемся на Claude для однозначных запросов
    _digest_kw = ["сводку", "сводка", "дайджест", "что сегодня", "покажи день",
                  "план на день", "что у меня сегодня", "что у меня на сегодня"]
    if any(kw in msg_lower for kw in _digest_kw) and not context.user_data.get("pending_overdue"):
        try:
            await update.message.reply_text(build_intraday_digest())
        except Exception as e:
            logger.error("Intraday digest error: %s", e)
            await update.message.reply_text(f"Ошибка: {e}")
        return

    INTENT_PROMPT = (
        f"Сегодня {today_iso}. Сообщение: «{message}»\n\n"
        "Определи намерение. Ответь ТОЛЬКО одним из кодов:\n"
        "SHOW_DIGEST — сводка на день: встречи + задачи + прогресс. "
        "Примеры: 'сводку', 'что сегодня', 'покажи день', 'как день'\n"
        "SHOW_ALL — показать список всех активных задач (без встреч). "
        "Примеры: 'покажи все задачи', 'список задач'\n"
        "SHOW_OVERDUE — показать просроченные задачи\n"
        "CLOSE: <название> — закрыть задачу\n"
        "RESCHEDULE: <название> | <YYYY-MM-DD> — перенести дедлайн\n"
        "CREATE_TASK — создать задачу\n"
        "OTHER — всё остальное"
    )
    try:
        intent = ask_claude(INTENT_PROMPT, message, model=MODEL_SMART).strip()
    except Exception as e:
        logger.error("Intent error: %s", e)
        intent = "OTHER"

    logger.info("Intent: %s", intent)

    if intent.startswith("SHOW_DIGEST"):
        try:
            await update.message.reply_text(build_intraday_digest())
        except Exception as e:
            logger.error("Intraday digest error: %s", e)
            await update.message.reply_text(f"Ошибка: {e}")
        return

    if intent.startswith("SHOW_ALL"):
        tasks = notion_get_tasks()
        await update.message.reply_text(format_tasks_list(tasks))
        return

    if intent.startswith("SHOW_OVERDUE"):
        overdue = notion_get_overdue()
        if not overdue:
            await update.message.reply_text("✅ Просроченных задач нет!")
        else:
            lines = [f"⚠️ Просроченные ({len(overdue)}):"]
            for t in overdue:
                lines.append(f"  {_task_line(t)}")
            context.user_data["pending_overdue"] = overdue
            context.user_data["last_task"]       = overdue[0]["title"]
            lines.append(f"\nПеренести «{overdue[0]['title']}»? (да / дата / закрой)")
            await update.message.reply_text("\n".join(lines))
        return

    if intent.startswith("CLOSE:"):
        description = intent[6:].strip()
        all_tasks   = notion_get_tasks()
        matched     = find_task_by_description(description, all_tasks)
        if matched:
            done = notion_close_task_by_id(matched["id"])
            await update.message.reply_text(
                f"✅ Закрыла: {matched['title']}" if done
                else f"❌ Ошибка при закрытии: {matched['title']}"
            )
        else:
            await update.message.reply_text(
                f"❓ Не нашла похожую задачу по описанию «{description}». Уточни?"
            )
        return

    if intent.startswith("RESCHEDULE:"):
        parts = intent[11:].split("|")
        if len(parts) == 2:
            description = parts[0].strip()
            new_date    = parts[1].strip()
            all_tasks   = notion_get_tasks()
            matched     = find_task_by_description(description, all_tasks)
            if matched:
                done = notion_update_deadline_by_id(matched["id"], new_date)
                await update.message.reply_text(
                    f"✅ Перенесла на {new_date}: {matched['title']}" if done
                    else f"❌ Ошибка при переносе: {matched['title']}"
                )
            else:
                await update.message.reply_text(
                    f"❓ Не нашла задачу по описанию «{description}». Уточни?"
                )
            return

    if intent.startswith("CREATE_TASK"):
        parse_prompt = (
            f"Сегодня {today_iso}. Сообщение: «{message}»\n\n"
            "Извлеки параметры задачи. Для каждой задачи — отдельный блок через '---':\n"
            "TITLE: <название>\n"
            "DEADLINE: <YYYY-MM-DD или пусто>\n"
            "PRIORITY: <Важное|Обычное|Когда-нибудь>\n"
            f"ZONE: <одно из: {', '.join(ZONES)}>\n"
            f"PROJECT: <одно из: {', '.join(PROJECTS)} или пусто>\n"
            "COMMENT: <дополнительный контекст или пусто>\n\n"
            "Умолчания: PRIORITY=Обычное, ZONE=💼 Бизнес, PROJECT=пусто."
        )
        try:
            parsed = ask_claude(parse_prompt, message, model=MODEL_SMART)
            blocks = [b.strip() for b in parsed.strip().split("---") if b.strip()]
            created = []
            for block in blocks:
                kv = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                      for l in block.splitlines() if ":" in l}
                title = kv.get("TITLE", "").strip()
                if not title:
                    continue
                notion_create_task(
                    title    = title,
                    deadline = kv.get("DEADLINE", "").strip() or None,
                    priority = kv.get("PRIORITY", "Обычное").strip(),
                    zone     = kv.get("ZONE", "💼 Бизнес").strip(),
                    project  = kv.get("PROJECT", "").strip() or None,
                    comment  = kv.get("COMMENT", "").strip() or None,
                )
                dl = f", до {kv.get('DEADLINE')}" if kv.get("DEADLINE") else ""
                created.append(f"• {title}{dl}")
                logger.info("Task created: %s", title)

            if created:
                await update.message.reply_text(
                    f"✅ Добавлено в Notion ({len(created)}):\n" + "\n".join(created)
                )
                return
        except Exception as e:
            logger.error("Task creation error: %s", e)

    # Обычный ответ с контекстом задач и календаря
    try:
        tasks    = notion_get_tasks()
        events   = get_calendar_events(days=7)
        cal_text = format_calendar_events(events)
        now_str  = _weekday_ru(datetime.now(ROME_TZ).strftime("%d.%m.%Y (%A)"))

        system = (
            SYSTEM_PROMPT
            + f"\n\nСЕГОДНЯ: {now_str}"
            + f"\n\n{build_notion_context(tasks)}"
            + (f"\n\n{cal_text}" if cal_text else "")
        )
        reply = clean_markdown(ask_claude(system, message, model=MODEL_SMART))
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error("Claude error: %s", e)
        await update.message.reply_text(f"Ошибка: {e}")


async def _extract_text_from_document(doc: Document, bot) -> str:
    file = await bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    raw  = buf.read()
    mime = doc.mime_type or ""

    if mime == "application/pdf" or (doc.file_name or "").endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw))
            return "\n".join(p.extract_text() or "" for p in reader.pages).strip()
        except Exception as e:
            return f"[Не удалось извлечь PDF: {e}]"

    if mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/msword") or (doc.file_name or "").endswith((".docx", ".doc")):
        try:
            import docx
            document = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in document.paragraphs).strip()
        except Exception as e:
            return f"[Не удалось извлечь Word: {e}]"

    try:
        return raw.decode("utf-8")
    except Exception:
        return "[Неподдерживаемый формат]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler(timezone=ROME_TZ)
    scheduler.add_job(send_morning_digest, CronTrigger(hour=8, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="morning", replace_existing=True)
    # Вечерняя — все дни кроме пятницы
    scheduler.add_job(send_evening_digest,
                      CronTrigger(day_of_week="mon,tue,wed,thu,sat,sun", hour=21, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="evening", replace_existing=True)
    # Пятница 21:00 — расширенная пятничная сводка
    scheduler.add_job(send_friday_digest,
                      CronTrigger(day_of_week="fri", hour=21, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="friday", replace_existing=True)
    # Воскресенье 09:00 — старт недели
    scheduler.add_job(send_sunday_digest,
                      CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="sunday", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started: 08:00 / 21:00 (пн-чт,сб) / пт 21:00 / вс 09:00 Europe/Rome")


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
