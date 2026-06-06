import os
import io
import logging
import time
from collections import deque
from datetime import datetime

# Load .env file if present (local dev); on Railway env vars are injected directly
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import json
import tempfile
import anthropic
import httpx
from telegram import Update, Document, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from notion_client import Client as NotionClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Google Calendar
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

# Google Calendar — credentials stored as env vars (never in git)
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON")  # content of token.json

_missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "NOTION_TOKEN": NOTION_TOKEN,
    "NOTION_TODOLIST_DB_ID": NOTION_TODOLIST_DB_ID,
}.items() if not v]

if _missing:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(_missing)}\n"
        "Set them in Railway → Variables or in a local .env file."
    )

AGENTS = {
    "paola":  "Паола — Личный ассистент",
    "carlo":  "Карло — Контент",
    "boris":  "Борис — Аналитик",
    "sandro": "Сандро — Разработчик",
}

AGENTS_PAGE_TITLE = "Агенты"
PROMPT_CACHE_TTL  = 3600  # seconds
HISTORY_LIMIT     = 10    # messages per user to keep

# Model routing: each agent uses the most appropriate Claude model
AGENT_MODELS = {
    "paola":  "claude-haiku-4-5-20251001",  # fast & cheap: briefings, task lists
    "carlo":  "claude-sonnet-4-6",           # quality matters: voice & content style
    "boris":  "claude-sonnet-4-6",           # depth matters: document analysis
    "sandro": "claude-sonnet-4-6",           # precision matters: technical explanations
    "team":   "claude-sonnet-4-6",           # quality: 4 agents answer in sequence
}
DEFAULT_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Conversation history  {user_id: deque([{role, content}, ...])}
# ---------------------------------------------------------------------------
user_histories: dict[int, deque] = {}


def get_history(user_id: int) -> list[dict]:
    return list(user_histories.get(user_id, []))


def add_to_history(user_id: int, role: str, content: str) -> None:
    if user_id not in user_histories:
        user_histories[user_id] = deque(maxlen=HISTORY_LIMIT)
    user_histories[user_id].append({"role": role, "content": content})


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------
notion = NotionClient(auth=NOTION_TOKEN)

_prompt_cache: dict[str, tuple[str, float]] = {}


def _extract_text_from_blocks(blocks: list) -> str:
    parts = []
    for block in blocks:
        btype = block.get("type")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _find_agents_parent_id() -> str | None:
    results = notion.search(
        query=AGENTS_PAGE_TITLE,
        filter={"property": "object", "value": "page"},
    ).get("results", [])
    for page in results:
        title_list = (
            page.get("properties", {})
            .get("title", {})
            .get("title", [])
        )
        title = "".join(t.get("plain_text", "") for t in title_list)
        if title.strip() == AGENTS_PAGE_TITLE:
            return page["id"]
    return None


def _find_agent_page_id(parent_id: str, agent_title: str) -> str | None:
    children = notion.blocks.children.list(block_id=parent_id).get("results", [])
    for block in children:
        if block.get("type") == "child_page":
            title = block["child_page"].get("title", "")
            if agent_title.lower() in title.lower():
                return block["id"]
    return None


def fetch_agent_prompt(agent_key: str) -> str:
    cached = _prompt_cache.get(agent_key)
    if cached and (time.time() - cached[1]) < PROMPT_CACHE_TTL:
        logger.info("Prompt cache hit for %s", agent_key)
        return cached[0]

    agent_title = AGENTS[agent_key]
    logger.info("Fetching prompt for %s from Notion", agent_title)

    parent_id = _find_agents_parent_id()
    if not parent_id:
        raise RuntimeError(f"Notion page '{AGENTS_PAGE_TITLE}' not found")

    page_id = _find_agent_page_id(parent_id, agent_title)
    if not page_id:
        raise RuntimeError(f"Agent page '{agent_title}' not found inside '{AGENTS_PAGE_TITLE}'")

    blocks = notion.blocks.children.list(block_id=page_id).get("results", [])
    prompt = _extract_text_from_blocks(blocks)
    if not prompt:
        prompt = f"Ты {agent_title}. Отвечай по-русски, профессионально и по делу."

    _prompt_cache[agent_key] = (prompt, time.time())
    return prompt


