import os
import io
import logging
import datetime as dt_module
from datetime import datetime, timedelta, date as date_cls

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
NOTION_TODOLIST_DB_ID     = os.environ.get("NOTION_TODOLIST_DB_ID")
NOTION_NEWS_ARCHIVE_DB_ID = os.environ.get("NOTION_NEWS_ARCHIVE_DB_ID")
NOTION_CONTENT_DB_ID      = os.environ.get("NOTION_CONTENT_DB_ID")
OWNER_CHAT_ID             = os.environ.get("OWNER_CHAT_ID")
GOOGLE_TOKEN_JSON         = os.environ.get("GOOGLE_TOKEN_JSON")

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

_news_digest_cache: dict = {}  # {"date": "YYYY-MM-DD", "text": "..."}

NEWS_DIGEST_SYSTEM = (
    "Ты готовишь ежедневный новостной дайджест для Юлии Иозефсон, "
    "независимого бизнес-консультанта (Италия, Дезио).\n\n"
    "Язык ответа: русский. Тон: партнёрский, связный, без рубленых тезисов. "
    "Формат: телефон-first, один экран.\n\n"
    "Структура — шесть рубрик:\n"
    "🇮🇹 Италия и бизнес-климат — макро, экономика, Ломбардия/Милан через призму бизнеса и PMI\n"
    "🏢 Индустрии клиентов — страхование, финансы, made-in-Italy, люкс, hospitality\n"
    "🤖 AI и цифровая трансформация — практическое применение, не хайп\n"
    "⚖️ Регуляторика — AI Act, налоги, партита IVA, трудовое право\n"
    "🌍 Что касается меня лично — Россия (санкции, банки, поездки), Израиль (фоновый радар)\n"
    "💡 Повод для поста — один угол для LinkedIn + готовая первая строка-зацепка\n\n"
    "Правила:\n"
    "- Энергофильтр: каждый пункт должен задевать бизнес, клиентов, рынок или лично Юлию. "
    "«Просто новость» выкидываем.\n"
    "- Свежесть: последние 24–48 часов. Не повторять то, что уже есть в вчерашнем выпуске.\n"
    "- Тишина лучше воды: если в рубрике нет ничего стоящего — пропустить молча.\n"
    "- Trust-лист источников: Il Sole 24 Ore, Corriere (Il Punto), ANSA, ISTAT, Bankitalia, "
    "Confcommercio, Pambianco, BBC, официальные документы ЕС (Consilium), Reuters.\n"
    "- Отвечай только plain text и эмодзи, без Markdown звёздочек."
)

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


def notion_get_latest_news_digest() -> dict | None:
    """Get the most recent entry from Архив новостей database."""
    if not NOTION_NEWS_ARCHIVE_DB_ID:
        return None
    try:
        resp = notion.databases.query(
            database_id=NOTION_NEWS_ARCHIVE_DB_ID,
            sorts=[{"property": "Дата выпуска", "direction": "descending"}],
            page_size=1,
        )
        if not resp.get("results"):
            return None
        page = resp["results"][0]
        date_prop  = page["properties"].get("Дата выпуска", {}).get("date")
        date_str   = date_prop["start"] if date_prop else None
        title_prop = page["properties"].get("Выпуск", {}).get("title", [])
        title      = title_prop[0]["text"]["content"] if title_prop else ""

        blocks     = notion.blocks.children.list(block_id=page["id"])
        body_parts = []
        for block in blocks.get("results", []):
            btype = block.get("type")
            if btype in ("paragraph", "bulleted_list_item", "numbered_list_item",
                         "heading_1", "heading_2", "heading_3"):
                rich_text = block.get(btype, {}).get("rich_text", [])
                text = "".join(rt.get("text", {}).get("content", "") for rt in rich_text)
                if text:
                    body_parts.append(text)

        return {"id": page["id"], "date": date_str, "title": title, "body": "\n".join(body_parts)}
    except Exception as e:
        logger.error("notion_get_latest_news_digest error: %s", e)
        return None


def notion_save_news_digest(date_iso: str, text: str) -> str | None:
    """Create a new entry in Архив новостей. Returns page ID."""
    if not NOTION_NEWS_ARCHIVE_DB_ID:
        return None
    try:
        date_fmt = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
        children = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]},
            }
            for line in text.split("\n")
        ]
        page = notion.pages.create(
            parent={"database_id": NOTION_NEWS_ARCHIVE_DB_ID},
            properties={
                "Выпуск":       {"title": [{"text": {"content": date_fmt}}]},
                "Дата выпуска": {"date": {"start": date_iso}},
            },
            children=children,
        )
        return page["id"]
    except Exception as e:
        logger.error("notion_save_news_digest error: %s", e)
        return None


