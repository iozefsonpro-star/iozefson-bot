"""Google Calendar: чтение событий (read-only), async-обёртка над sync-клиентом."""
import asyncio
import json
import logging
from datetime import datetime, date, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build as gcal_build

import config

logger = logging.getLogger(__name__)

GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Эмодзи по календарям — как в Паоле, чтобы вид сводок совпадал
GCAL_EMOJI = {
    "Business": "💼", "Networking": "👥", "Salute": "🏥",
    "Beauty": "💅", "Mental efficiency": "🧠", "Viaggi": "✈️",
    "Work Vin": "🏠", "Vita": "🏠", "My calendar": "🏠", "Birthday": "🎂",
}
# Календари, которые не показываем совсем.
# Домашние/рабочие календари мужа (🏠 Work Vin, Vita) исключены — их смены
# (R9, R12 и т.п.) не нужны в бизнес-сводке. «My calendar» оставлен как личный Юлии.
GCAL_SKIP = {"Tasks", "Ciclo Yuliya", "Астро-календарь", "Work Vin", "Vita"}
# Отдельные события, которые прячем по названию независимо от календаря.
GCAL_SKIP_TITLES = {"BBR"}
GENERALI_DOMAIN = "@agmonza.it"
# События длиной от стольких дней (курсы, периоды) — не встречи в сетке дня,
# а отдельный блок «В процессе» с датой окончания. Как LONG_EVENT_DAYS в боте.
LONG_EVENT_DAYS = 3

_service_cache: dict = {}


def _get_service_sync():
    now = datetime.now(config.ROME_TZ)
    if _service_cache.get("expires_at") and _service_cache["expires_at"] > now:
        return _service_cache["service"]
    if not config.GOOGLE_TOKEN_JSON:
        return None
    try:
        creds = Credentials.from_authorized_user_info(
            json.loads(config.GOOGLE_TOKEN_JSON), GCAL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        svc = gcal_build("calendar", "v3", credentials=creds, cache_discovery=False)
        _service_cache["service"] = svc
        _service_cache["expires_at"] = now + timedelta(minutes=55)
        return svc
    except Exception as e:
        logger.error("Google credentials error: %s", e)
        return None


def _fetch_events_sync(days: int, from_now: bool) -> dict:
    empty = {"events": [], "long": [], "hidden_past_today": 0}
    service = _get_service_sync()
    if not service:
        return empty
    now = datetime.now(config.ROME_TZ)
    today_iso = now.date().isoformat()
    # Окно запроса к Google всегда с начала дня — from_now не сужает его.
    # Иначе долгое событие (курс на пару месяцев), у которого сегодня как раз
    # заканчивается сессия, могло бы выпасть из окна и пропасть из «В процессе»
    # только из-за того, что "сейчас" позже конца сегодняшней сессии.
    # from_now скрывает прошедшие обычные встречи ниже, в Python, точечно.
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59)

    events: list[dict] = []
    long_events: list[dict] = []
    hidden_past_today = 0
    try:
        calendars = service.calendarList().list().execute().get("items", [])
    except Exception as e:
        logger.error("Calendar list error: %s", e)
        return empty

    for cal in calendars:
        cal_name = cal.get("summary", "")
        if cal_name in GCAL_SKIP:
            continue
        emoji = GCAL_EMOJI.get(cal_name, "📌")
        try:
            items = service.events().list(
                calendarId=cal["id"], timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(), singleEvents=True,
                orderBy="startTime", maxResults=30,
            ).execute().get("items", [])
        except Exception as e:
            logger.warning("Calendar '%s' error: %s", cal_name, e)
            continue

        for ev in items:
            title = ev.get("summary", "(без названия)")
            if title.strip().upper() in GCAL_SKIP_TITLES:
                continue
            # Google Calendar сам прячет отклонённые встречи в своём интерфейсе —
            # API этого не делает по умолчанию, поэтому фильтруем вручную.
            if any(a.get("self") and a.get("responseStatus") == "declined"
                   for a in ev.get("attendees", [])):
                continue
            start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
            end_raw   = ev["end"].get("dateTime", ev["end"].get("date", ""))
            dt_end = None
            if "T" in start_raw:
                dt_start = datetime.fromisoformat(start_raw).astimezone(config.ROME_TZ)
                dt_end   = datetime.fromisoformat(end_raw).astimezone(config.ROME_TZ)
                day_key  = dt_start.date().isoformat()
                time_str = f"{dt_start:%H:%M}–{dt_end:%H:%M}"
                sort_key = dt_start.isoformat()
                duration_days = (dt_end - dt_start).days
            else:
                day_key  = start_raw
                time_str = "весь день"
                sort_key = start_raw
                duration_days = (date.fromisoformat(end_raw)
                                 - date.fromisoformat(start_raw)).days

            if duration_days >= LONG_EVENT_DAYS:
                # Долгое событие держим, пока не прошла его дата окончания —
                # не по точному времени, чтобы не гасло в последний день раньше полуночи.
                if end_raw[:10] >= today_iso and not any(
                        l["title"] == title for l in long_events):
                    long_events.append({"title": title, "end_day": end_raw[:10],
                                        "calendar": cal_name})
                continue

            # from_now прячет уже закончившиеся СЕГОДНЯ обычные встречи;
            # будущие дни (неделя вперёд) не трогаем — они ещё не наступили.
            if from_now and day_key == today_iso and dt_end is not None and dt_end <= now:
                hidden_past_today += 1
                continue

            ev_emoji = emoji
            if any(GENERALI_DOMAIN in a.get("email", "")
                   for a in ev.get("attendees", [])):
                ev_emoji = "💼"

            events.append({
                "day": day_key, "time": time_str, "title": title,
                "emoji": ev_emoji, "calendar": cal_name, "_sort": sort_key,
            })

    events.sort(key=lambda e: e["_sort"])
    long_events.sort(key=lambda e: e["end_day"])
    return {"events": events, "long": long_events, "hidden_past_today": hidden_past_today}


async def get_events_full(days: int = 0, from_now: bool = False) -> dict:
    """События календарей: {"events": встречи, "long": долгие периоды/курсы}.

    days=0 — только сегодня; from_now=True — скрыть уже закончившиеся встречи
    (Google timeMin фильтрует по времени окончания, идущие сейчас остаются).
    """
    return await asyncio.to_thread(_fetch_events_sync, days, from_now)


async def get_events(days: int = 0, from_now: bool = False) -> list[dict]:
    """Только встречи (без долгих событий). days=0 — только сегодня."""
    return (await get_events_full(days, from_now))["events"]


def format_event(ev: dict) -> str:
    return f"{ev['emoji']} {ev['time']} — {ev['title']}"


_MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def format_long(ev: dict) -> str:
    """«Gastronomia - corso (до 24 августа)» — как в блоке «В процессе» бота."""
    try:
        d = date.fromisoformat(ev["end_day"])
        end = f"до {d.day} {_MONTHS_RU[d.month - 1]}"
    except (KeyError, ValueError):
        end = f"до {ev.get('end_day', '?')}"
    return f"{ev['title']} ({end})"