# ---------------------------------------------------------------------------
# To-do list Notion functions
# ---------------------------------------------------------------------------

def notion_create_task(title: str, deadline: str | None, priority: str, section: str, assignee: str) -> dict:
    properties = {
        "Задача":     {"title": [{"text": {"content": title}}]},
        "Приоритет":  {"select": {"name": priority}},
        "Статус":     {"select": {"name": "To do"}},
        "Раздел":     {"select": {"name": section}},
        "Кто делает": {"multi_select": [{"name": assignee}]},
    }
    if deadline:
        properties["Дедлайн"] = {"date": {"start": deadline}}
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
        props = page.get("properties", {})
        title    = "".join(t.get("plain_text", "") for t in props.get("Задача", {}).get("title", []))
        deadline = (props.get("Дедлайн", {}).get("date") or {}).get("start", "")
        priority = (props.get("Приоритет", {}).get("select") or {}).get("name", "")
        status   = (props.get("Статус", {}).get("select") or {}).get("name", "")
        assignee = ", ".join(o["name"] for o in props.get("Кто делает", {}).get("multi_select", []))
        tasks.append({"id": page["id"], "title": title, "deadline": deadline,
                      "priority": priority, "status": status, "assignee": assignee})
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


def notion_delete_task(title: str) -> bool:
    results = notion.databases.query(**{
        "database_id": NOTION_TODOLIST_DB_ID,
        "filter": {"property": "Задача", "rich_text": {"contains": title}},
    }).get("results", [])
    if not results:
        return False
    notion.pages.update(page_id=results[0]["id"], archived=True)
    return True


def format_tasks_list(tasks: list[dict]) -> str:
    if not tasks:
        return "✅ Активных задач нет."
    icon_map = {"Срочное": "🔴", "Важное": "🟡", "Обычное": "🟢"}
    lines = ["📋 Активные задачи:\n"]
    for t in tasks:
        icon     = icon_map.get(t["priority"], "⚪")
        deadline = f" — до {t['deadline']}" if t["deadline"] else ""
        assignee = f" [{t['assignee']}]" if t["assignee"] else ""
        lines.append(f"{icon} {t['title']}{deadline}{assignee}")
    return "\n".join(lines)


def build_notion_context(tasks: list[dict]) -> str:
    """Silent context injected into system prompt — never shown to user."""
    if not tasks:
        return "To-do list пуст."
    lines = ["Текущие задачи пользователя:"]
    for t in tasks:
        deadline = f", дедлайн {t['deadline']}" if t["deadline"] else ""
        lines.append(f"- {t['title']} [{t['priority']}{deadline}, {t['assignee']}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API  — supports conversation history
# ---------------------------------------------------------------------------
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def ask_claude(system_prompt: str, user_message: str,
               history: list[dict] | None = None,
               model: str = DEFAULT_MODEL,
               max_tokens: int = 2048) -> str:
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_message})
    response = claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Morning digest (APScheduler)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Single source account
GCAL_ACCOUNT = "yulia.iozefson.a@gmail.com"

# calendar_name → emoji
GCAL_EMOJI = {
    "Business":          "💼",
    "Networking":        "👥",
    "Salute":            "🏥",
    "Beauty":            "💅",
    "Mental efficiency": "🧠",
    "Viaggi":            "✈️",
    "Астро-календарь":   "🌙",
    "Work Vin":          "🏠",
    "Vita":              "🏠",
    "My calendar":       "🏠",
    "Birthday":          "🎂",
}
# Generali detected by attendee domain — gets 💼 same as Business
GENERALI_DOMAIN = "@agmonza.it"
GCAL_SKIP       = {"Tasks", "Ciclo Yuliya"}  # never show
LONG_EVENT_DAYS = 3  # events longer than this → "В процессе"