def notion_create_content_idea(topic: str, hook: str) -> bool:
    """Create an idea entry in ✍️ Контент database."""
    if not NOTION_CONTENT_DB_ID:
        return False
    try:
        notion.pages.create(
            parent={"database_id": NOTION_CONTENT_DB_ID},
            properties={
                "Тема":        {"title": [{"text": {"content": topic}}]},
                "Платформа":   {"select": {"name": "LinkedIn"}},
                "Формат":      {"select": {"name": "Пост"}},
                "Статус":      {"select": {"name": "💡 Идея"}},
                "Комментарий": {"rich_text": [{"text": {"content": hook}}]},
            },
        )
        return True
    except Exception as e:
        logger.error("notion_create_content_idea error: %s", e)
        return False


def _parse_task_props(page: dict) -> dict:
    props = page.get("properties", {})
    return {
        "id":        page["id"],
        "title":     "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", [])),
        "priority":  (props.get("Приоритет", {}).get("select") or {}).get("name", ""),
        "zone":      (props.get("Зона", {}).get("select") or {}).get("name", ""),
        "deadline":  (props.get("Дедлайн", {}).get("date") or {}).get("start", ""),
        "status":    (props.get("Статус", {}).get("select") or {}).get("name", ""),
        "performer": (props.get("Кто делает", {}).get("select") or {}).get("name", ""),
        "project":   (props.get("Проект", {}).get("select") or {}).get("name", ""),
    }


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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
    return tasks


def notion_get_sospeso() -> list[dict]:
    response = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Статус", "select": {"equals": "Sospeso"}},
    })
    tasks = []
    for page in response.get("results", []):
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
    return tasks


def notion_get_tomorrow_important() -> list[dict]:
    tomorrow = (date_cls.today() + timedelta(days=1)).strftime("%Y-%m-%d")
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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
    return tasks


def notion_get_done_this_week() -> list[dict]:
    """Tasks with status Done, last-edited Mon–Fri of current week (Rome TZ)."""
    now        = datetime.now(ROME_TZ)
    week_start = now.date() - timedelta(days=now.weekday())  # Monday
    week_end   = week_start + timedelta(days=4)              # Friday

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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
    return tasks


def notion_get_undone_deadline_this_week() -> list[dict]:
    """Tasks with deadline Mon–Fri of current week, status NOT Done."""
    now        = datetime.now(ROME_TZ)
    week_start = now.date() - timedelta(days=now.weekday())
    week_end   = week_start + timedelta(days=4)

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
        t = _parse_task_props(page)
        if t["title"]:
            tasks.append(t)
    return tasks


def notion_get_next_week_tasks() -> dict:
    """For Sunday digest: tasks grouped for next Mon–Fri."""
    now        = datetime.now(ROME_TZ)
    days_ahead = (7 - now.weekday()) % 7 or 7  # days until next Monday
    next_mon   = now.date() + timedelta(days=days_ahead)
    next_fri   = next_mon + timedelta(days=4)

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

    deadline_tasks  = [t for p in deadline_resp.get("results", []) if (t := _parse_task_props(p))["title"]]
    no_dl_tasks     = [t for p in no_deadline_resp.get("results", []) if (t := _parse_task_props(p))["title"]]
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


def ask_claude_with_search(system: str, user_message: str, max_tokens: int = 4096) -> str:
    """Call Claude API with built-in web search tool for news gathering."""
    response = claude.messages.create(
        model=MODEL_SMART,
        max_tokens=max_tokens,
        system=system,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
        messages=[{"role": "user", "content": user_message}],
    )
    text_parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(text_parts)


def clean_markdown(text: str) -> str:
    text = text.replace("**", "").replace("__", "")
    text = text.replace("* ", "• ").replace("*", "")
    text = text.replace("`", "")
    return text


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GCAL_CACHE_TTL_MINUTES  = 10
GCAL_SERVICE_TTL_MINUTES = 55
_cal_cache: dict     = {}  # {(days, from_now): {"data": ..., "expires_at": ...}}
_gcal_service_cache: dict = {}

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


