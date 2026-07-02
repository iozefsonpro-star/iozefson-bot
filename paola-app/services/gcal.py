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
GCAL_SKIP = {"Tasks", "Ciclo Yuliya", "Астро-календарь"}
GENERALI_DOMAIN = "@agmonza.it"

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


def _fetch_events_sync(days: int, from_now: bool) -> list[dict]:
    service = _get_service_sync()
    if not service:
        return []
    now = datetime.now(config.ROME_TZ)
    time_min = now if from_now else now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59)

    events: list[dict] = []
    try:
        calendars = service.calendarList().list().execute().get("items", [])
    except Exception as e:
        logger.error("Calendar list error: %s", e)
        return []

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
            start_raw = ev["start"].get("dateTime", ev["start"].get("date", ""))
            end_raw   = ev["end"].get("dateTime", ev["end"].get("date", ""))
            if "T" in start_raw:
                dt_start = datetime.fromisoformat(start_raw).astimezone(config.ROME_TZ)
                dt_end   = datetime.fromisoformat(end_raw).astimezone(config.ROME_TZ)
                day_key  = dt_start.date().isoformat()
                time_str = f"{dt_start:%H:%M}–{dt_end:%H:%M}"
                sort_key = dt_start.isoformat()
            else:
                day_key  = start_raw
                time_str = "весь день"
                sort_key = start_raw

            ev_emoji = emoji
            if any(GENERALI_DOMAIN in a.get("email", "")
                   for a in ev.get("attendees", [])):
                ev_emoji = "💼"

            events.append({
                "day": day_key, "time": time_str, "title": title,
                "emoji": ev_emoji, "calendar": cal_name, "_sort": sort_key,
            })

    events.sort(key=lambda e: e["_sort"])
    return events


async def get_events(days: int = 0, from_now: bool = False) -> list[dict]:
    """События календарей. days=0 — только сегодня."""
    return await asyncio.to_thread(_fetch_events_sync, days, from_now)


def format_event(ev: dict) -> str:
    return f"{ev['emoji']} {ev['time']} — {ev['title']}"