def _get_gcal_credentials() -> Credentials | None:
    if not GOOGLE_TOKEN_JSON:
        logger.warning("GOOGLE_TOKEN_JSON not set — calendar disabled")
        return None
    try:
        token_data = json.loads(GOOGLE_TOKEN_JSON)
        creds = Credentials.from_authorized_user_info(token_data, GCAL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info("Google token refreshed")
        return creds
    except Exception as e:
        logger.error("Google credentials error: %s", e)
        return None


def _fmt_time_range(ev: dict, rome_tz) -> tuple[str, str, str]:
    """Return (date_str, time_range_str, sort_key) for an event."""
    from datetime import timedelta
    start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
    end_raw   = ev["end"].get("dateTime",   ev["end"].get("date",   ""))

    if "T" in start_raw:
        dt_start   = datetime.fromisoformat(start_raw).astimezone(rome_tz)
        dt_end     = datetime.fromisoformat(end_raw).astimezone(rome_tz)
        date_str   = dt_start.strftime("%d.%m (%a)")
        time_range = f"{dt_start.strftime('%H:%M')}–{dt_end.strftime('%H:%M')}"
        sort_key   = dt_start.isoformat()
        duration_d = (dt_end - dt_start).days
    else:
        from datetime import date as date_cls
        d_start  = date_cls.fromisoformat(start_raw)
        d_end    = date_cls.fromisoformat(end_raw)
        date_str = d_start.strftime("%d.%m (%a)")
        time_range = "весь день"
        sort_key   = start_raw
        duration_d = (d_end - d_start).days

    return date_str, time_range, sort_key, duration_d, end_raw


def get_calendar_events(days: int = 7) -> dict:
    """
    Returns:
      "by_date": {
          "2026-06-08": [
              {date_label, time, title, emoji, _sort, end_raw, duration}, ...
          ]
      }
      "long":      [{title, end_raw}, ...]   — multi-day events
      "birthdays": [{date_label, title}, ...]
    """
    creds = _get_gcal_credentials()
    if not creds:
        return {}

    try:
        from datetime import timedelta, date as date_cls
        service  = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
        rome_tz  = pytz.timezone("Europe/Rome")
        now      = datetime.now(rome_tz)
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
            cal_id = cal["id"]
            emoji  = GCAL_EMOJI.get(cal_name, "📌")

            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=20,
                ).execute()

                for ev in result.get("items", []):
                    title = ev.get("summary", "(без названия)")
                    if title.strip().upper() == "BBR":
                        continue

                    date_str, time_range, sort_key, duration_d, end_raw = \
                        _fmt_time_range(ev, rome_tz)
                    day_key = sort_key[:10]  # YYYY-MM-DD

                    # Long events → separate block
                    if duration_d >= LONG_EVENT_DAYS:
                        long_evs.append({"title": title, "end_raw": end_raw})
                        continue

                    # Birthdays
                    if cal_name == "Birthday":
                        birthdays.append({"date_label": date_str, "title": title})
                        continue

                    # Override emoji for Generali by attendee domain
                    attendees = ev.get("attendees", [])
                    ev_emoji = emoji
                    if any(GENERALI_DOMAIN in a.get("email", "") for a in attendees):
                        ev_emoji = "💼"

                    item = {
                        "date_label": date_str,
                        "time":       time_range,
                        "title":      title,
                        "emoji":      ev_emoji,
                        "_sort":      sort_key,
                        "end_raw":    end_raw,
                    }
                    by_date.setdefault(day_key, []).append(item)

            except Exception as e:
                logger.warning("Calendar '%s' error: %s", cal_name, e)

        # Sort events within each day chronologically
        for evs in by_date.values():
            evs.sort(key=lambda x: x["_sort"])

        total = sum(len(v) for v in by_date.values()) + len(long_evs) + len(birthdays)
        logger.info("Loaded %d calendar events", total)
        return {"by_date": by_date, "long": long_evs, "birthdays": birthdays}

    except Exception as e:
        logger.error("Google Calendar error: %s", e)
        return {}