def _get_gcal_service():
    now = datetime.now(ROME_TZ)
    if _gcal_service_cache.get("expires_at", now) > now:
        return _gcal_service_cache.get("service")
    creds = _get_gcal_credentials()
    if not creds:
        return None
    svc = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
    _gcal_service_cache["service"] = svc
    _gcal_service_cache["expires_at"] = now + timedelta(minutes=GCAL_SERVICE_TTL_MINUTES)
    return svc


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
    now       = datetime.now(ROME_TZ)
    cache_key = (days, from_now)
    entry     = _cal_cache.get(cache_key)
    if entry and entry["expires_at"] > now:
        return entry["data"]
    result = _get_calendar_events_fresh(days=days, from_now=from_now)
    _cal_cache[cache_key] = {"data": result, "expires_at": now + timedelta(minutes=GCAL_CACHE_TTL_MINUTES)}
    return result


def _get_calendar_events_fresh(days: int = 7, from_now: bool = False) -> dict:
    service = _get_gcal_service()
    if not service:
        return {}
    try:
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
                        d_start    = date_cls.fromisoformat(start_raw)
                        d_end      = date_cls.fromisoformat(end_raw)
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


def _format_event_line(ev: dict) -> str:
    return f"{ev['emoji']} {ev['time']} — {ev['title']}"


def format_calendar_events(data: dict) -> str:
    if not data:
        return ""
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
            lines.append(_format_event_line(ev))

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
# Астро-блок
# ---------------------------------------------------------------------------
ASTRO_CAL_NAME = "Астро-календарь"