def _month_ru(s: str) -> str:
    return (s.replace("Jan","янв").replace("Feb","фев").replace("Mar","мар")
             .replace("Apr","апр").replace("May","май").replace("Jun","июн")
             .replace("Jul","июл").replace("Aug","авг").replace("Sep","сен")
             .replace("Oct","окт").replace("Nov","ноя").replace("Dec","дек"))


def _weekday_ru(s: str) -> str:
    return (s.replace("Monday","Пн").replace("Tuesday","Вт").replace("Wednesday","Ср")
             .replace("Thursday","Чт").replace("Friday","Пт").replace("Saturday","Сб")
             .replace("Sunday","Вс"))


def format_calendar_events(data: dict) -> str:
    """
    Chronological format grouped by date, emoji per event, no Markdown.
    Paola must reproduce this format as plain text without asterisks.
    """
    if not data:
        return ""

    from datetime import date as date_cls
    lines = [
        "События на ближайшие 7 дней (plain text, без звёздочек и Markdown):"
    ]

    by_date = data.get("by_date", {})
    for day_key in sorted(by_date.keys()):
        evs = by_date[day_key]
        if not evs:
            continue
        # Build date header: Пн, 08.06
        try:
            d = date_cls.fromisoformat(day_key)
            weekday = _weekday_ru(d.strftime("%A"))
            date_label = d.strftime("%d.%m")
            lines.append(f"\n📅 {weekday}, {date_label}")
        except Exception:
            lines.append(f"\n📅 {day_key}")
        for ev in evs:
            lines.append(f"{ev['emoji']} {ev['time']} — {ev['title']}")

    # Long / in-progress events
    if data.get("long"):
        lines.append("\n📚 В процессе:")
        for ev in data["long"]:
            try:
                end_d   = date_cls.fromisoformat(ev["end_raw"][:10])
                end_fmt = _month_ru(end_d.strftime("до %d %b"))
            except Exception:
                end_fmt = f"до {ev['end_raw'][:10]}"
            lines.append(f"  {ev['title']} ({end_fmt})")

    # Birthdays
    if data.get("birthdays"):
        lines.append("\n🎂 Дни рождения:")
        for ev in data["birthdays"]:
            lines.append(f"  {ev['date_label']} — {ev['title']}")

    return "\n".join(lines)


ROME_TZ = pytz.timezone("Europe/Rome")


async def _send_digest(bot: Bot, digest_type: str) -> None:
    """Send morning or evening digest to OWNER_CHAT_ID."""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID not set, skipping %s digest", digest_type)
        return
    try:
        tasks = notion_get_tasks()
        now_str = datetime.now(ROME_TZ).strftime("%d.%m.%Y (%A)")
        prompt = fetch_agent_prompt("paola")

        if digest_type == "morning":
            digest_request = (
                f"Сегодня {now_str}. Составь краткий утренний брифинг: "
                "поприветствуй, перечисли активные задачи с приоритетами, "
                "выдели самое важное на сегодня. Будь краткой и бодрой."
            )
        else:
            digest_request = (
                f"Сегодня {now_str}. Составь краткую вечернюю сводку: "
                "подведи итоги дня, напомни о незакрытых задачах, "
                "предложи что стоит сделать завтра. Будь тёплой и поддерживающей."
            )

        events = get_calendar_events(days=7)
        cal_context = format_calendar_events(events)
        system = (
            prompt
            + f"\n\nСЕГОДНЯШНЯЯ ДАТА: {now_str}."
            + f"\n\n{build_notion_context(tasks)}"
            + (f"\n\n{cal_context}" if cal_context else "")
        )
        text = ask_claude(system, digest_request, model=AGENT_MODELS["paola"])
        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
        logger.info("%s digest sent to %s", digest_type.capitalize(), OWNER_CHAT_ID)
    except Exception as e:
        logger.error("%s digest error: %s", digest_type, e)