ASTRO_SYSTEM_PROMPT = """\
Ты — астрологический интерпретатор для ежедневного дайджеста Юлии.

Тебе передают JSON с тремя полями:
- date: дата и день недели
- astro_events: события из астрокалендаря «Сириус» на сегодня (окно 08:00–21:00, Rome time)
- meetings: деловые и личные встречи Юлии на сегодня из Google Calendar

Твоя задача: вернуть готовый текст астро-блока для вставки в утреннюю сводку.

ПРАВИЛА ФОРМИРОВАНИЯ БЛОКА:

Рабочее окно: 08:00–21:00. Если период начался ночью, учитывай только его часть внутри окна.

Принцип фильтрации: не пересказывать каждый период. Выделять только то, что реально влияет на день: работа, презентации/публичность, финансы, переговоры, начинания, отношения. Спокойные нейтральные периоды — одной фоновой строкой, не раздувать.

Связка с календарём — главная ценность:
- Встреча или презентация в неблагоприятный для публичности период (💪 ДОКАЖИ, 🌾 СБОР ПЛОДОВ, ⏸️ ЗАМРИ, 🙏 СВЕРХОЖИДАНИЯ) → пометить ⚠️, посоветовать осторожность.
- Встреча в поддерживающий период (🏆 РОСТ И УСПЕХ, 💡 ВДОХНОВЕНИЕ, 📚 УЧИСЬ, ОБЩАЙСЯ) → отметить как зелёный свет.
- Финансовые/договорные действия в 💧 СЛИВ РЕСУРСА, 💭 ПУТАНИЦА, ❌ РАЗРУШЕНИЕ, ⚠️ РАЗНОГЛАСИЯ → флаг внимания.

Формат вывода — синтетический, 3–5 строк. Связная проза, не список. Структура:
1. Общий характер дня (с указанием смены периодов и времени, если переходов несколько).
2. Что в плюс, на что обратить внимание.
3. ⚠️ Отдельной строкой — конфликты встреч с астропериодами, если есть.

Если день ровный и без острых периодов — одна строка, не нагружать.
Если день острый (💥 КОНФЛИКТ или ❌ РАЗРУШЕНИЕ в рабочие часы) — вынести это вперёд с пометкой внимания.

Первая строка ВСЕГДА: ✨ Астро-день, {день недели} {число} {месяц}
Без преамбулы, без markdown-обёрток, только plain text с эмодзи.

СПРАВОЧНИК 22 ЭНЕРГИЙ:

✅ ВСЁ ПОНРАВИТСЯ — Отличный период для покупок гардероба и интерьера, финансовых вложений, начала сотрудничества и знакомств, процедур красоты, примирения и улучшения отношений. Дела — удовольствие и приятный результат. Отношения — гармония, желание флиртовать. Новости порадуют. Финансы — положительный результат.

💪 ДОКАЖИ — Не подходящее время для публичности, собеседований, презентаций — вероятно непринятие идей. Для результата нужно приложить больше усилий чем обычно. Отношения — недопонимание, акцент на разнице. Важно опираться на харизму и индивидуальность, быть собой.

⚠️ РАЗНОГЛАСИЯ — Неудачный период для финансовых трат, вложений, знакомств. Беспокойство о деньгах и отношениях. Начинания — разочарование из-за ожидания большего. Новости — разочарование, состояние тревоги. Финансы — потеря или меньше ожидаемого. Отношения — столкновение интересов с партнёром.

🌾 СБОР ПЛОДОВ — Сложное время для публичности и презентаций. Можно увидеть результаты трудов. Важно сохранять спокойствие. Начинания — время завершения, результаты прошлых действий. Отношения — показывается скрытое, страхи перерастают в эмоции, недосказанности выходят на поверхность.

🌀 СУЕТА, НЕРВОЗНОСТЬ — Неблагоприятное время для работы с информацией, переговоров, деловых знакомств и поездок. Повышенная суетливость. Предельная внимательность при подписании и составлении документов. Не стоит покупать электронные устройства. Начинания — могут быть просчёты и ошибки. Отношения — общение может привести к дискомфорту. Новости некорректные, есть ошибка. Вопрос решить сложно из-за «расфокуса».

💡 ВДОХНОВЕНИЕ — Время творческого подъёма. Хороший период для публичных выступлений, презентаций, похода в театр. Люди видят нас с лучшей стороны, можно получить похвалу и признание. Начинания — окружение поддержит, результат обрадует. Отношения — радость и лёгкость, взаимопонимание. Не бойтесь действовать.

🙏 СВЕРХОЖИДАНИЯ — Возможны завышенные ожидания, необоснованный оптимизм, борьба с авторитетами. Правовые вопросы решаются не в нашу пользу. Сложно вызвать доверие. Начинания — удар по амбициям. Отношения — завышенные требования. Большие ожидания не оправдываются. Вероятен конфликт с человеком выше по положению.

⚡ НЕ ПО ПЛАНУ — Стоит быть готовым к неожиданным изменениям. Вероятны нервозность, разрывы деловых отношений, аварийные события, сбои техники. Начинания — срыв планов. Отношения — жажда свободы, эпатажное поведение партнёра. Новости — резко, неожиданно. Вопрос отношений — указание на расстояние и независимость.

📚 УЧИСЬ, ОБЩАЙСЯ — Хорошее время для работы с документами, учёбы, переговоров, деловых звонков, чтения книг. Интеллектуальная деятельность будет нравиться. Начинания — легко договориться, поездки удачны. Отношения — возможны случайные знакомства. Новости — скоро будет информация, которая поможет вопросу.

🏆 РОСТ И УСПЕХ — Период удачи и роста во всех делах, связанных с социальной реализацией, правовыми вопросами, обучением. Начинания — рост и успех, помощь со стороны. Отношения — становятся более доверительными и радостными. Новости — есть перспектива роста и улучшения. Период щедрый. Используйте момент — ставьте амбициозную задачу.

❌ РАЗРУШЕНИЕ — Не стоит начинать новые дела. Осторожно с крупными финансами, сделки могут быть разорительными. Избегать массовых мероприятий и скоплений людей. Возможны давление и проверки. Отношения — ревность, манипуляции, тревога. Новости — близится кризис, потеря. Решение болезненно и требует больших ресурсов.

🎯 РЕШАЙСЯ — Время решительных действий, занять лидирующую позицию, победить конкурентов. Начинания — приведут к победе. Отношения — симпатии со стороны мужского пола, повышается страстность. Новости — есть возможность победить. Опираться важно на себя.

⏸️ ЗАМРИ — Неподходящее время для публичности и презентаций. Проведите время спокойно, без активной работы. Начинания — есть скрытые факторы, лучше планировать и отложить реализацию. Отношения — многого не видно, ощущение непонимания. Новости — отсутствует ясность, решений лучше не принимать.

💭 ПУТАНИЦА — Нежелательно планировать дела, требующие концентрации. Информация воспринимается искаженно. Не стоит доверять людям. Осторожно с алкоголем, медикаментами и хим. веществами. Начинания — крушение иллюзий. Отношения — тайны и обманы. Новости — есть скрытый умысел, закончится обманом.

🚧 ПРЕПЯТСТВИЯ — Возможны препятствия, давление обстоятельств. Вероятность задержек, болезней и неприятных ситуаций. Начинания — чреваты помехами. Отношения — охлаждение чувств, дистанция. Новости — затык, остановка, столкновение с внешней проблемой. Возможно столкновение с людьми выше по должности.

💰 РЕСУРС, МАСШТАБ — Сила и напор в делах, в первую очередь финансовых, будут оправданы. Позитив в контактах со структурами власти и налоговыми службами. Хорошее время для начала масштабных проектов. Начинания — выполнение сложных дел по силам. Отношения — глубокая связь на энергетическом уровне. Новости — преобразование ситуации, предпосылки выйти на следующую ступень.

💚 СЛУШАЙ СЕРДЦЕ — Хороший период для творчества, медитаций, отдыха. Чувствительность повышена. Начинания — дело пойдёт, только если вдохновляет. Отношения — глубина и чувство единения. Ответ — опирайтесь не на разум, а слушайте чувства. Предчувствия подскажут. Качество ситуации зависит от того, насколько вы честны с собой и миром.

💥 КОНФЛИКТ — Повышенная активность и напор. Возможны ссоры и конфликты. Вероятны аварии на дорогах, травматизм в быту. Начинания — время прорыва, обязательно личное включение без делегирования. Отношения — споры, отсутствие взаимопонимания. Новости — есть скрытый фактор, результат затянется. Новое дело лучше не начинать, доделывайте то, что есть.

🔒 СТАБИЛЬНОСТЬ — Благоприятное время для приведения дел в порядок, подвести итоги, избавиться от лишнего. Удачное время для решения вопросов с чиновниками. Начинания — устойчивый и долговечный результат. Отношения — стабилизация, переход в новый статус. Работа — надёжность и постепенный рост.

💧 СЛИВ РЕСУРСА — Возможны потери ресурсов, искушения, обманы, разочарования, недопонимания. Не стоит идти на поводу у удовольствий, осторожно относиться к заманчивым предложениям. Не принимать поспешных решений под влиянием эмоций или страхов. Если вопрос о партнёрстве — имеется скрытый умысел, партнёр нечестен.

🌙 ПУСТЫЕ НАЧИНАНИЯ — Луна без курса. Не стоит браться за новые дела, лучше экономить силы. Рекомендуется заниматься привычными делами. Новости — ситуация никак не изменится. Ухудшения нет. Спокойно можете проходить мимо предложения и родившейся идеи.

🌀 ПЕРЕМЕНЫ — Актуальна тема свободы и независимости. Идеальное время для экспериментов и новизны. Начинания — приведут к приятным неожиданным изменениям. Отношения — новые знакомства, скорее дружеские. Новости — ситуация быстро и неожиданно меняется. Важно делать то, что не делали, экспериментировать. Ориентироваться на инновационный подход.\
"""


def get_astro_events(today_iso: str) -> list[dict]:
    """Fetch Астро-календарь events intersecting 08:00–21:00 Rome window."""
    service = _get_gcal_service()
    if not service:
        return []
    try:
        day          = datetime.strptime(today_iso, "%Y-%m-%d")
        window_start = ROME_TZ.localize(day.replace(hour=8,  minute=0, second=0, microsecond=0))
        window_end   = ROME_TZ.localize(day.replace(hour=21, minute=0, second=0, microsecond=0))

        cal_list      = service.calendarList().list().execute().get("items", [])
        cal_names     = [c.get("summary", "") for c in cal_list]
        logger.info("Astro: calendars in list: %s", cal_names)
        astro_cal = next((c for c in cal_list if c.get("summary") == ASTRO_CAL_NAME), None)
        if not astro_cal:
            logger.warning("Астро-календарь не найден. Доступные: %s", cal_names)
            return []

        # timeMin = начало дня, timeMax = начало следующего дня
        # (использование конца дня 21:00 отсекает all-day events в Google Calendar API)
        day_start = ROME_TZ.localize(day.replace(hour=0, minute=0, second=0, microsecond=0))
        day_next  = day_start + timedelta(days=1)
        result = service.events().list(
            calendarId=astro_cal["id"],
            timeMin=day_start.isoformat(),
            timeMax=day_next.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        ).execute()

        raw_items = result.get("items", [])
        logger.info("Astro: found %d events before filtering", len(raw_items))

        events = []
        for ev in raw_items:
            title     = ev.get("summary", "(без названия)")
            start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
            end_raw   = ev["end"].get("dateTime",   ev["end"].get("date", ""))
            if "T" in start_raw:
                dt_start = datetime.fromisoformat(start_raw).astimezone(ROME_TZ)
                dt_end   = datetime.fromisoformat(end_raw).astimezone(ROME_TZ)
                # пропустить события заканчивающиеся до 08:00
                if dt_end <= window_start:
                    continue
                events.append({
                    "name":  title,
                    "start": dt_start.strftime("%H:%M"),
                    "end":   dt_end.strftime("%H:%M"),
                })
            else:
                # all-day events — включаем всегда (астро-период на весь день)
                events.append({"name": title, "start": "весь день", "end": "весь день"})

        logger.info("Astro: %d events after filtering: %s", len(events), [e["name"] for e in events])
        return events
    except Exception as e:
        logger.error("Astro calendar fetch error: %s", e)
        return []