async def send_morning_digest(bot: Bot) -> None:
    await _send_digest(bot, "morning")


async def send_evening_digest(bot: Bot) -> None:
    await _send_digest(bot, "evening")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_active_agent(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("agent", "paola")


def set_active_agent(context: ContextTypes.DEFAULT_TYPE, agent: str) -> None:
    context.user_data["agent"] = agent


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Привет! Я многоагентный бот.\n\n"
        "Доступные агенты:\n"
        "/paola — Паола, личный ассистент (по умолчанию)\n"
        "/carlo — Карло, контент\n"
        "/boris — Борис, аналитик\n"
        "/sandro — Сандро, разработчик\n"
        "/team — режим совета директоров (все агенты)\n\n"
        "Отправь текст или прикрепи PDF/Word файл."
    )
    await update.message.reply_text(text)


async def _switch_agent(agent_key: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_active_agent(context, agent_key)
    name = AGENTS[agent_key]
    await update.message.reply_text(f"Активный агент: *{name}*", parse_mode="Markdown")


async def cmd_paola(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("paola", update, context)

async def cmd_carlo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("carlo", update, context)

async def cmd_boris(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("boris", update, context)

async def cmd_sandro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("sandro", update, context)

async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_active_agent(context, "team")
    await update.message.reply_text(
        "🏛 Режим *Совет директоров* активирован.\n"
        "Все агенты ответят по очереди.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id      = update.effective_user.id
    user_message = update.message.text or ""
    doc          = update.message.document

    # Extract file text if present
    file_text = ""
    if doc:
        await update.message.reply_text("⏳ Читаю файл...")
        try:
            file_text = await extract_text_from_document(doc, context.bot)
        except Exception as e:
            logger.error("File extraction error: %s", e)
            await update.message.reply_text(f"Ошибка при чтении файла: {e}")
            return

    combined = user_message
    if file_text:
        combined = (
            f"{user_message}\n\n[Содержимое файла {doc.file_name}]:\n{file_text}"
            if user_message else
            f"[Содержимое файла {doc.file_name}]:\n{file_text}"
        )

    if not combined.strip():
        await update.message.reply_text("Пожалуйста, напиши вопрос или прикрепи файл.")
        return

    agent = get_active_agent(context)

    if agent == "team":
        await _handle_team(update, context, combined, user_id)
    else:
        await _handle_single(update, context, agent, combined, user_id)


async def _handle_single(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    agent_key: str,
    message: str,
    user_id: int,
) -> None:
    await update.message.chat.send_action("typing")
    try:
        prompt = fetch_agent_prompt(agent_key)
    except Exception as e:
        logger.error("Notion fetch error for %s: %s", agent_key, e)
        await update.message.reply_text(f"Ошибка при загрузке промпта из Notion: {e}")
        return

    # Paola-specific: show/close/delete tasks via keywords
    if agent_key == "paola":
        msg_lower = message.lower()

        if any(kw in msg_lower for kw in ["покажи задачи", "список задач", "мои задачи"]):
            try:
                tasks = notion_get_tasks()
                await update.message.reply_text(format_tasks_list(tasks))
                return
            except Exception as e:
                logger.error("Notion get tasks error: %s", e)

        if any(kw in msg_lower for kw in ["закрой задачу", "закрыть задачу", "выполнена задача"]):
            for kw in ["закрой задачу", "закрыть задачу", "выполнена задача"]:
                if kw in msg_lower:
                    title = message[msg_lower.index(kw) + len(kw):].strip()
                    if title:
                        try:
                            done = notion_close_task(title)
                            await update.message.reply_text(
                                f"✅ Задача закрыта: {title}" if done else f"❌ Задача не найдена: {title}"
                            )
                            return
                        except Exception as e:
                            logger.error("Notion close task error: %s", e)
                    break

        if any(kw in msg_lower for kw in ["удали задачу", "удалить задачу"]):
            for kw in ["удали задачу", "удалить задачу"]:
                if kw in msg_lower:
                    title = message[msg_lower.index(kw) + len(kw):].strip()
                    if title:
                        try:
                            deleted = notion_delete_task(title)
                            await update.message.reply_text(
                                f"🗑 Задача удалена: {title}" if deleted else f"❌ Задача не найдена: {title}"
                            )
                            return
                        except Exception as e:
                            logger.error("Notion delete task error: %s", e)
                    break

        # Inject date + tasks + calendar silently into system prompt
        today_str = datetime.now().strftime("%d.%m.%Y (%A)")
        prompt = prompt + f"\n\nСЕГОДНЯШНЯЯ ДАТА: {today_str}."
        try:
            tasks = notion_get_tasks()
            prompt = prompt + f"\n\n{build_notion_context(tasks)}"
        except Exception as e:
            logger.warning("Could not load tasks for context: %s", e)
        try:
            events = get_calendar_events(days=7)
            cal_context = format_calendar_events(events)
            if cal_context:
                prompt = (
                    prompt
                    + f"\n\n{cal_context}"
                    + "\n\nПРАВИЛО ОТОБРАЖЕНИЯ КАЛЕНДАРЯ: когда показываешь события — "
                    "используй только plain text и эмодзи. Никаких звёздочек (**), "
                    "подчёркиваний или другого Markdown. Формат строго такой:\n"
                    "📅 Пн, 08.06\n"
                    "💼 09:30–11:00 — Название события"
                )
        except Exception as e:
            logger.warning("Could not load calendar for context: %s", e)

    # Paola: detect task creation intent via Claude
    if agent_key == "paola":
        try:
            today   = datetime.now().strftime("%Y-%m-%d")
            weekday = datetime.now().strftime("%A")
            parse_prompt = (
                f"Сегодняшняя дата: {today} ({weekday}).\n"
                "Проанализируй сообщение пользователя. Если он просит создать, добавить, "
                "записать или запомнить одну или несколько задач/дел/напоминаний — "
                "извлеки параметры КАЖДОЙ задачи отдельно и ответь СТРОГО в формате ниже "
                "(без лишних слов). Для каждой задачи — отдельный блок через '---':\n\n"
                "TASK: YES\n"
                "TITLE: <название>\n"
                "DEADLINE: <YYYY-MM-DD или пусто>\n"
                "PRIORITY: <Срочное|Важное|Обычное>\n"
                "SECTION: <Саморазвитие|Здоровье|Семья|Нетворкинг|Бизнес>\n"
                "ASSIGNEE: <Юля|Паола|Карло|Борис|Сандро>\n\n"
                "Если НЕ просит создать задачи:\nTASK: NO\n\n"
                "Умолчания: PRIORITY=Обычное, SECTION=Бизнес, ASSIGNEE=Юля."
            )
            parsed = ask_claude(parse_prompt, message)
            blocks = [b.strip() for b in parsed.strip().split("---") if b.strip()]
            created_tasks = []

            for block in blocks:
                kv = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                      for l in block.splitlines() if ":" in l}
                if kv.get("TASK", "NO").strip().upper() != "YES":
                    continue
                title    = kv.get("TITLE", "").strip()
                deadline = kv.get("DEADLINE", "").strip() or None
                priority = kv.get("PRIORITY", "Обычное").strip()
                section  = kv.get("SECTION", "Бизнес").strip()
                assignee = kv.get("ASSIGNEE", "Юля").strip()
                if title:
                    notion_create_task(title, deadline, priority, section, assignee)
                    deadline_str = f", до {deadline}" if deadline else ""
                    logger.info("Task created in Notion: %s", title)
                    created_tasks.append(f"• *{title}*{deadline_str} [{priority}, {assignee}]")

            if created_tasks:
                # Send Claude's friendly reply first
                try:
                    agent_model = AGENT_MODELS.get(agent_key, DEFAULT_MODEL)
                    reply = ask_claude(prompt, message, get_history(user_id), model=agent_model)
                    add_to_history(user_id, "user", message)
                    add_to_history(user_id, "assistant", reply)
                    await update.message.reply_text(reply)
                except Exception:
                    pass
                await update.message.reply_text(
                    f"✅ Добавлено в Notion ({len(created_tasks)}):\n" + "\n".join(created_tasks),
                    parse_mode="Markdown"
                )
                return

        except Exception as e:
            logger.error("Task intent detection error: %s", e)

    # Regular Claude reply with history
    try:
        agent_model = AGENT_MODELS.get(agent_key, DEFAULT_MODEL)
        history = get_history(user_id)
        reply   = ask_claude(prompt, message, history, model=agent_model)
        logger.info("Agent %s using model %s", agent_key, agent_model)
    except Exception as e:
        logger.error("Claude API error: %s", e)
        await update.message.reply_text(f"Ошибка Claude API: {e}")
        return

    add_to_history(user_id, "user", message)
    add_to_history(user_id, "assistant", reply)
    await update.message.reply_text(reply)


async def _handle_team(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
    user_id: int,
) -> None:
    await update.message.reply_text("🏛 Совет директоров начинает работу...\n")
    history = get_history(user_id)

    for agent_key, agent_name in AGENTS.items():
        await update.message.chat.send_action("typing")
        try:
            prompt = fetch_agent_prompt(agent_key)
        except Exception as e:
            logger.error("Notion fetch error for %s: %s", agent_key, e)
            await update.message.reply_text(f"*{agent_name}*: Ошибка загрузки промпта — {e}", parse_mode="Markdown")
            continue
        try:
            agent_model = AGENT_MODELS.get(agent_key, DEFAULT_MODEL)
            reply = ask_claude(prompt, message, history, model=agent_model, max_tokens=700)
            logger.info("Team agent %s using model %s", agent_key, agent_model)
        except Exception as e:
            logger.error("Claude API error for %s: %s", agent_key, e)
            await update.message.reply_text(f"*{agent_name}*: Ошибка Claude API — {e}", parse_mode="Markdown")
            continue
        await update.message.reply_text(f"*{agent_name}*:\n{reply}", parse_mode="Markdown")

    # Save last exchange to history
    add_to_history(user_id, "user", message)


# ---------------------------------------------------------------------------
# File text extraction
# ---------------------------------------------------------------------------

async def extract_text_from_document(doc: Document, bot) -> str:
    file = await bot.get_file(doc.file_id)
    buf  = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    raw  = buf.read()
    mime = doc.mime_type or ""

    if mime == "application/pdf" or doc.file_name.endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        except Exception as e:
            logger.warning("PDF extraction failed: %s", e)
            return "[Не удалось извлечь текст из PDF]"

    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or doc.file_name.endswith((".docx", ".doc")):
        try:
            import docx
            document = docx.Document(io.BytesIO(raw))
            return "\n".join(p.text for p in document.paragraphs).strip()
        except Exception as e:
            logger.warning("DOCX extraction failed: %s", e)
            return "[Не удалось извлечь текст из Word-документа]"

    try:
        return raw.decode("utf-8")
    except Exception:
        return "[Неподдерживаемый формат файла]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Called after the event loop is running — safe to start AsyncIOScheduler here."""
    scheduler = AsyncIOScheduler(timezone=ROME_TZ)
    scheduler.add_job(
        send_morning_digest,
        trigger=CronTrigger(hour=8, minute=0, timezone=ROME_TZ),
        args=[app.bot],
        id="morning_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        send_evening_digest,
        trigger=CronTrigger(hour=21, minute=0, timezone=ROME_TZ),
        args=[app.bot],
        id="evening_digest",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — morning 08:00 / evening 21:00 Europe/Rome")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("paola",  cmd_paola))
    app.add_handler(CommandHandler("carlo",  cmd_carlo))
    app.add_handler(CommandHandler("boris",  cmd_boris))
    app.add_handler(CommandHandler("sandro", cmd_sandro))
    app.add_handler(CommandHandler("team",   cmd_team))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