def build_astro_block(astro_events: list[dict], meetings: list[dict], today_display: str) -> str:
    """Ask Claude to generate the astro block. Returns empty string on any failure."""
    if not astro_events:
        return ""
    try:
        payload = json.dumps(
            {"date": today_display, "astro_events": astro_events, "meetings": meetings},
            ensure_ascii=False,
        )
        text = ask_claude(ASTRO_SYSTEM_PROMPT, payload, model=MODEL_SMART, max_tokens=400)
        return text.strip()
    except Exception as e:
        logger.error("Astro block Claude error: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Дайджесты
# ---------------------------------------------------------------------------

def _sort_by_priority(tasks: list[dict]) -> list[dict]:
    return sorted(tasks, key=lambda t: PRIORITY_ORDER.index(t["priority"])
                  if t.get("priority") in PRIORITY_ORDER else 2)


async def _send_digest(bot: Bot, digest_type: str, app=None) -> None:
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

            today_meetings = [
                {"title": ev["title"], "time": ev["time"]}
                for ev in events.get("by_date", {}).get(today_iso, [])
            ]
            astro_events = get_astro_events(today_iso)
            astro_block  = build_astro_block(astro_events, today_meetings, now_str)

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

            # 5. Астро-блок
            if astro_block:
                parts.append(astro_block)

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
                if app is not None:
                    ud = app.user_data.setdefault(int(OWNER_CHAT_ID), {})
                    ud["pending_overdue"] = list(overdue)
                    ud["last_task"] = overdue[0]["title"]

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
            parts.append(f"  {_format_event_line(ev)}")
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
        now     = datetime.now(ROME_TZ)
        now_str = now.strftime("%d.%m.%Y")

        week_start = now.date() - timedelta(days=now.weekday())
        saturday   = week_start + timedelta(days=5)
        sunday     = week_start + timedelta(days=6)

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
        sat_line = (", ".join(_format_event_line(ev) for ev in sat_events)
                    if sat_events else "свободно")
        sun_line = (", ".join(_format_event_line(ev) for ev in sun_events)
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
            day      = next_mon + timedelta(days=i)
            day_fmt  = day.strftime("%d.%m")
            day_evs  = cal_by_date.get(str(day), [])
            ev_str   = (", ".join(_format_event_line(ev) for ev in day_evs)
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


async def send_evening_digest(bot: Bot, app=None) -> None:
    await _send_digest(bot, "evening", app=app)


async def prepare_news_digest(bot: Bot) -> None:
    """08:25 — call Claude with web search, save result to Архив новостей."""
    if not OWNER_CHAT_ID:
        return
    try:
        now       = datetime.now(ROME_TZ)
        today_iso = now.strftime("%Y-%m-%d")

        last_digest      = notion_get_latest_news_digest()
        yesterday_block  = ""
        if last_digest and last_digest.get("body"):
            yesterday_block = f"\n\nВчерашний выпуск (не повторять):\n{last_digest['body'][:2000]}"

        user_msg = (
            f"Подготовь дайджест новостей за последние 24–48 часов. "
            f"Сегодня {now.strftime('%d.%m.%Y')}.{yesterday_block}"
        )
        digest_text = ask_claude_with_search(NEWS_DIGEST_SYSTEM, user_msg, max_tokens=4096)

        if digest_text.strip():
            _news_digest_cache["date"] = today_iso
            _news_digest_cache["text"] = digest_text
            notion_save_news_digest(today_iso, digest_text)
            logger.info("News digest prepared and saved for %s", today_iso)
        else:
            logger.warning("prepare_news_digest: empty response from Claude")
    except Exception as e:
        logger.error("prepare_news_digest error: %s", e)


async def send_news_digest(bot: Bot) -> None:
    """08:30 — send today's news digest to Telegram."""
    if not OWNER_CHAT_ID:
        return
    try:
        today_iso = datetime.now(ROME_TZ).strftime("%Y-%m-%d")
        if _news_digest_cache.get("date") == today_iso and _news_digest_cache.get("text"):
            digest_body = _news_digest_cache["text"]
        else:
            digest = notion_get_latest_news_digest()
            if not digest or not digest.get("body"):
                logger.warning("send_news_digest: no digest found in archive")
                return
            digest_body = digest["body"]

        now_str = datetime.now(ROME_TZ).strftime("%d.%m.%Y")
        text    = f"📰 Новостной дайджест — {now_str}\n\n{digest_body}"

        if len(text) > 4096:
            text = text[:4090] + "..."

        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
        logger.info("News digest sent")
    except Exception as e:
        logger.error("send_news_digest error: %s", e)


# ---------------------------------------------------------------------------
# Обработчики сообщений
# ---------------------------------------------------------------------------

async def cmd_check_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        creds = _get_gcal_credentials()
        if not creds:
            await update.message.reply_text(
                "❌ Нет подключения к Google Calendar.\n"
                "Причина: переменная GOOGLE_TOKEN_JSON не задана или токен невалидный."
            )
            return
        if not creds.valid:
            await update.message.reply_text(
                "❌ Токен Google Calendar недействителен и не может быть обновлён.\n"
                "Нужно переавторизоваться через auth_google.py."
            )
            return
        service = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
        calendars = service.calendarList().list().execute().get("items", [])
        lines = [f"✅ Google Calendar подключён. Найдено календарей: {len(calendars)}\n"]
        for cal in calendars:
            name = cal.get("summary", "?")
            cal_id = cal.get("id", "?")
            primary = " (основной)" if cal.get("primary") else ""
            lines.append(f"• {name}{primary}\n  {cal_id}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при проверке Calendar: {e}")


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


_DIGEST_KW = ["сводку", "сводка", "дайджест", "что сегодня", "покажи день",
              "план на день", "что у меня сегодня", "что у меня на сегодня"]


def _classify_intent(message: str, today_iso: str) -> str:
    prompt = (
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
        "TAKE_POST — взять повод для поста из новостного дайджеста в работу. "
        "Примеры: 'возьми пост в работу', 'запиши идею поста', 'сохрани пост', "
        "'добавь в контент', 'возьми в работу'\n"
        "OTHER — всё остальное"
    )
    try:
        return ask_claude(prompt, message, model=MODEL_SMART).strip()
    except Exception as e:
        logger.error("Intent error: %s", e)
        return "OTHER"


async def _handle_overdue_response(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    pending_tasks: list, today_iso: str, tomorrow_str: str,
) -> bool:
    """Handle user response to an overdue task prompt. Returns True if handled."""
    msg_lower  = (update.message.text or "").lower()
    task_title = context.user_data.get("last_task", "") or (pending_tasks[0]["title"] if pending_tasks else "")
    if not task_title:
        return False

    task_id = pending_tasks[0]["id"] if pending_tasks else None
    handled = False

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
                update.message.text or "", model=MODEL_SMART,
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

    return handled


async def _handle_show_digest(update: Update) -> None:
    try:
        await update.message.reply_text(build_intraday_digest())
    except Exception as e:
        logger.error("Intraday digest error: %s", e)
        await update.message.reply_text(f"Ошибка: {e}")


async def _handle_show_all(update: Update) -> None:
    tasks = notion_get_tasks()
    await update.message.reply_text(format_tasks_list(tasks))


async def _handle_show_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_close_task(update: Update, description: str) -> None:
    all_tasks = notion_get_tasks()
    matched   = find_task_by_description(description, all_tasks)
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


async def _handle_reschedule(update: Update, payload: str) -> None:
    parts = payload.split("|")
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


async def _handle_create_task(update: Update, message: str, today_iso: str) -> None:
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
    except Exception as e:
        logger.error("Task creation error: %s", e)


async def _handle_take_post(update: Update, message: str) -> None:
    try:
        today_iso   = datetime.now(ROME_TZ).strftime("%Y-%m-%d")
        if _news_digest_cache.get("date") == today_iso and _news_digest_cache.get("text"):
            digest_body = _news_digest_cache["text"]
        else:
            last_digest = notion_get_latest_news_digest()
            if not last_digest or not last_digest.get("body"):
                await update.message.reply_text(
                    "❓ Не нашла свежий дайджест в архиве. Попробуй после 08:30."
                )
                return
            digest_body = last_digest["body"]

        extract_prompt = (
            "Из дайджеста извлеки рубрику '💡 Повод для поста' и верни строго:\n"
            "ТЕМА: <краткая тема поста>\n"
            "ХУК: <первая строка-зацепка из дайджеста>\n\n"
            f"Дайджест:\n{digest_body}"
        )
        parsed = ask_claude(extract_prompt, message, model=MODEL_SMART)
        kv = {}
        for line in parsed.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                kv[key.strip()] = val.strip()

        topic = kv.get("ТЕМА", "").strip()
        hook  = kv.get("ХУК", "").strip()

        if not topic:
            await update.message.reply_text("❓ Не смогла извлечь тему из дайджеста.")
            return

        if notion_create_content_idea(topic, hook):
            await update.message.reply_text("Идея для поста сохранена в Контент 💡")
            logger.info("Content idea created: %s", topic)
        else:
            await update.message.reply_text("❌ Ошибка при сохранении в Контент.")
    except Exception as e:
        logger.error("TAKE_POST error: %s", e)
        await update.message.reply_text(f"Ошибка: {e}")


async def _handle_other(update: Update, message: str) -> None:
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    today_date   = date_cls.today()
    tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
    today_iso    = today_date.strftime("%Y-%m-%d")

    msg_lower     = message.lower()
    pending_tasks = context.user_data.get("pending_overdue", [])

    if pending_tasks:
        handled = await _handle_overdue_response(update, context, pending_tasks, today_iso, tomorrow_str)
        if handled:
            return

    if msg_lower.strip() in ("астро тест", "/astro"):
        try:
            dbg_events = get_astro_events(today_iso)
            if not dbg_events:
                await update.message.reply_text("Астро: событий не найдено (get_astro_events вернул [])")
            else:
                lines = [f"Найдено {len(dbg_events)} астро-событий:"]
                for e in dbg_events:
                    lines.append(f"• {e['name']} ({e['start']}–{e['end']})")
                block = build_astro_block(dbg_events, [], _weekday_ru(datetime.now(ROME_TZ).strftime("%d.%m.%Y (%A)")))
                lines.append("\n--- Блок от Claude ---")
                lines.append(block if block else "(Claude вернул пустой ответ)")
                await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Астро тест ошибка: {e}")
        return

    if any(kw in msg_lower for kw in _DIGEST_KW) and not pending_tasks:
        await _handle_show_digest(update)
        return

    intent = _classify_intent(message, today_iso)
    logger.info("Intent: %s", intent)

    if intent.startswith("SHOW_DIGEST"):
        await _handle_show_digest(update)
    elif intent.startswith("SHOW_ALL"):
        await _handle_show_all(update)
    elif intent.startswith("SHOW_OVERDUE"):
        await _handle_show_overdue(update, context)
    elif intent.startswith("CLOSE:"):
        await _handle_close_task(update, intent[6:].strip())
    elif intent.startswith("RESCHEDULE:"):
        await _handle_reschedule(update, intent[11:])
    elif intent.startswith("CREATE_TASK"):
        await _handle_create_task(update, message, today_iso)
    elif intent.startswith("TAKE_POST"):
        await _handle_take_post(update, message)
    else:
        await _handle_other(update, message)


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
    # Новостной дайджест — подготовка 08:25, отправка 08:30
    scheduler.add_job(prepare_news_digest, CronTrigger(hour=8, minute=25, timezone=ROME_TZ),
                      args=[app.bot], id="news_prepare", replace_existing=True)
    scheduler.add_job(send_news_digest, CronTrigger(hour=8, minute=30, timezone=ROME_TZ),
                      args=[app.bot], id="news_send", replace_existing=True)
    # Вечерняя — все дни кроме пятницы
    scheduler.add_job(send_evening_digest,
                      CronTrigger(day_of_week="mon,tue,wed,thu,sat,sun", hour=21, minute=0, timezone=ROME_TZ),
                      args=[app.bot, app], id="evening", replace_existing=True)
    # Пятница 21:00 — расширенная пятничная сводка
    scheduler.add_job(send_friday_digest,
                      CronTrigger(day_of_week="fri", hour=21, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="friday", replace_existing=True)
    # Воскресенье 09:00 — старт недели
    scheduler.add_job(send_sunday_digest,
                      CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=ROME_TZ),
                      args=[app.bot], id="sunday", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started: 08:00 / 08:25 (news_prepare) / 08:30 (news_send) / 21:00 (пн-чт,сб) / пт 21:00 / вс 09:00 Europe/Rome")


def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check_calendar", cmd_check_calendar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
